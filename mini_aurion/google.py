"""Native Google adapter (Gemini generateContent API).

Same protocol and fail-closed rules as muse.py: unparseable answers
become evidence-free replies, missing usage settles at worst case,
transport failures raise. Transport itself is shared
(provider_http); only the wire shape here is Google's:

- POST {base}/v1beta/models/{model}:generateContent, API key in the
  x-goog-api-key header (never in the URL query).
- system prompt rides as system_instruction parts, not a message.
- Turns merge to one user message: roles must alternate starting
  with user, and the harness never sends model history.
- Tools convert to functionDeclarations; answers come back as
  functionCall parts with native object args.
- usageMetadata (prompt/candidates tokens) prices the call. A
  content block with no candidates is an evidence-free reply: the
  critic fails it and the loop retries or escalates.
"""
from __future__ import annotations

import json
import os
import urllib.parse
import urllib.request
from dataclasses import dataclass, field

from .muse import SYSTEM_PROMPT
from .provider_http import (check_base_url, estimate_input_tokens,
                            merged_user_texts, opener_for, parse_text_reply,
                            post_json, price_nano)

API_KEY_ENV_VAR = "GOOGLE_API_KEY"
BASE_URL_ENV_VAR = "MINI_AURION_GOOGLE_BASE_URL"
MODEL_ENV_VAR = "MINI_AURION_GOOGLE_MODEL"
PRICE_INPUT_ENV_VAR = "MINI_AURION_GOOGLE_PRICE_IN"
PRICE_OUTPUT_ENV_VAR = "MINI_AURION_GOOGLE_PRICE_OUT"

DEFAULT_BASE_URL = "https://generativelanguage.googleapis.com"
DEFAULT_MAX_OUTPUT_TOKENS = 4096
DEFAULT_TIMEOUT_SECONDS = 60.0
DEFAULT_MAX_RESPONSE_BYTES = 256 * 1024


class GoogleError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.code = code


@dataclass(frozen=True)
class GoogleConfig:
    api_key: str = field(repr=False)
    base_url: str = DEFAULT_BASE_URL
    model: str = ""
    input_usd_per_m: str = ""
    output_usd_per_m: str = ""
    max_output_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES
    temperature: float = 0.0
    provider: str = "google"

    def __post_init__(self):
        if not self.api_key:
            raise GoogleError("NO_KEY", f"{API_KEY_ENV_VAR} is not set")
        check_base_url(self.base_url, GoogleError)
        if not self.model:
            raise GoogleError(
                "NO_MODEL", f"{MODEL_ENV_VAR} names no model")
        if not self.input_usd_per_m or not self.output_usd_per_m:
            raise GoogleError(
                "NO_PRICES",
                f"set {PRICE_INPUT_ENV_VAR} and {PRICE_OUTPUT_ENV_VAR}"
                " (unknown prices cannot reserve spend)")
        if self.max_output_tokens <= 0 or self.timeout_seconds <= 0 \
                or self.max_response_bytes <= 0:
            raise GoogleError("BAD_CONFIG", "limits must be positive")

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


class GoogleModel:
    """Live Google model behind the specialist/orchestrator protocol."""

    def __init__(self, config: GoogleConfig):
        self.config = config
        self.provider = config.provider
        self.model = config.model
        # The model id rides in the path, so it is quoted: a hostile or
        # sloppy id must not escape the path segment.
        safe_model = urllib.parse.quote(config.model, safe="")
        self.url = (config.base_url.rstrip("/") + "/v1beta/models/"
                    + safe_model + ":generateContent")
        self.last_cost_nano = 0
        self.calls = 0
        self._opener = opener_for(config.base_url, GoogleError)

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
        body = {"system_instruction": {
                    "parts": [{"text": system_prompt or SYSTEM_PROMPT}]},
                "contents": [{"role": "user",
                              "parts": [{"text": text}]}
                             for text in merged_user_texts(messages)],
                "generationConfig": {
                    "temperature": self.config.temperature,
                    "maxOutputTokens": self.config.max_output_tokens}}
        if tools:
            body["tools"] = [{"functionDeclarations": [
                {"name": t.get("name", ""),
                 "description": t.get("description", ""),
                 "parameters": t.get("parameters", {"type": "object"})}
                for t in tools]}]
        raw_body = json.dumps(body).encode("utf-8")
        request = urllib.request.Request(
            self.url, data=raw_body,
            headers={"x-goog-api-key": self.config.api_key,
                     "Content-Type": "application/json"})
        response = post_json(self._opener, request,
                             self.config.timeout_seconds,
                             self.config.max_response_bytes,
                             GoogleError)
        self.last_cost_nano = self._cost_of(response, len(raw_body))
        return self._to_reply(response)

    def _cost_of(self, response: dict, request_bytes: int) -> int:
        usage = response.get("usageMetadata") \
            if isinstance(response, dict) else None
        if isinstance(usage, dict):
            prompt = usage.get("promptTokenCount")
            completion = usage.get("candidatesTokenCount")
            if isinstance(prompt, int) and isinstance(completion, int):
                return (price_nano(self.config.input_usd_per_m, prompt)
                        + price_nano(self.config.output_usd_per_m,
                                     completion))
        return self.worst_case_nano([], request_bytes)

    def _to_reply(self, response: dict) -> dict:
        parts = []
        try:
            candidates = response.get("candidates", [])
            if candidates:
                content = candidates[0].get("content", {})
                if isinstance(content.get("parts"), list):
                    parts = content["parts"]
        except AttributeError:
            parts = []
        tool_calls = []
        texts = []
        for part in parts:
            if not isinstance(part, dict):
                continue
            call = part.get("functionCall")
            if isinstance(call, dict):
                args = call.get("args", {})
                if isinstance(args, dict):
                    tool_calls.append({"tool": str(call.get("name", "")),
                                       "args": args})
            elif isinstance(part.get("text"), str):
                texts.append(part["text"])
        if tool_calls:
            return {"text": "\n".join(texts), "tool_calls": tool_calls,
                    "evidence": [], "confidence": 0.0, "subtasks": []}
        return parse_text_reply("\n".join(texts))
