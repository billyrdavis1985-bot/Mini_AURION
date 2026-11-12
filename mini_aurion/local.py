"""Local volume tier: an OpenAI-compatible model server (Ollama-style
``/v1/chat/completions``) behind the same specialist/orchestrator
protocol as MuseModel.

Same reply shape (text, tool calls, evidence, confidence), same
redirect refusal, same response caps: a tier swap changes cost and
capability, never the harness contract. Local tokens price at zero;
electricity is the operator's business, not the ledger's.

Plain http is allowed for loopback and private-LAN hosts (a Pi on the
home network is the expected deployment). There is no key to steal and
no paid account behind the URL, so the https-everywhere rule for paid
APIs does not apply; public http is still refused.
"""
from __future__ import annotations

import dataclasses
import ipaddress
import os
from urllib.parse import urlsplit

from .muse import (DEFAULT_MAX_OUTPUT_TOKENS,
                   DEFAULT_MAX_RESPONSE_BYTES, DEFAULT_TIMEOUT_SECONDS,
                   MuseConfig, MuseError, MuseModel, _check_base_url)

LOCAL_BASE_URL_ENV_VAR = "MINI_AURION_LOCAL_BASE_URL"
LOCAL_MODEL_ENV_VAR = "MINI_AURION_LOCAL_MODEL"
DEFAULT_LOCAL_BASE_URL = "http://127.0.0.1:11434/v1"


def _is_private_host(host: str | None) -> bool:
    if not host:
        return False
    try:
        return ipaddress.ip_address(host).is_private
    except ValueError:
        return host.lower() in ("localhost",)


def _check_local_base_url(url: str) -> str:
    try:
        return _check_base_url(url)
    except MuseError:
        pass
    parts = urlsplit(url)
    if parts.scheme == "http" and _is_private_host(parts.hostname):
        return url.rstrip("/")
    raise MuseError("BAD_URL", "local URL must be loopback, private-LAN"
                    " http, or https")


@dataclasses.dataclass(frozen=True)
class LocalConfig(MuseConfig):
    api_key: str = "local"
    base_url: str = DEFAULT_LOCAL_BASE_URL
    model: str = ""
    input_usd_per_m: str = "0"
    output_usd_per_m: str = "0"
    max_output_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES
    temperature: float = 0.0

    def __post_init__(self):
        # Same limits, looser transport: private-LAN http is allowed.
        if self.max_output_tokens <= 0 or self.timeout_seconds <= 0 \
                or self.max_response_bytes <= 0:
            raise MuseError("BAD_CONFIG", "limits must be positive")
        _check_local_base_url(self.base_url)

    @classmethod
    def from_env(cls, environ=None, **overrides):
        env = environ if environ is not None else os.environ
        base_url = overrides.get(
            "base_url", env.get(LOCAL_BASE_URL_ENV_VAR,
                                DEFAULT_LOCAL_BASE_URL))
        model = overrides.get(
            "model", env.get(LOCAL_MODEL_ENV_VAR, ""))
        if not model:
            raise MuseError("NO_LOCAL_MODEL",
                            f"{LOCAL_MODEL_ENV_VAR} names no model")
        return cls(api_key="local", base_url=base_url, model=model)


class LocalModel(MuseModel):
    """Volume tier. Today this is the paid-API protocol pointed at a
    local server; the subclass is the seam for local-only fallbacks
    (small models often fumble structured tool calls)."""

    def __init__(self, config: LocalConfig):
        super().__init__(config)
