"""Reviewer findings on #70/#71 (the Monitor), kept as expected-failure
tests.

Each test fails today. When its fix lands, unittest reports "unexpected
success" and CI fails until that test's marker is deleted; the test then
stays as a regression guard, per the review-findings contract.

The Monitor binds loopback only, which stops other machines. It does not
stop the operator's own browser: any web page the operator has open can
send a form POST to http://127.0.0.1:<port>/ (no CORS preflight for a
plain form), and a DNS-rebinding page can read responses. The writes are
the operator's controls: spend caps and approvals (OPERATIONS sections 2
and 4), so they must only be reachable from the Monitor's own pages.

No network beyond 127.0.0.1, no API key, $0.
"""
from __future__ import annotations

import http.client
import sqlite3
import sys
import tempfile
import threading
import unittest
import urllib.parse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mini_aurion.ledger import Ledger  # noqa: E402
from mini_aurion.monitor import serve  # noqa: E402

CAP = 1000


class MonitorAcceptsOnlyItsOwnPages(unittest.TestCase):

    def setUp(self):
        # Cleanups run last-in first-out: the temp dir goes last, after
        # the server and every SQLite handle close (Windows refuses to
        # delete open files).
        tmp = tempfile.TemporaryDirectory(prefix="ma-k-")
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name)
        self.root = self.base / "state"
        self.root.mkdir()
        self.ledger_path = self.root / "ledger.sqlite3"
        Ledger(self.ledger_path, task_cap_nano=CAP, daily_cap_nano=CAP,
               global_cap_nano=CAP).close()
        self.server = serve(self.root, 0)
        self.port = self.server.server_port
        thread = threading.Thread(target=self.server.serve_forever,
                                  daemon=True)
        thread.start()
        self.addCleanup(thread.join, 5)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def _request(self, method, path, form=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port,
                                          timeout=10)
        self.addCleanup(conn.close)
        body = urllib.parse.urlencode(form) if form is not None else None
        sent = {"Content-Type": "application/x-www-form-urlencoded"}
        sent.update(headers or {})
        conn.request(method, path, body, sent)
        response = conn.getresponse()
        response.read()
        return response.status

    def _global_cap(self) -> int:
        ledger = Ledger.open(self.ledger_path)
        try:
            return ledger.caps().get("global")
        finally:
            ledger.close()

    def test_K1_a_foreign_page_cannot_change_caps(self):
        """A hidden form on any web page posts to /caps with the
        browser's Origin and Referer naming that page. Today the Monitor
        applies it: the probe raised the global cap from 1,000 nanoUSD to
        999,999,999,999,999 (about $1M). /approve and /kill share the
        same unchecked handler, so the same page can approve a gated
        action.

        Fix: refuse POSTs whose Origin (or, if absent, Referer) is not
        the Monitor's own http://127.0.0.1:<port> or
        http://localhost:<port>, and require a per-process random token
        that the Monitor's own forms carry. Either alone closes this
        test; both together are the usual pair."""
        status = self._request(
            "POST", "/caps",
            {"ledger": str(self.ledger_path), "global": "999999999999999"},
            {"Origin": "http://evil.example",
             "Referer": "http://evil.example/page"})
        self.assertNotIn(status, (200, 303), "foreign POST was accepted")
        self.assertEqual(self._global_cap(), CAP,
                         "a foreign page changed the spend cap")

    def test_K2_a_rebinding_host_is_refused(self):
        """DNS rebinding: a page on rebind.evil.example re-resolves its
        own name to 127.0.0.1, then reads the Monitor same-origin
        (receipts, task contents) and posts to it. Browsers still send
        the attacker's name in the Host header, which is the defence.

        Fix: answer only Host values 127.0.0.1:<port> and
        localhost:<port>; anything else gets 400 or 403."""
        status = self._request("GET", "/", None,
                               {"Host": "rebind.evil.example"})
        self.assertGreaterEqual(status, 400,
                                "served the Monitor to a foreign Host")

    def test_K3_caps_edits_stay_inside_the_watched_root(self):
        """/caps takes the ledger path from the form and Ledger.open
        accepts any existing file, so the request created `reservations`
        and `caps` tables inside an unrelated SQLite file outside
        --root, then wrote caps into it.

        Fix: resolve the path and refuse anything outside --root (or
        accept only ledgers listed by the Monitor's own task scan)."""
        outside = self.base / "someone_elses.sqlite3"
        db = sqlite3.connect(outside)
        db.execute("CREATE TABLE notes (x TEXT)")
        db.commit()
        db.close()
        self._request("POST", "/caps", {"ledger": str(outside), "task": "5"},
                      {"Origin": f"http://127.0.0.1:{self.port}"})
        db = sqlite3.connect(outside)
        try:
            tables = sorted(row[0] for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"))
        finally:
            db.close()
        self.assertEqual(tables, ["notes"],
                         "the Monitor wrote into a file outside its root")


if __name__ == "__main__":
    unittest.main()
