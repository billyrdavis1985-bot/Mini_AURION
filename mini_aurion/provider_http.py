"""Shared HTTP plumbing for native model adapters.

muse.py was the first adapter; anthropic.py and google.py reuse its
fail-closed transport instead of duplicating it: loopback-only plain
http, no redirects, identifier-scrubbed error shorts, char-to-token
estimates, the internal-role-to-user mapping, and the text-to-reply
parse. One copy, so a transport fix lands everywhere.
"""
from __future__ import annotations

import http.client
import ipaddress
import json
import re
import urllib.error
import urllib.request
from urllib.parse import urlsplit

from .specialist import MAX_TOOL_CHARS

NANO_PER_USD = 1_000_000_000
# Per-message cap. Derived from the producer's cap, with headroom: the
# specialist emits MAX_TOOL_CHARS plus its own marker, and an equal cap here
# would cut that marker off and misreport the already-cut size as the file's
# size. Any cut here is marked, never silent.
MAX_MESSAGE_CHARS = MAX_TOOL_CHARS + 200

_ERROR_IDENTIFIER = re.compile(r"[A-Za-z0-9_.\[\]-]{1,64}")
_FENCED_JSON = re.compile(r"```(?:json|JSON)?[ \t]*\r?\n(.*?)\r?\n[ \t]*```",
                          re.DOTALL)


def is_loopback(host: str | None) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host or "").is_loopback
    except ValueError:
        return False


def check_base_url(url: str, error) -> str:
    """https, or plain http to loopback for tests. error(code, msg)
    builds the adapter's own exception."""
    parts = urlsplit(url)
    if parts.username or parts.password or parts.query or parts.fragment \
            or not parts.hostname:
        raise error("BAD_URL", "base URL must be a plain https URL")
    if parts.scheme == "https" or (parts.scheme == "http"
                                   and is_loopback(parts.hostname)):
        return url.rstrip("/")
    raise error("BAD_URL",
                "https required (plain http only for loopback tests)")


def opener_for(base_url: str, error):
    """Opener that refuses redirects. Loopback skips proxies so tests
    never depend on the operator's proxy env."""
    class _NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers,
                             newurl):
            raise error("REDIRECT_REFUSED", "redirects are refused")

    if is_loopback(urlsplit(base_url).hostname):
        return urllib.request.build_opener(
            _NoRedirect, urllib.request.ProxyHandler({}))
    return urllib.request.build_opener(_NoRedirect)


def check_prices(input_usd_per_m: str, output_usd_per_m: str,
                 error) -> None:
    """Refuse unparseable prices at config time. A typo'd price must
    fail the build, never detonate mid-run inside Decimal."""
    from decimal import Decimal, InvalidOperation
    for value in (input_usd_per_m, output_usd_per_m):
        try:
            Decimal(value)
        except (InvalidOperation, ValueError, TypeError):
            raise error("BAD_PRICES",
                        f"price {value!r} is not a number") from None


def price_nano(price_per_m: str, tokens: int) -> int:
    from decimal import Decimal
    return int(Decimal(price_per_m) * tokens * NANO_PER_USD / 1_000_000)


def estimate_input_tokens(body_chars: int) -> int:
    return max(1, body_chars // 4)


def api_user_message(message: dict) -> dict:
    """Map every internal role to user text. Internal roles (brief,
    tool_result, planner-chosen subtask ids) are untrusted labels, so they
    ride inside the text where they cannot become instructions."""
    role = str(message.get("role", "user"))
    text = str(message.get("text", ""))
    if len(text) > MAX_MESSAGE_CHARS:
        text = (text[:MAX_MESSAGE_CHARS] +
                f"\n[truncated: showing {MAX_MESSAGE_CHARS} of {len(text)}"
                " chars]")
    # Tool results name their request: without the tool and args the model
    # cannot tell its own past calls apart and re-issues them (measured
    # live: list_dir . three times in one task). Tool and args are
    # harness-known, not model-supplied, so they are safe to render.
    if role == "tool_result":
        args_repr = repr(message.get("args", {}))
        if len(args_repr) > 200:
            args_repr = args_repr[:200] + "...[args truncated]"
        text = (f"[tool_result {message.get('tool', '')}"
                f" {args_repr}]\n{text}")
    elif role != "user":
        text = f"[{role}] {text}"
    return {"role": "user", "content": text}


def merged_user_texts(messages: list) -> list[str]:
    """User contents with consecutive turns joined. Strict-alternation
    APIs (Anthropic, Google) reject back-to-back user messages; the
    harness never sends assistant history, so joining loses nothing."""
    texts = [api_user_message(m)["content"] for m in messages]
    return ["\n\n".join(texts)] if texts else [""]


def short_http_error(exc: urllib.error.HTTPError) -> str:
    try:
        raw = exc.read(2048).decode("utf-8", "replace")
        data = json.loads(raw)
        error = data.get("error", {}) if isinstance(data, dict) else {}
        kept = {}
        for key in ("type", "code", "param", "status"):
            value = error.get(key)
            if isinstance(value, str) and _ERROR_IDENTIFIER.fullmatch(value):
                kept[key] = value
        return f"HTTP {exc.code} {kept or 'no-detail'}"
    except Exception:  # noqa: BLE001 - error parsing never fails the call path
        return f"HTTP {exc.code}"


def post_json(opener, request, timeout_seconds: float,
              max_response_bytes: int, error) -> dict:
    """Send one POST and decode the JSON body, fail-closed."""
    try:
        with opener.open(request, timeout=timeout_seconds) as resp:
            payload = resp.read(max_response_bytes + 1)
    except urllib.error.HTTPError as exc:
        raise error("HTTP_ERROR", short_http_error(exc)) from exc
    except (urllib.error.URLError, http.client.HTTPException,
            TimeoutError, OSError) as exc:
        raise error("TRANSPORT_FAILED",
                    f"{type(exc).__name__}") from exc
    if len(payload) > max_response_bytes:
        raise error("RESPONSE_TOO_LARGE", "response exceeded the cap")
    try:
        return json.loads(payload.decode("utf-8"))
    except ValueError as exc:
        raise error("BAD_RESPONSE", "response is not JSON") from exc


def parse_text_reply(text: str) -> dict:
    """Answer text to protocol reply: CANNOT_VERIFY passes through,
    JSON claims parse, anything else is an evidence-free reply the
    critic will fail. A confusing model can never verify itself."""
    stripped = text.strip()
    if stripped == "CANNOT_VERIFY":
        return {"text": stripped, "tool_calls": [], "evidence": [],
                "confidence": 0.0, "subtasks": []}
    candidate = stripped
    fenced = _FENCED_JSON.search(stripped)
    if fenced:
        candidate = fenced.group(1)
    try:
        parsed = json.loads(candidate)
    except ValueError:
        parsed = None
    if not isinstance(parsed, dict):
        return {"text": stripped, "tool_calls": [], "evidence": [],
                "confidence": 0.0, "subtasks": []}
    evidence = parsed.get("evidence", [])
    if not isinstance(evidence, list):
        evidence = []
    try:
        confidence = float(parsed.get("confidence", 0.0))
    except (TypeError, ValueError):
        confidence = 0.0
    subtasks = parsed.get("subtasks", [])
    if not isinstance(subtasks, list):
        subtasks = []
    return {"text": str(parsed.get("claim", stripped)),
            "tool_calls": [], "evidence": evidence,
            "confidence": min(max(confidence, 0.0), 1.0),
            "subtasks": subtasks}
