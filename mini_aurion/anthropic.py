"""Native Anthropic adapter (Messages API).

Same protocol and fail-closed rules as muse.py: unparseable answers
become evidence-free replies, missing usage settles at worst case,
transport failures raise. Transport itself is shared
(provider_http); only the wire shape here is Anthropic's:

- POST {base}/v1/messages, x-api-key header, anthropic-version pinned.
- system prompt rides as a top-level field, not a message.
- Consecutive user turns are merged: the API rejects back-to-back
  user messages, and the harness never sends assistant history.
- Tools convert to {name, description, input_schema}; answers come
  back as tool_use blocks with native object input.
"""
from __future__ import annotations

import json
import os
import urllib.request
from dataclasses import dataclass, field

from .muse import SYSTEM_PROMPT
from .provider_http import (check_base_url, estimate_input_tokens,
                            merged_user_texts, opener_for, parse_text_reply,
                            post_json, price_nano)

API_KEY_ENV_VAR = "ANTHROPIC_API_KEY"
BASE_URL_ENV_VAR = "MINI_AURION_ANTHROPIC_BASE_URL"
MODEL_ENV_VAR = "MINI_AURION_ANTHROPIC_MODEL"
PRICE_INPUT_ENV_VAR = "MINI_AURION_ANTHROPIC_PRICE_IN"
PRICE_OUTPUT_ENV_VAR = "MINI_AURION_ANTHROPIC_PRICE_OUT"

DEFAULT_BASE_URL = "https://api.anthropic.com"
ANTHROPIC_VERSION = "2023-06-01"
DEFAULT_MAX_OUTPUT_TOKENS = 4096
DEFAULT_TIMEOUT_SECONDS = 60.0
DEFAULT_MAX_RESPONSE_BYTES = 256 * 1024


class AnthropicError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.code = code


@dataclass(frozen=True)
class AnthropicConfig:
    api_key: str = field(repr=False)
    base_url: str = DEFAULT_BASE_URL
    model: str = ""
    input_usd_per_m: str = ""
    output_usd_per_m: str = ""
    max_output_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES
    temperature: float = 0.0
    provider: str = "anthropic"

    def __post_init__(self):
        if not self.api_key:
            raise AnthropicError("NO_KEY", f"{API_KEY_ENV_VAR} is not set")
        check_base_url(self.base_url, AnthropicError)
        if not self.model:
            raise AnthropicError(
                "NO_MODEL", f"{MODEL_ENV_VAR} names no model")
        if not self.input_usd_per_m or not self.output_usd_per_m:
            raise AnthropicError(
                "NO_PRICES",
                f"set {PRICE_INPUT_ENV_VAR} and {PRICE_OUTPUT_ENV_VAR}"
                " (unknown prices cannot reserve spend)")
        if self.max_output_tokens <= 0 or self.timeout_seconds <= 0 \
                or self.max_response_bytes <= 0:
            raise AnthropicError("BAD_CONFIG", "limits must be positive")

    @classmethod
    def from_env(cls, environ=None, **overrides):
        env = environ if environ is not None else os.environ
        return cls(api_key=overrides.get(
                       "api_key", env.get(API_KEY_ENV_VAR, "")),
                   base_url=overrides.get(
                       "base_url",
                       env.get(BASE_URL_ENV_VAR, DEFAULT_BASE_URL)),
                   model=overrides.get(
                       "model", env.get(MODEL_ENV_VAR, "")),
                   input_usd_per_m=env.get(PRICE_INPUT_ENV_VAR, ""),
                   output_usd_per_m=env.get(PRICE_OUTPUT_ENV_VAR, ""))


class AnthropicModel:
    """Live Anthropic model behind the specialist/orchestrator protocol."""

    def __init__(self, config: AnthropicConfig):
        self.config = config
        self.provider = config.provider
        self.model = config.model
        self.url = config.base_url.rstrip("/") + "/v1/messages"
        self.last_cost_nano = 0
        self.calls = 0
        self._opener = opener_for(config.base_url, AnthropicError)

    def worst_case_nano(self, messages: list,
                        max_new_text_chars: int = 0) -> int:
        body_chars = len(json.dumps(messages)) + max_new_text_chars
        in_tokens = estimate_input_tokens(body_chars)
        return (price_nano(self.config.input_usd_per_m, in_tokens)
                + price_nano(self.config.output_usd_per_m,
                             self.config.max_output_tokens))

    def complete(self, messages: list, tools: list,
                 system_prompt: str | None = None) -> dict:
        self.calls += 1
        body = {"model": self.config.model,
                "max_tokens": self.config.max_output_tokens,
                "temperature": self.config.temperature,
                "system": system_prompt or SYSTEM_PROMPT,
                "messages": [{"role": "user", "content": text}
                             for text in merged_user_texts(messages)]}
        if tools:
            body["tools"] = [
                {"name": t.get("name", ""),
                 "description": t.get("description", ""),
                 "input_schema": t.get("parameters", {"type": "object"})}
                for t in tools]
        raw_body = json.dumps(body).encode("utf-8")
        request = urllib.request.Request(
            self.url, data=raw_body,
            headers={"x-api-key": self.config.api_key,
                     "anthropic-version": ANTHROPIC_VERSION,
                     "Content-Type": "application/json"})
        response = post_json(self._opener, request,
                             self.config.timeout_seconds,
                             self.config.max_response_bytes,
                             AnthropicError)
        self.last_cost_nano = self._cost_of(response, len(raw_body))
        return self._to_reply(response)

    def _cost_of(self, response: dict, request_bytes: int) -> int:
        usage = response.get("usage") if isinstance(response, dict) else None
        if isinstance(usage, dict):
            prompt = usage.get("input_tokens")
            completion = usage.get("output_tokens")
            if isinstance(prompt, int) and isinstance(completion, int):
                return (price_nano(self.config.input_usd_per_m, prompt)
                        + price_nano(self.config.output_usd_per_m,
                                     completion))
        return self.worst_case_nano([], request_bytes)

    def _to_reply(self, response: dict) -> dict:
        blocks = []
        try:
            content = response.get("content", [])
            if isinstance(content, list):
                blocks = content
        except AttributeError:
            blocks = []
        tool_calls = []
        texts = []
        for block in blocks:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use":
                args = block.get("input", {})
                if isinstance(args, dict):
                    tool_calls.append({"tool": str(block.get("name", "")),
                                       "args": args})
            elif block.get("type") == "text":
                texts.append(str(block.get("text", "")))
        if tool_calls:
            return {"text": "\n".join(texts), "tool_calls": tool_calls,
                    "evidence": [], "confidence": 0.0, "subtasks": []}
        return parse_text_reply("\n".join(texts))
