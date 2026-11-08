import os
import tempfile
import unittest

from mini_aurion.fixture import FakeTools
from mini_aurion.ledger import Ledger, LedgerError
from mini_aurion.receipts import ReceiptStore
from mini_aurion.tools import (NeedsApproval, ToolDenied, ToolFailed,
                               ToolRegistry, ToolWrapper)

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

    def wrap(self, entries, stage="trusted"):
        registry = ToolRegistry()
        for name, action_class, estimate, backend in entries:
            registry.register(name, action_class, estimate, backend)
        return ToolWrapper(registry, receipts=self.receipts,
                           ledger=self.ledger, stage=stage, task_id="t1",
                           agent_id="spec")

    def test_read_call_leaves_begin_settle_receipts(self):
        out = self.wrap([("r", "READ", 0, FakeTools({"r": "bytes"}))]).call(
            "r", {})
        self.assertEqual(out["result"], "bytes")
        self.assertEqual(out["cost_nano"], 0)
        receipts = self.receipts.read_all()
        self.assertEqual([r["action"] for r in receipts],
                         ["tool_call", "settle"])
        self.assertEqual(receipts[0]["outcome"], "pending")
        self.assertEqual(receipts[1]["outcome"], "ok")
        self.assertEqual(self.receipts.verify(), 2)

    def test_repeat_read_is_a_pointer_not_the_content_again(self):
        backend = CostlyBackend(300)
        tools = self.wrap([("r", "READ", 500, backend)])
        first = tools.call("r", {})
        second = tools.call("r", {})
        self.assertEqual(len(backend.calls), 1)
        self.assertTrue(second["duplicate"])
        self.assertFalse(first["duplicate"])
        self.assertIn("[duplicate:", second["result"])
        self.assertNotIn("done", second["result"])
        self.assertEqual(second["cost_nano"], 0)
        self.assertEqual(self.ledger.spend("t1")["settled_nano"], 300)
        cached = [r for r in self.receipts.read_all()
                  if r["detail"].get("cached")]
        self.assertEqual(len(cached), 1)

    def test_backend_cost_settles_through_ledger(self):
        out = self.wrap([("w", "WRITE", 500, CostlyBackend(300))]).call(
            "w", {})
        self.assertEqual(out["cost_nano"], 300)
        self.assertEqual(self.ledger.spend("t1")["settled_nano"], 300)
        self.assertEqual(self.ledger.totals()["inflight_nano"], 0)

    def test_experimental_write_is_denied_and_receipted(self):
        with self.assertRaises(ToolDenied) as ctx:
            self.wrap([("w", "WRITE", 0, FakeTools())],
                      stage="experimental").call("w", {})
        self.assertEqual(ctx.exception.code, "POLICY_DENIED")
        receipts = self.receipts.read_all()
        self.assertEqual(len(receipts), 1)
        self.assertEqual(receipts[0]["action"], "denied")
        self.assertEqual(self.ledger.totals()["settled_nano"], 0)

    def test_gated_write_needs_approval_first(self):
        wrapper = self.wrap([("w", "WRITE", 0, FakeTools({"w": "v"}))],
                            stage="gated")
        with self.assertRaises(NeedsApproval) as req:
            wrapper.call("w", {})
        self.assertEqual(req.exception.request["action_class"], "WRITE")
        receipts = self.receipts.read_all()
        self.assertEqual(receipts[0]["action"], "approval_requested")
        out = wrapper.call("w", {}, approved=True)
        self.assertEqual(out["result"], "v")

    def test_cap_breach_is_refused_before_any_call(self):
        backend = CostlyBackend(300)
        wrapper = self.wrap([("w", "WRITE", 10_000, backend)])
        wrapper.call("w", {"n": 2})
        # First call settled to actual 300; a second 10,000 breaches the cap.
        with self.assertRaises(LedgerError) as ctx:
            wrapper.call("w", {})
        self.assertEqual(ctx.exception.code, "CAP_EXCEEDED")
        self.assertEqual(len(backend.calls), 1)
        denials = [r for r in self.receipts.read_all()
                   if r["action"] == "denied"]
        self.assertEqual(len(denials), 1)
        self.assertIn("CAP_EXCEEDED", denials[0]["detail"]["reason"])

    def test_backend_failure_settles_failed_and_reraises(self):
        with self.assertRaises(ToolFailed) as ctx:
            self.wrap([("f", "READ", 0, FailingBackend())]).call("f", {})
        self.assertEqual(ctx.exception.seq, 1)
        receipts = self.receipts.read_all()
        self.assertEqual(receipts[1]["outcome"], "failed")
        self.assertEqual(self.receipts.verify(), 2)

    def test_silent_backend_settles_at_worst_case(self):
        class Silent:
            def call(self, name, args):
                return "r"
        out = self.wrap([("s", "READ", 5_000, Silent())]).call("s", {})
        self.assertEqual(out["cost_nano"], 5_000)
        self.assertEqual(self.ledger.spend("t1")["settled_nano"], 5_000)

    def test_silent_free_tool_settles_zero(self):
        class Silent:
            def call(self, name, args):
                return "r"
        out = self.wrap([("s", "READ", 0, Silent())]).call("s", {})
        self.assertEqual(out["cost_nano"], 0)
        self.assertEqual(self.ledger.totals()["settled_nano"], 0)

    def test_unknown_tool_is_refused_and_receipted(self):
        wrapper = self.wrap([("r", "READ", 0, FakeTools({"r": "v"}))])
        with self.assertRaises(ToolDenied) as ctx:
            wrapper.call("ghost", {})
        self.assertEqual(ctx.exception.code, "UNKNOWN_TOOL")
        receipts = self.receipts.read_all()
        self.assertEqual(receipts[0]["action"], "denied")

    def test_definitions_expose_registered_tools(self):
        registry = ToolRegistry()
        registry.register("r", "READ", 0, FakeTools({"r": "v"}),
                          description="reads", parameters={"type": "object"})
        self.assertEqual(registry.definitions(),
                         [{"name": "r", "description": "reads",
                           "parameters": {"type": "object"}}])
        self.assertEqual(ToolRegistry().definitions(), [])

    def test_bad_tool_definition_is_refused(self):
        registry = ToolRegistry()
        with self.assertRaises(ToolDenied):
            registry.register("t", "DELETE", 0, FakeTools())
        with self.assertRaises(ToolDenied):
            registry.register("t", "READ", -1, FakeTools())
        with self.assertRaises(ToolDenied):
            registry.register("t", "READ", 0, object())
        registry.register("t", "READ", 0, FakeTools())
        with self.assertRaises(ToolDenied):
            registry.register("t", "READ", 0, FakeTools())


if __name__ == "__main__":
    unittest.main()
