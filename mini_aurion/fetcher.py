"""fetch_url: outside pages in, snapshots out (ADR-003 D1, D5).

A page is fetched once, its text extracted with the standard library,
and the whole thing stored content-addressed under the task's state
directory. The critic (B2 PR2) checks quotes against those bytes, so a
later check proves what the agent saw even after the page changes.

D5 treats every page as hostile input: the URL is parsed strictly, the
name is resolved once, the address it resolves to is classified before
connecting, the connection goes to that checked address (never
re-resolving, which a rebinding DNS server would answer differently),
every redirect hop is re-checked, and only HTML and plain text under
the size cap come back. No cookies, no credentials, no proxies, no
JavaScript. Text reaches the model only through the existing
tool-result wrapping.
"""
from __future__ import annotations

import hashlib
import http.client
import ipaddress
import json
import socket
import ssl
import time
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin, urlsplit

DEFAULT_TIMEOUT_SECONDS = 20.0
DEFAULT_MAX_BYTES = 256_000
MAX_REDIRECTS = 5
ALLOWED_CONTENT_TYPES = ("text/html", "text/plain")
USER_AGENT = "Mini-AURION-research/1 (+local research tool; no cookies)"


class FetchError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.code = code


def address_is_public(ip_text: str) -> bool:
    """True only for globally routable unicast addresses. Everything
    else — loopback, private, link-local, multicast, reserved,
    unspecified — is refused before connecting (D5). The is_global
    conjunct catches shared address space (100.64.0.0/10), which
    ipaddress calls neither private nor global; mesh VPNs hand the
    operator's own machines addresses there. is_global alone would
    admit 64:ff9b::/96, which the list below still refuses."""
    try:
        ip = ipaddress.ip_address(ip_text)
    except ValueError:
        return False
    return ip.is_global and not (
        ip.is_loopback or ip.is_private or ip.is_link_local
        or ip.is_multicast or ip.is_reserved or ip.is_unspecified)


def parse_url(url: str) -> tuple:
    """Strict parse: http(s), no credentials, a hostname, a sane port."""
    if not isinstance(url, str) or not url:
        raise FetchError("FETCH_BAD_URL", "URL must be a non-empty string")
    try:
        parts = urlsplit(url)
    except ValueError as exc:
        raise FetchError("FETCH_BAD_URL", f"unparseable URL: {exc}") from None
    if parts.scheme not in ("http", "https"):
        raise FetchError("FETCH_BAD_URL",
                         f"scheme {parts.scheme!r} is not http or https")
    if parts.username or parts.password:
        raise FetchError("FETCH_BAD_URL", "credentials in a URL are refused")
    host = parts.hostname or ""
    if not host:
        raise FetchError("FETCH_BAD_URL", "URL has no hostname")
    try:
        port = parts.port or (443 if parts.scheme == "https" else 80)
    except ValueError:
        raise FetchError("FETCH_BAD_URL", "bad port") from None
    if not 1 <= port <= 65535:
        raise FetchError("FETCH_BAD_URL", "bad port")
    path = parts.path or "/"
    if parts.query:
        path += "?" + parts.query
    return parts.scheme, host, port, path


def resolve_and_check(host: str, port: int,
                      allow_private: bool = False) -> str:
    """Resolve once; return the address to connect to, or refuse.

    The caller connects to the returned address and never resolves
    again, so a rebinding answer cannot swap the destination between
    the check and the connection. allow_private exists for tests
    against loopback fake servers; the runner never sets it.
    """
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise FetchError("FETCH_DNS",
                         f"cannot resolve {host!r}") from exc
    if not infos:
        raise FetchError("FETCH_DNS", f"cannot resolve {host!r}")
    address = infos[0][4][0]
    if not allow_private and not address_is_public(address):
        raise FetchError("FETCH_REFUSED",
                         f"{host!r} resolves to non-public {address}")
    return address


class _TextExtractor(HTMLParser):
    """HTML to text. Script and style are dropped; noscript is kept
    (nothing here executes JavaScript, so noscript is what a reader
    would see). Block tags break lines; entities are unescaped."""

    _BLOCKS = {"p", "div", "br", "h1", "h2", "h3", "h4", "h5", "h6",
               "li", "ul", "ol", "tr", "table", "section", "article",
               "header", "footer", "title"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self._skip += 1
        elif tag in self._BLOCKS:
            self._parts.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style") and self._skip:
            self._skip -= 1

    def handle_data(self, data):
        if not self._skip:
            self._parts.append(data)

    def text(self) -> str:
        return "".join(self._parts)


def html_to_text(body: bytes, content_type: str) -> str:
    charset = "utf-8"
    for chunk in content_type.split(";")[1:]:
        name, _, value = chunk.partition("=")
        if name.strip().lower() == "charset" and value.strip():
            charset = value.strip().strip("\"'")[:40] or "utf-8"
    try:
        page = body.decode(charset, errors="replace")
    except (LookupError, ValueError):
        page = body.decode("utf-8", errors="replace")
    if content_type.split(";")[0].strip().lower() == "text/html":
        parser = _TextExtractor()
        parser.feed(page)
        parser.close()
        return parser.text()
    return page


def _tcp_connect(address: str, port: int, timeout: float) -> socket.socket:
    """TCP to a checked address. Built by hand, not create_connection,
    because create_connection resolves its argument again — the second
    resolution a rebinding server waits for."""
    family = socket.AF_INET6 if ipaddress.ip_address(address).version == 6 \
        else socket.AF_INET
    sock = socket.socket(family, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    sock.connect((address, port))
    return sock


class _PinnedHTTPConnection(http.client.HTTPConnection):
    """Plain HTTP to a checked address: same single-resolution rule."""

    def connect(self):
        self.sock = _tcp_connect(self.host, self.port, self.timeout)


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """TLS to the hostname over TCP to the checked address."""

    def __init__(self, address: str, port: int, hostname: str, *,
                 timeout: float, context: ssl.SSLContext):
        super().__init__(address, port, timeout=timeout, context=context)
        self._verified_hostname = hostname

    def connect(self):
        sock = _tcp_connect(self.host, self.port, self.timeout)
        self.sock = self._context.wrap_socket(
            sock, server_hostname=self._verified_hostname)


def _content_type_ok(value: str | None) -> str:
    if not value:
        raise FetchError("FETCH_BAD_TYPE", "missing content type")
    mime = value.split(";")[0].strip().lower()
    if mime not in ALLOWED_CONTENT_TYPES:
        raise FetchError("FETCH_BAD_TYPE",
                         f"content type {mime!r} is not fetchable")
    return value


def _one_hop(scheme: str, address: str, host: str, port: int, path: str,
             timeout_seconds: float, max_bytes: int) -> tuple:
    """One checked request. Returns (status, headers, body) or raises."""
    host_header = host if port in (80, 443) else f"{host}:{port}"
    headers = {"Host": host_header, "User-Agent": USER_AGENT,
               "Accept": "text/html,text/plain",
               "Connection": "close"}
    try:
        if scheme == "https":
            context = ssl.create_default_context()
            conn = _PinnedHTTPSConnection(address, port, host,
                                          timeout=timeout_seconds,
                                          context=context)
        else:
            conn = _PinnedHTTPConnection(address, port,
                                         timeout=timeout_seconds)
        conn.request("GET", path, headers=headers)
        resp = conn.getresponse()
        status = resp.status
        location = resp.getheader("Location")
        content_type = resp.getheader("Content-Type")
        length = resp.getheader("Content-Length")
        if length is not None:
            try:
                if int(length) > max_bytes:
                    raise FetchError("FETCH_TOO_LARGE",
                                     "content length exceeds the cap")
            except ValueError:
                pass
        body = resp.read(max_bytes + 1)
    except FetchError:
        raise
    except (OSError, http.client.HTTPException) as exc:
        raise FetchError("FETCH_TRANSPORT",
                         f"{type(exc).__name__}") from exc
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001 - close path never fails the fetch
            pass
    if len(body) > max_bytes:
        raise FetchError("FETCH_TOO_LARGE", "body exceeds the cap")
    return status, content_type, location, body


def fetch_page(url: str, *, timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
               max_bytes: int = DEFAULT_MAX_BYTES,
               allow_private: bool = False,
               max_redirects: int = MAX_REDIRECTS) -> dict:
    """Fetch one URL through the D5 gates. Returns the final URL,
    status, content type and extracted text."""
    current = url
    for _ in range(max_redirects + 1):
        scheme, host, port, path = parse_url(current)
        address = resolve_and_check(host, port,
                                    allow_private=allow_private)
        status, content_type, location, body = _one_hop(
            scheme, address, host, port, path,
            timeout_seconds, max_bytes)
        if status in (301, 302, 303, 307, 308) and location:
            current = urljoin(current, location)
            continue
        if status != 200:
            raise FetchError("FETCH_HTTP", f"HTTP {status}")
        content_type = _content_type_ok(content_type)
        return {"final_url": current, "status": status,
                "content_type": content_type,
                "text": html_to_text(body, content_type)}
    raise FetchError("FETCH_TOO_MANY_REDIRECTS",
                     f"more than {max_redirects} redirects")


def write_snapshot(snapshot_dir, *, url: str, final_url: str,
                   status: int, content_type: str, text: str) -> tuple:
    """Store the page content-addressed. Returns (hash, path). The same
    bytes always land at the same path, so a re-fetch that changed
    nothing costs one file, and a quote's hash names immutable bytes."""
    snapshot = {
        "url": url,
        "final_url": final_url,
        "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "status": status,
        "content_type": content_type,
        "text": text,
    }
    canonical = json.dumps(snapshot, sort_keys=True,
                           separators=(",", ":")).encode("utf-8")
    digest = hashlib.sha256(canonical).hexdigest()
    out_dir = Path(snapshot_dir) / "snapshots"
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{digest}.json"
    if not path.exists():
        path.write_text(canonical.decode("utf-8"), encoding="utf-8")
    return digest, str(path)


def fetch_url(url: str, *, snapshot_dir,
              timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
              max_bytes: int = DEFAULT_MAX_BYTES,
              allow_private: bool = False,
              max_redirects: int = MAX_REDIRECTS) -> dict:
    """Fetch, snapshot, and report. The model-facing result carries the
    snapshot hash it must cite; the bytes live under snapshot_dir."""
    page = fetch_page(url, timeout_seconds=timeout_seconds,
                      max_bytes=max_bytes, allow_private=allow_private,
                      max_redirects=max_redirects)
    digest, _ = write_snapshot(
        snapshot_dir, url=url, final_url=page["final_url"],
        status=page["status"], content_type=page["content_type"],
        text=page["text"])
    return {"text": page["text"], "snapshot": digest,
            "final_url": page["final_url"], "status": page["status"],
            "content_type": page["content_type"]}


class FetchBackend:
    """Registry backend. The snapshot hash rides the settle receipt
    (via last_receipt_detail, like last_cost_nano) so the receipt
    names the bytes the model read."""

    def __init__(self, snapshot_dir, *, timeout_seconds: float =
                 DEFAULT_TIMEOUT_SECONDS, max_bytes: int = DEFAULT_MAX_BYTES,
                 allow_private: bool = False):
        self.snapshot_dir = snapshot_dir
        self.timeout_seconds = timeout_seconds
        self.max_bytes = max_bytes
        self.allow_private = allow_private
        self.last_cost_nano = 0
        self.last_receipt_detail: dict | None = None

    def call(self, name: str, args: dict) -> dict:
        if name != "fetch_url":
            raise KeyError(f"unknown tool {name!r}")
        if not isinstance(args, dict) or not args.get("url"):
            raise FetchError("FETCH_BAD_URL", "fetch_url needs a url")
        out = fetch_url(
            args["url"], snapshot_dir=self.snapshot_dir,
            timeout_seconds=self.timeout_seconds, max_bytes=self.max_bytes,
            allow_private=self.allow_private)
        self.last_cost_nano = 0
        self.last_receipt_detail = {
            "snapshot": out["snapshot"], "final_url": out["final_url"],
            "status": out["status"]}
        return out


def register_fetch_tool(registry, snapshot_dir, **kwargs) -> None:
    """Register fetch_url as a READ tool. The research runner calls
    this; code-QA never does, so its path and prompts stay unchanged."""
    registry.register(
        "fetch_url", "READ", 0, FetchBackend(snapshot_dir, **kwargs),
        description="Fetch a public web page (HTML or plain text)."
                    " Returns extracted text plus the snapshot hash to"
                    " cite. Loopback, private and intranet addresses,"
                    " non-text types, oversize bodies and redirect loops"
                    " are refused.",
        parameters={"type": "object",
                    "properties": {"url": {"type": "string"}},
                    "required": ["url"]})
