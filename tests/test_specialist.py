import os
import tempfile
import unittest

from mini_aurion.fixture import ESCALATE_TEXT, FakeModel, FakeTools
from mini_aurion.ledger import Ledger
from mini_aurion.receipts import ReceiptStore
from mini_aurion.specialist import Specialist
from mini_aurion.tools import ToolRegistry, ToolWrapper

CAPS = {"task_cap_nano": 10_000, "daily_cap_nano": 50_000,
        "global_cap_nano": 100_000}


class SpecialistTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="ma-spec-")
        self.receipts = ReceiptStore(os.path.join(self.tmp.name, "r.jsonl"))
        self.ledger = Ledger(os.path.join(self.tmp.name, "l.sqlite3"), **CAPS)

    def tearDown(self):
        self.ledger.close()
        self.tmp.cleanup()

    def investigate_with(self, script, tools_dict=None, classes=None,
                         **kw):
        backend = FakeTools(tools_dict or {})
        registry = ToolRegistry()
        for name in (tools_dict or {}):
            action_class, estimate = (classes or {}).get(name, ("READ", 0))
            registry.register(name, action_class, estimate, backend)
        tools = ToolWrapper(registry, receipts=self.receipts,
                            ledger=self.ledger, stage="trusted", task_id="t1",
                            agent_id="spec")
        brief = kw.pop("brief", "brief")
        estimate = kw.pop("model_estimate_nano", 0)
        spec = Specialist("spec", FakeModel(script), tools,
                          model_estimate_nano=estimate, **kw)
        return spec.investigate("t1", brief)

    def test_tool_loop_builds_evidenced_message(self):
        msg = self.investigate_with(
            [{"text": "", "tool_calls": [{"tool": "read", "args": {}}]},
             {"text": "found it",
              "evidence": [{"kind": "file", "ref": "a.txt"}],
              "confidence": 0.9}],
            {"read": "bytes"})
        self.assertFalse(msg.escalated)
        self.assertEqual(msg.claim, "found it")
        self.assertEqual(msg.confidence, 0.9)
        self.assertEqual(len(msg.evidence), 1)
        kinds = [r["action"] for r in self.receipts.read_all()]
        self.assertIn("model_call", kinds)
        self.assertIn("tool_call", kinds)

    def test_model_saying_cannot_verify_escalates(self):
        msg = self.investigate_with([{"text": ESCALATE_TEXT}])
        self.assertTrue(msg.escalated)
        self.assertIn(ESCALATE_TEXT, msg.claim)

    def test_tool_budget_exhaustion_escalates(self):
        script = [{"text": "", "tool_calls": [{"tool": "r", "args": {}}]}] * 6
        msg = self.investigate_with(script, {"r": "v"}, max_tool_calls=2)
        self.assertTrue(msg.escalated)
        self.assertIn("tool budget exhausted", msg.claim)

    def test_model_budget_exhaustion_escalates(self):
        script = [{"text": "", "tool_calls": [{"tool": "r", "args": {}}]}] * 6
        msg = self.investigate_with(script, {"r": "v"}, max_model_calls=2,
                       max_tool_calls=10)
        self.assertTrue(msg.escalated)
        self.assertIn("model budget exhausted", msg.claim)

    def test_tool_error_is_fed_back_not_fatal(self):
        msg = self.investigate_with(
            [{"text": "", "tool_calls": [{"tool": "missing", "args": {}}]},
             {"text": ESCALATE_TEXT}],
            {})
        self.assertTrue(msg.escalated)

    def test_approval_need_escalates_with_reason(self):
        registry = ToolRegistry()
        registry.register("w", "WRITE", 0, FakeTools({"w": "v"}))
        tools = ToolWrapper(registry, receipts=self.receipts,
                            ledger=self.ledger, stage="gated", task_id="t1",
                            agent_id="spec")
        spec = Specialist("spec", FakeModel(
            [{"text": "", "tool_calls": [{"tool": "w", "args": {}}]}]), tools,
            model_estimate_nano=0)
        msg = spec.investigate("t1", "brief")
        self.assertTrue(msg.escalated)
        self.assertIn("needs approval", msg.claim)

    def test_model_spend_is_reserved_and_settled(self):
        model = FakeModel([{"text": "done",
                            "evidence": [{"kind": "file", "ref": "a"}]}])
        model.last_cost_nano = 400
        tools = ToolWrapper(ToolRegistry(), receipts=self.receipts,
                            ledger=self.ledger, stage="trusted", task_id="t1",
                            agent_id="spec")
        spec = Specialist("spec", model, tools, model_estimate_nano=500)
        msg = spec.investigate("t1", "brief")
        self.assertFalse(msg.escalated)
        self.assertEqual(msg.cost_nano, 400)
        self.assertEqual(self.ledger.spend("t1")["settled_nano"], 400)

    def test_refused_model_call_escalates(self):
        tools = ToolWrapper(ToolRegistry(), receipts=self.receipts,
                            ledger=self.ledger, stage="trusted", task_id="t1",
                            agent_id="spec")
        spec = Specialist("spec", FakeModel([{"text": "done"}]), tools,
                          model_estimate_nano=99_999)
        msg = spec.investigate("t1", "brief")
        self.assertTrue(msg.escalated)
        self.assertIn("spend cap refused", msg.claim)


if __name__ == "__main__":
    unittest.main()
