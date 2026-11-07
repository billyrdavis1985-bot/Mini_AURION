import os
import tempfile
import unittest

from mini_aurion.critic import Critic
from mini_aurion.fixture import FakeModel, FakeTools
from mini_aurion.ledger import Ledger
from mini_aurion.memory import Memory
from mini_aurion.orchestrator import Orchestrator
from mini_aurion.receipts import ReceiptStore
from mini_aurion.specialist import Specialist
from mini_aurion.tools import ToolRegistry, ToolWrapper

CAPS = {"task_cap_nano": 100_000, "daily_cap_nano": 500_000,
        "global_cap_nano": 1_000_000}
GOOD_EVIDENCE = [{"kind": "file", "ref": "a.txt"}]


def plan(*subtasks):
    return {"text": "plan", "subtasks": list(subtasks)}


def answer(text, evidence=None):
    return {"text": text, "evidence": evidence or GOOD_EVIDENCE,
            "confidence": 0.9}


class OrchestratorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="ma-orc-")
        self.root = os.path.join(self.tmp.name, "corpus")
        os.mkdir(self.root)
        with open(os.path.join(self.root, "a.txt"), "w") as fh:
            fh.write("real")
        self.receipts = ReceiptStore(os.path.join(self.tmp.name, "r.jsonl"))
        self.ledger = Ledger(os.path.join(self.tmp.name, "l.sqlite3"), **CAPS)
        self.mem = Memory(os.path.join(self.tmp.name, "m.sqlite3"),
                          file_root=self.root)

    def tearDown(self):
        self.ledger.close()
        self.mem.close()
        self.tmp.cleanup()

    def build(self, plan_reply, spec_scripts, synth_text="done",
              budget=10, synth_evidence=None):
        synth = {"text": synth_text,
                 "evidence": synth_evidence
                 if synth_evidence is not None else GOOD_EVIDENCE}
        orc_model = FakeModel([plan_reply, synth])
        specialists = {}
        for name, script in spec_scripts.items():
            tools = ToolWrapper(ToolRegistry(), receipts=self.receipts,
                                ledger=self.ledger, stage="trusted",
                                task_id="t1", agent_id=name)
            specialists[name] = Specialist(name, FakeModel(script), tools)
        orc = Orchestrator("orc", orc_model, specialists,
                           Critic("critic", self.mem), receipts=self.receipts,
                           ledger=self.ledger, message_budget=budget)
        return orc

    def test_happy_path_completes_with_combined_evidence(self):
        orc = self.build(
            plan({"id": "s1", "brief": "b1", "specialist": "alpha"},
                 {"id": "s2", "brief": "b2", "specialist": "beta"}),
            {"alpha": [answer("A")], "beta": [answer("B")]},
            synth_text="A and B")
        result = orc.run("t1", "question?")
        self.assertEqual(result["status"], "complete")
        self.assertEqual(result["text"], "A and B")
        self.assertEqual(result["evidence"], GOOD_EVIDENCE)
        self.assertEqual(self.receipts.verify(), self.receipts.verify())
        actions = [r["action"] for r in self.receipts.read_all()]
        self.assertIn("task_start", actions)
        self.assertIn("task_end", actions)
        self.assertEqual(actions.count("delegate"), 2)

    def test_failed_answer_gets_one_retry_then_accepts(self):
        orc = self.build(
            plan({"id": "s1", "brief": "b1", "specialist": "alpha"}),
            {"alpha": [answer("bad",
                              [{"kind": "file", "ref": "ghost.txt"}]),
                       answer("good")]})
        result = orc.run("t1", "q?")
        self.assertEqual(result["status"], "complete")
        self.assertEqual(result["findings"][0]["status"], "accepted")
        self.assertEqual(result["findings"][0]["message"].claim, "good")

    def test_twice_failed_answer_escalates_to_partial(self):
        orc = self.build(
            plan({"id": "s1", "brief": "b1", "specialist": "alpha"},
                 {"id": "s2", "brief": "b2", "specialist": "alpha"}),
            {"alpha": [answer("bad",
                              [{"kind": "file", "ref": "ghost.txt"}]),
                       answer("bad2",
                              [{"kind": "file", "ref": "ghost.txt"}]),
                       answer("good")]})
        result = orc.run("t1", "q?")
        self.assertEqual(result["status"], "partial")
        statuses = [f["status"] for f in result["findings"]]
        self.assertEqual(statuses, ["escalated", "accepted"])

    def test_specialist_escalation_yields_partial(self):
        orc = self.build(
            plan({"id": "s1", "brief": "b1", "specialist": "alpha"},
                 {"id": "s2", "brief": "b2", "specialist": "alpha"}),
            {"alpha": [{"text": "CANNOT_VERIFY"}, answer("good")]})
        result = orc.run("t1", "q?")
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["findings"][0]["status"], "escalated")

    def test_message_budget_leaves_rest_unattempted(self):
        orc = self.build(
            plan({"id": "s1", "brief": "b1", "specialist": "alpha"},
                 {"id": "s2", "brief": "b2", "specialist": "alpha"}),
            {"alpha": [answer("good")]},
            budget=1)
        result = orc.run("t1", "q?")
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["unattempted"], ["s2"])

    def test_nothing_accepted_escalates(self):
        orc = self.build(
            plan({"id": "s1", "brief": "b1", "specialist": "alpha"}),
            {"alpha": [{"text": "CANNOT_VERIFY"}]})
        result = orc.run("t1", "q?")
        self.assertEqual(result["status"], "escalated")
        self.assertIn("CANNOT_VERIFY", result["text"])

    def test_unevidenced_synthesis_drops_to_partial(self):
        orc = self.build(
            plan({"id": "s1", "brief": "b1", "specialist": "alpha"}),
            {"alpha": [answer("good")]},
            synth_text="good plus extra claims", synth_evidence=[])
        result = orc.run("t1", "q?")
        self.assertEqual(result["status"], "partial")
        self.assertTrue(result["text"].startswith("PARTIAL:"))
        self.assertIn("good", result["text"])
        self.assertNotIn("extra claims", result["text"])


if __name__ == "__main__":
    unittest.main()
