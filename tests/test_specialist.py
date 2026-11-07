import os
import tempfile
import unittest

from mini_aurion.fixture import ESCALATE_TEXT, FakeModel, FakeTools
from mini_aurion.ledger import Ledger
from mini_aurion.receipts import ReceiptStore
from mini_aurion.specialist import Specialist
from mini_aurion.tools import ToolWrapper

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

    def investigate_with(self, script, tools_dict=None, **kw):
        tools = ToolWrapper(FakeTools(tools_dict or {}),
                            receipts=self.receipts, ledger=self.ledger,
                            stage="trusted", task_id="t1", agent_id="spec")
        spec = Specialist("spec", FakeModel(script), tools, **kw)
        return spec.investigate("t1", kw.pop("brief", "brief"))

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
        tools = ToolWrapper(FakeTools({"w": "v"}),
                            receipts=self.receipts, ledger=self.ledger,
                            stage="gated", task_id="t1", agent_id="spec")
        spec = Specialist("spec", FakeModel(
            [{"text": "", "tool_calls": [{"tool": "w", "args": {},
                                          "class": "WRITE"}]}]), tools)
        msg = spec.investigate("t1", "brief")
        self.assertTrue(msg.escalated)
        self.assertIn("needs approval", msg.claim)


if __name__ == "__main__":
    unittest.main()
