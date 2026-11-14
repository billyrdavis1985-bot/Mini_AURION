"""fetch_url: D5 safety gates and D1 snapshots. Fake loopback servers
only, plus unit tests; no real network. The PR's one real fetch is
Evidence, not a test."""
from __future__ import annotations

import hashlib
import http.server
import json
import os
import socket
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mini_aurion.fetcher import (FetchBackend, FetchError,  # noqa: E402
                                 _PinnedHTTPSConnection,
                                 address_is_public, fetch_page, fetch_url,
                                 html_to_text, parse_url,
                                 register_fetch_tool, resolve_and_check,
                                 write_snapshot)
from mini_aurion.ledger import Ledger  # noqa: E402
from mini_aurion.receipts import ReceiptStore  # noqa: E402
from mini_aurion.tools import ToolRegistry, ToolWrapper  # noqa: E402

CAPS = {"task_cap_nano": 10_000, "daily_cap_nano": 50_000,
        "global_cap_nano": 100_000}


class AddressTests(unittest.TestCase):
    def test_only_global_unicast_passes(self):
        for bad in ("127.0.0.1", "::1", "10.0.0.9", "172.16.4.4",
                    "192.168.1.1", "169.254.10.20", "224.0.0.1",
                    "0.0.0.0", "::", "not-an-ip"):
            self.assertFalse(address_is_public(bad), bad)
        for good in ("8.8.8.8", "1.1.1.1", "93.184.216.34",
                     "2606:4700:4700::1111"):
            self.assertTrue(address_is_public(good), good)


class ParseTests(unittest.TestCase):
    def test_strict_shapes(self):
        self.assertEqual(parse_url("https://example.com/a?b=c"),
                         ("https", "example.com", 443, "/a?b=c"))
        self.assertEqual(parse_url("http://example.com:8080"),
                         ("http", "example.com", 8080, "/"))
        for bad in ("ftp://example.com/", "file:///etc/passwd",
                    "https://user:pass@example.com/",
                    "https://", "https:///path", "",
                    "https://example.com:99999/"):
            with self.assertRaises(FetchError, msg=bad) as ctx:
                parse_url(bad)
            self.assertEqual(ctx.exception.code, "FETCH_BAD_URL")


class ResolveTests(unittest.TestCase):
    def test_public_answer_returns_the_checked_address(self):
        with mock.patch("socket.getaddrinfo") as probed:
            probed.return_value = [(2, 1, 6, "", ("93.184.216.34", 443))]
            self.assertEqual(resolve_and_check("example.com", 443),
                             "93.184.216.34")

    def test_private_answer_is_refused_after_resolution(self):
        with mock.patch("socket.getaddrinfo") as probed:
            probed.return_value = [(2, 1, 6, "", ("127.0.0.1", 80))]
            with self.assertRaises(FetchError) as ctx:
                resolve_and_check("rebind.evil.example", 80)
            self.assertEqual(ctx.exception.code, "FETCH_REFUSED")

    def test_unresolvable_name_is_a_dns_error(self):
        with mock.patch("socket.getaddrinfo",
                        side_effect=socket.gaierror("nope")):
            with self.assertRaises(FetchError) as ctx:
                resolve_and_check("missing.invalid", 443)
            self.assertEqual(ctx.exception.code, "FETCH_DNS")

    def test_literal_loopback_ip_is_refused(self):
        with self.assertRaises(FetchError) as ctx:
            resolve_and_check("127.0.0.1", 80)
        self.assertEqual(ctx.exception.code, "FETCH_REFUSED")


class ExtractTests(unittest.TestCase):
    def test_script_and_style_are_dropped(self):
        body = (b"<html><head><style>.x{color:red}</style>"
                b"<title>T</title></head><body><p>Hello <b>world</b></p>"
                b"<script>alert(1)</script><noscript>plain</noscript>"
                b"</body></html>")
        text = html_to_text(body, "text/html; charset=utf-8")
        self.assertIn("Hello", text)
        self.assertIn("world", text)
        self.assertIn("plain", text)
        self.assertNotIn("alert", text)
        self.assertNotIn("color", text)

    def test_entities_unescape_and_plain_passes_through(self):
        self.assertIn("a & b", html_to_text(b"a &amp; b", "text/html"))
        self.assertEqual(html_to_text(b"raw bytes", "text/plain"), "raw bytes")


class SnapshotTests(unittest.TestCase):
    def test_same_bytes_same_hash_same_file(self):
        tmp = tempfile.TemporaryDirectory(prefix="ma-snap-")
        self.addCleanup(tmp.cleanup)
        first = write_snapshot(tmp.name, url="https://e.com/", final_url="x",
                               status=200, content_type="text/plain",
                               text="same")
        second = write_snapshot(tmp.name, url="https://e.com/", final_url="x",
                                status=200, content_type="text/plain",
                                text="same")
        self.assertEqual(first, second)
        self.assertTrue(Path(first[1]).exists())
        third = write_snapshot(tmp.name, url="https://e.com/", final_url="x",
                               status=200, content_type="text/plain",
                               text="different")
        self.assertNotEqual(first[0], third[0])


class _Pages(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _send(self, code, ctype, body, extra=None):
        data = body if isinstance(body, bytes) else body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):  # noqa: N802
        if self.path == "/page":
            self._send(200, "text/html",
                       "<html><body><p>quoted line here</p></body></html>")
        elif self.path == "/text":
            self._send(200, "text/plain", "plain words")
        elif self.path == "/json":
            self._send(200, "application/json", '{"a": 1}')
        elif self.path == "/big":
            self._send(200, "text/plain", b"x" * 5000)
        elif self.path == "/redir":
            self._send(302, "text/plain", "go",
                       {"Location": "/page"})
        elif self.path == "/loop":
            self._send(302, "text/plain", "again",
                       {"Location": "/loop"})
        elif self.path == "/missing":
            self._send(404, "text/plain", "gone")
        elif self.path == "/notype":
            data = b"mystery"
            self.send_response(200)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        else:
            self._send(404, "text/plain", "gone")


class FetchIntegrationTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="ma-fetch-")
        self.addCleanup(tmp.cleanup)
        self.dir = tmp.name
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0),
                                                      _Pages)
        thread = threading.Thread(target=self.server.serve_forever,
                                  daemon=True)
        thread.start()
        self.addCleanup(thread.join, 5)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def test_html_page_returns_text_and_snapshot(self):
        out = fetch_url(self.base + "/page", snapshot_dir=self.dir,
                        allow_private=True)
        self.assertIn("quoted line here", out["text"])
        self.assertEqual(out["final_url"], self.base + "/page")
        snap = Path(self.dir) / "snapshots" / f"{out['snapshot']}.json"
        stored = json.loads(snap.read_text())
        self.assertIn("quoted line here", stored["text"])
        self.assertEqual(stored["url"], self.base + "/page")

    def test_plain_page_returns_text(self):
        out = fetch_url(self.base + "/text", snapshot_dir=self.dir,
                        allow_private=True)
        self.assertEqual(out["text"], "plain words")

    def test_wrong_type_missing_type_and_oversize_refuse(self):
        for path, code in (("/json", "FETCH_BAD_TYPE"),
                           ("/notype", "FETCH_BAD_TYPE"),
                           ("/missing", "FETCH_HTTP")):
            with self.assertRaises(FetchError, msg=path) as ctx:
                fetch_url(self.base + path, snapshot_dir=self.dir,
                          allow_private=True)
            self.assertEqual(ctx.exception.code, code)
        with self.assertRaises(FetchError) as ctx:
            fetch_url(self.base + "/big", snapshot_dir=self.dir,
                      allow_private=True, max_bytes=100)
        self.assertEqual(ctx.exception.code, "FETCH_TOO_LARGE")

    def test_redirect_is_followed_and_recorded(self):
        out = fetch_url(self.base + "/redir", snapshot_dir=self.dir,
                        allow_private=True)
        self.assertIn("quoted line here", out["text"])
        self.assertEqual(out["final_url"], self.base + "/page")

    def test_redirect_loop_refuses(self):
        with self.assertRaises(FetchError) as ctx:
            fetch_url(self.base + "/loop", snapshot_dir=self.dir,
                      allow_private=True, max_redirects=2)
        self.assertEqual(ctx.exception.code, "FETCH_TOO_MANY_REDIRECTS")

    def test_loopback_is_refused_by_default(self):
        with self.assertRaises(FetchError) as ctx:
            fetch_url(self.base + "/page", snapshot_dir=self.dir)
        self.assertEqual(ctx.exception.code, "FETCH_REFUSED")

    def test_every_redirect_hop_is_rechecked(self):
        seen = []
        real = resolve_and_check

        def recording(host, port, allow_private=False):
            seen.append(host)
            return real(host, port, allow_private=allow_private)

        with mock.patch("mini_aurion.fetcher.resolve_and_check",
                        side_effect=recording):
            fetch_url(self.base + "/redir", snapshot_dir=self.dir,
                      allow_private=True)
        self.assertEqual(seen, ["127.0.0.1", "127.0.0.1"])

    def test_one_resolution_per_hop(self):
        real = socket.getaddrinfo
        calls = []

        def counting(*args, **kwargs):
            calls.append(args[:2])
            return real(*args, **kwargs)

        with mock.patch("socket.getaddrinfo", side_effect=counting):
            fetch_url(self.base + "/page", snapshot_dir=self.dir,
                      allow_private=True)
        self.assertEqual(len(calls), 1)

    def test_tls_goes_to_the_hostname_over_tcp_to_the_ip(self):
        created = []
        wrapped = []

        class FakeSock:
            pass

        class FakeContext:
            def wrap_socket(self, sock, server_hostname=None):
                wrapped.append(server_hostname)
                return sock

        def fake_create(address, port, timeout=None):
            created.append((address, port))
            return FakeSock()

        conn = _PinnedHTTPSConnection("93.184.216.34", 443, "example.com",
                                      timeout=5, context=FakeContext())
        with mock.patch("mini_aurion.fetcher._tcp_connect",
                        side_effect=fake_create):
            conn.connect()
        self.assertEqual(created, [("93.184.216.34", 443)])
        self.assertEqual(wrapped, ["example.com"])


class RegistryTests(unittest.TestCase):
    def test_tool_is_registered_as_read_with_a_url_param(self):
        registry = ToolRegistry()
        register_fetch_tool(registry, "/tmp/nowhere")
        defs = {d["name"]: d for d in registry.definitions()}
        self.assertIn("fetch_url", defs)
        self.assertEqual(
            defs["fetch_url"]["parameters"]["required"], ["url"])
        self.assertEqual(registry.get("fetch_url")["class"], "READ")

    def test_bare_call_needs_a_url(self):
        backend = FetchBackend("/tmp/nowhere")
        with self.assertRaises(FetchError):
            backend.call("fetch_url", {})
        with self.assertRaises(KeyError):
            backend.call("other", {"url": "x"})

    def test_settle_receipt_carries_the_snapshot_hash(self):
        tmp = tempfile.TemporaryDirectory(prefix="ma-fetchreg-")
        self.addCleanup(tmp.cleanup)
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Pages)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 5)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        base = f"http://127.0.0.1:{server.server_address[1]}"
        receipts = ReceiptStore(os.path.join(tmp.name, "r.jsonl"))
        ledger = Ledger(os.path.join(tmp.name, "l.sqlite3"), **CAPS)
        self.addCleanup(ledger.close)
        registry = ToolRegistry()
        register_fetch_tool(registry, tmp.name, allow_private=True)
        tools = ToolWrapper(registry, receipts=receipts, ledger=ledger,
                            stage="experimental", task_id="t1",
                            agent_id="spec")
        out = tools.call("fetch_url", {"url": base + "/text"})
        self.assertIn("plain words", out["result"]["text"])
        settles = [r for r in receipts.read_all()
                   if r["action"] == "settle"]
        self.assertEqual(len(settles), 1)
        self.assertEqual(settles[0]["detail"]["snapshot"],
                         out["result"]["snapshot"])


if __name__ == "__main__":
    unittest.main()
