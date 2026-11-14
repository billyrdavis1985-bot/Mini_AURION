import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from mini_aurion.ledger import Ledger, LedgerError  # noqa: E402
from mini_aurion.monitor import (_pid_alive, decide_approval, find_tasks,  # noqa: E402
                                 kill_task, pending_approvals, serve,
                                 task_status)
from mini_aurion.receipts import ReceiptStore  # noqa: E402

CAPS = {"task_cap_nano": 10_000_000_000, "daily_cap_nano": 50_000_000_000,
        "global_cap_nano": 100_000_000_000}


def _opener():
    # Loopback direct: never via any configured HTTP proxy.
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _http_get(url):
    with _opener().open(url, timeout=10) as resp:
        return resp.status, resp.read()


def _http_post(url, form):
    data = urllib.parse.urlencode(form).encode()
    req = urllib.request.Request(url, data=data, method="POST")
    try:
        with _opener().open(req, timeout=10) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


class MonitorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="ma-monitor-")
        self.root = os.path.join(self.tmp.name, "state")
        os.makedirs(self.root)
        self.task_dir = os.path.join(self.root, "task-a")
        os.makedirs(self.task_dir)
        self.receipts = os.path.join(self.task_dir, "receipts.jsonl")
        store = ReceiptStore(self.receipts)
        store.append(agent_id="orc", task_id="task-a", action="task_start")
        self.ledger_path = os.path.join(self.tmp.name, "eval-l.db")
        self.ledger = Ledger(self.ledger_path, **CAPS)
        self.proc = {
            "task_id": "task-a", "pid": os.getpid(),
            "started": "2026-01-01T00:00:00Z",
            "receipts": self.receipts, "ledger": self.ledger_path,
            "routing": "sha256:abc",
            "seats": {"specialist": {"provider": "meta",
                                     "model": "muse-spark-1.1"}},
            "status": "running", "end_status": "",
        }
        self._write_proc()
        self.server = None

    def tearDown(self):
        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()
        self.ledger.close()
        self.tmp.cleanup()

    def _write_proc(self):
        with open(os.path.join(self.task_dir, "proc.json"), "w") as fh:
            fh.write(json.dumps(self.proc, indent=1))

    def _serve(self):
        self.server = serve(self.root, 0)
        port = self.server.server_port
        worker = threading.Thread(target=self.server.serve_forever,
                                  daemon=True)
        worker.start()
        return f"http://127.0.0.1:{port}"

    def test_live_view_lists_the_task_and_its_spend(self):
        rid = self.ledger.reserve("task-a", 4_000)
        self.ledger.settle(rid, 3_000)
        base = self._serve()
        _, page_bytes = _http_get(base + "/")
        page = page_bytes.decode()
        self.assertIn("task-a", page)
        self.assertIn("$0.0000", page)  # 3k nano rounds to zero at 4dp
        self.assertIn("running", page)
        self.assertIn("specialist=meta/muse-spark-1.1", page)
        self.assertIn("/caps", page)

    def test_receipts_view_shows_newest_first(self):
        store = ReceiptStore(self.receipts)
        store.append(agent_id="spec", task_id="task-a", action="tool_call",
                     tool="read_file")
        base = self._serve()
        _, page_bytes = _http_get(base + "/receipts?task=task-a")
        page = page_bytes.decode()
        self.assertLess(page.index("tool_call"), page.index("task_start"))

    def test_experiments_view_shows_routing_and_tiers(self):
        base = self._serve()
        _, page_bytes = _http_get(base + "/experiments")
        page = page_bytes.decode()
        self.assertIn("sha256:abc", page)

    def test_server_binds_loopback_only(self):
        self._serve()
        self.assertEqual(self.server.server_address[0], "127.0.0.1")

    def test_caps_edit_lands_in_the_ledger(self):
        base = self._serve()
        code, _ = _http_post(base + "/caps",
                             {"ledger": self.ledger_path, "task": "500"})
        self.assertEqual(code, 200)
        check = Ledger.open(self.ledger_path)
        try:
            self.assertEqual(check.caps()["task"], 500)
        finally:
            check.close()

    def test_approvals_round_trip_through_the_page(self):
        queue = os.path.join(self.task_dir, "approvals.jsonl")
        with open(queue, "w") as fh:
            fh.write(json.dumps({"id": "apr_1", "task_id": "task-a",
                                 "tool": "w", "args": {},
                                 "action_class": "WRITE", "reason": "r",
                                 "status": "pending", "ts": "t"}) + "\n")
        base = self._serve()
        _, page_bytes = _http_get(base + "/approvals")
        page = page_bytes.decode()
        self.assertIn("apr_1", page)
        code, _ = _http_post(base + "/approve",
                             {"id": "apr_1", "decision": "denied"})
        self.assertEqual(code, 200)
        self.assertEqual(pending_approvals(self.root), [])

    def test_decide_refuses_an_unknown_request(self):
        with self.assertRaises(KeyError):
            decide_approval(self.root, "apr_nope", "approved")

    def test_kill_stops_the_process_and_refunds_inflight(self):
        sleeper = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)"])
        try:
            rid = self.ledger.reserve("task-a", 4_000)
            self.assertEqual(self.ledger.spend("task-a")["inflight_nano"],
                             4_000)
            self.proc["pid"] = sleeper.pid
            self._write_proc()
            out = kill_task(self.root, "task-a")
            self.assertEqual(out["refund"],
                             {"count": 1, "released_nano": 4_000})
            sleeper.wait(timeout=15)
            self.assertFalse(_pid_alive(sleeper.pid))
            rows = ReceiptStore(self.receipts).read_all()
            kills = [r for r in rows if r["action"] == "kill"]
            self.assertEqual(len(kills), 1)
            self.assertEqual(kills[0]["agent_id"], "monitor")
            with self.assertRaises(LedgerError):
                self.ledger.settle(rid, 1)
            proc = next(p for p in find_tasks(self.root)
                        if p["task_id"] == "task-a")
            self.assertEqual(proc["status"], "killed")
        finally:
            if sleeper.poll() is None:
                sleeper.kill()
                sleeper.wait()

    def test_kill_refuses_a_task_that_already_ended(self):
        self.proc["status"] = "ended"
        self.proc["end_status"] = "complete"
        self._write_proc()
        with self.assertRaises(KeyError):
            kill_task(self.root, "task-a")

    def test_dead_pid_shows_died_not_running(self):
        self.proc["pid"] = 2**30 + 7  # no such process
        self._write_proc()
        proc = next(p for p in find_tasks(self.root)
                    if p["task_id"] == "task-a")
        self.assertEqual(task_status(proc), "died")


    def test_pid_probe_reports_self_alive_and_bogus_dead(self):
        self.assertTrue(_pid_alive(os.getpid()))
        self.assertFalse(_pid_alive(2**30 + 7))
        self.assertFalse(_pid_alive(0))
        self.assertFalse(_pid_alive("not-a-pid"))


if __name__ == "__main__":
    unittest.main()
