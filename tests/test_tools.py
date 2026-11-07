import os
import tempfile
import unittest

from mini_aurion.fixture import FakeTools
from mini_aurion.ledger import Ledger, LedgerError
from mini_aurion.receipts import ReceiptStore
from mini_aurion.tools import NeedsApproval, ToolDenied, ToolFailed, ToolWrapper

CAPS = {"task_cap_nano": 10_000, "daily_cap_nano": 50_000,
        "global_cap_nano": 100_000}


class CostlyBackend:
    """Backend that reports a real cost per call."""

    def __init__(self, cost=300):
        self.last_cost_nano = 0
        self._cost = cost
        self.calls = []

    def call(self, name, args):
        self.calls.append((name, args))
        self.last_cost_nano = self._cost
        return "done"


class FailingBackend:
    last_cost_nano = 100

    def call(self, name, args):
        raise RuntimeError("boom")


class ToolWrapperTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="ma-tools-")
        self.receipts = ReceiptStore(os.path.join(self.tmp.name, "r.jsonl"))
        self.ledger = Ledger(os.path.join(self.tmp.name, "l.sqlite3"), **CAPS)

    def tearDown(self):
        self.ledger.close()
        self.tmp.cleanup()

    def wrap(self, backend, stage="trusted"):
        return ToolWrapper(backend, receipts=self.receipts, ledger=self.ledger,
                           stage=stage, task_id="t1", agent_id="spec")

    def test_read_call_leaves_begin_settle_receipts(self):
        out = self.wrap(FakeTools({"r": "bytes"})).call("r", {}, "READ")
        self.assertEqual(out["result"], "bytes")
        self.assertEqual(out["cost_nano"], 0)
        receipts = self.receipts.read_all()
        self.assertEqual([r["action"] for r in receipts],
                         ["tool_call", "settle"])
        self.assertEqual(receipts[0]["outcome"], "pending")
        self.assertEqual(receipts[1]["outcome"], "ok")
        self.assertEqual(self.receipts.verify(), 2)

    def test_backend_cost_settles_through_ledger(self):
        out = self.wrap(CostlyBackend(300)).call("w", {}, "WRITE",
                                                 estimate_nano=500)
        self.assertEqual(out["cost_nano"], 300)
        self.assertEqual(self.ledger.spend("t1")["settled_nano"], 300)
        self.assertEqual(self.ledger.totals()["inflight_nano"], 0)

    def test_experimental_write_is_denied_and_receipted(self):
        with self.assertRaises(ToolDenied) as ctx:
            self.wrap(FakeTools(), stage="experimental").call("w", {}, "WRITE")
        self.assertEqual(ctx.exception.code, "POLICY_DENIED")
        receipts = self.receipts.read_all()
        self.assertEqual(len(receipts), 1)
        self.assertEqual(receipts[0]["action"], "denied")
        self.assertEqual(self.ledger.totals()["settled_nano"], 0)

    def test_gated_write_needs_approval_first(self):
        wrapper = self.wrap(FakeTools({"w": "v"}), stage="gated")
        with self.assertRaises(NeedsApproval) as req:
            wrapper.call("w", {}, "WRITE")
        self.assertEqual(req.exception.request["action_class"], "WRITE")
        receipts = self.receipts.read_all()
        self.assertEqual(receipts[0]["action"], "approval_requested")
        out = wrapper.call("w", {}, "WRITE", approved=True)
        self.assertEqual(out["result"], "v")

    def test_cap_breach_is_refused_before_any_call(self):
        backend = CostlyBackend(300)
        wrapper = self.wrap(backend)
        wrapper.call("w", {}, "WRITE", estimate_nano=10_000)
        # First call settled to actual 300; 9,701 more would breach 10,000.
        with self.assertRaises(LedgerError) as ctx:
            wrapper.call("w", {}, "WRITE", estimate_nano=9_701)
        self.assertEqual(ctx.exception.code, "CAP_EXCEEDED")
        self.assertEqual(len(backend.calls), 1)
        denials = [r for r in self.receipts.read_all()
                   if r["action"] == "denied"]
        self.assertEqual(len(denials), 1)
        self.assertIn("CAP_EXCEEDED", denials[0]["detail"]["reason"])

    def test_backend_failure_settles_failed_and_reraises(self):
        with self.assertRaises(ToolFailed) as ctx:
            self.wrap(FailingBackend()).call("f", {}, "READ")
        self.assertEqual(ctx.exception.seq, 1)
        receipts = self.receipts.read_all()
        self.assertEqual(receipts[1]["outcome"], "failed")
        self.assertEqual(self.receipts.verify(), 2)


if __name__ == "__main__":
    unittest.main()
