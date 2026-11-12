import os
import tempfile
import unittest

from mini_aurion.contract import Criterion, TaskContract
from mini_aurion.critic import Critic
from mini_aurion.fixture import FakeModel, FakeTools
from mini_aurion.ledger import Ledger
from mini_aurion.memory import Memory
from mini_aurion.messages import AgentMessage
from mini_aurion.orchestrator import Orchestrator
from mini_aurion.receipts import ReceiptStore
from mini_aurion.specialist import Specialist
from mini_aurion.tools import ToolRegistry, ToolWrapper

CAPS = {"task_cap_nano": 100_000, "daily_cap_nano": 500_000,
        "global_cap_nano": 1_000_000}
GOOD_EVIDENCE = [{"kind": "file", "ref": "a.txt:1",
 "quote": "real file content"}]


def plan(*subtasks):
    return {"text": "plan", "subtasks": list(subtasks)}


def answer(text, evidence=None):
    return {"text": text, "evidence": evidence or GOOD_EVIDENCE,
            "confidence": 0.9}


def contract(ids=("A1", "A2"), decompose=True):
    # Planner-path contract: decomposition is opt-in, and these tests
    # exercise the planner, so they opt in. decompose=False is covered
    # by its own tests below.
    return TaskContract(contract_id="t", version=1, goal="g",
                        deliverable="d",
                        acceptance=tuple(Criterion(i, "mechanical", i)
                                         for i in ids),
                        decompose=decompose)


class OrchestratorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="ma-orc-")
        self.root = os.path.join(self.tmp.name, "corpus")
        os.mkdir(self.root)
        with open(os.path.join(self.root, "a.txt"), "w") as fh:
            fh.write("real file content here")
        self.receipts = ReceiptStore(os.path.join(self.tmp.name, "r.jsonl"))
        self.ledger = Ledger(os.path.join(self.tmp.name, "l.sqlite3"), **CAPS)
        self.mem = Memory(os.path.join(self.tmp.name, "m.sqlite3"),
                          file_root=self.root)

    def tearDown(self):
        self.ledger.close()
        self.mem.close()
        self.tmp.cleanup()

    def build(self, plan_reply, spec_scripts, synth_text="done",
              budget=10, synth_evidence=None, contract=None,
              acceptance=(), planner_prompt=None, judge_prompt=None,
              judge_text="RELEVANT: answers it"):
        synth = {"text": synth_text,
                 "evidence": synth_evidence
                 if synth_evidence is not None else GOOD_EVIDENCE}
        script = [plan_reply]
        if judge_prompt is not None:
            script.append({"text": judge_text})
        script.append(synth)
        orc_model = FakeModel(script)
        specialists = {}
        for name, script in spec_scripts.items():
            tools = ToolWrapper(ToolRegistry(), receipts=self.receipts,
                                ledger=self.ledger, stage="trusted",
                                task_id="t1", agent_id=name)
            specialists[name] = Specialist(name, FakeModel(script), tools,
                                                 model_estimate_nano=0)
        begun = self.receipts.begin(agent_id="spec", task_id="t1",
                                        action="tool_call", tool="read_file",
                                        detail={"args": {"path": "a.txt"}})
        self.receipts.settle(begun["seq"], 0)
        orc = Orchestrator("orc", orc_model, specialists,
                           Critic("critic", self.mem,
                                  receipts=self.receipts),
                           receipts=self.receipts,
                           ledger=self.ledger, message_budget=budget,
                           model_estimate_nano=0, contract=contract,
                           acceptance=acceptance,
                           planner_prompt=planner_prompt,
                           judge_prompt=judge_prompt)
        self.last_orc_model = orc_model
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
        # verify() raises on any break and returns the receipt count.
        self.assertEqual(self.receipts.verify(), len(self.receipts.read_all()))
        actions = [r["action"] for r in self.receipts.read_all()]
        self.assertIn("task_start", actions)
        self.assertIn("task_end", actions)
        self.assertEqual(actions.count("delegate"), 2)

    def test_untagged_subtask_is_refused(self):
        orc = self.build(
            plan({"id": "s1", "brief": "b1", "specialist": "alpha"}),
            {"alpha": [answer("A")]}, contract=contract())
        result = orc.run("t1", "question?")
        self.assertEqual(result["status"], "escalated")
        self.assertEqual(result["findings"][0]["status"], "refused")
        denied = [r for r in self.receipts.read_all()
                  if r["action"] == "delegate" and r["outcome"] == "denied"]
        self.assertEqual(len(denied), 1)

    def test_unknown_criterion_is_refused(self):
        orc = self.build(
            plan({"id": "s1", "brief": "b1", "specialist": "alpha",
                  "criteria": ["AX"]}),
            {"alpha": [answer("A")]}, contract=contract())
        result = orc.run("t1", "question?")
        self.assertEqual(result["findings"][0]["status"], "refused")
        self.assertIn("AX", result["findings"][0]["reason"])

    def test_tagged_subtask_runs(self):
        orc = self.build(
            plan({"id": "s1", "brief": "b1", "specialist": "alpha",
                  "criteria": ["A1"]}),
            {"alpha": [answer("A")]}, contract=contract())
        result = orc.run("t1", "question?")
        self.assertEqual(result["findings"][0]["status"], "accepted")

    def test_garbage_plan_falls_back_to_default(self):
        orc = self.build({"text": "plan"},
                         {"alpha": [answer("A")]}, contract=contract())
        result = orc.run("t1", "question?")
        self.assertEqual(result["status"], "complete")
        self.assertEqual(result["findings"][0]["subtask"], "s1")

    def test_all_refused_does_not_fall_back(self):
        orc = self.build(
            plan({"id": "s1", "brief": "b1", "specialist": "alpha"}),
            {"alpha": [answer("A")]}, contract=contract())
        result = orc.run("t1", "question?")
        self.assertEqual(
            [f["status"] for f in result["findings"]], ["refused"])

    def test_no_decompose_skips_planner(self):
        orc = self.build({"text": "ignored plan",
                          "evidence": GOOD_EVIDENCE},
                         {"alpha": [answer("A")]},
                         contract=contract(decompose=False))
        result = orc.run("t1", "question?")
        self.assertEqual(result["status"], "complete")
        self.assertEqual([f["subtask"] for f in result["findings"]],
                         ["s1"])
        self.assertEqual(self.last_orc_model.calls, 1)
        self.assertEqual(self.last_orc_model.systems, [None])

    def test_no_decompose_still_enforces_acceptance(self):
        orc = self.build({"text": "ignored plan"},
                         {"alpha": [answer("A")]},
                         contract=contract(decompose=False),
                         acceptance=(("A1", lambda cand: False),))
        result = orc.run("t1", "question?")
        self.assertEqual(result["status"], "partial")

    def test_planner_receives_prompt_and_contract(self):
        orc = self.build(
            plan({"id": "s1", "brief": "b1", "specialist": "alpha",
                  "criteria": ["A1"]}),
            {"alpha": [answer("A")]}, contract=contract(),
            planner_prompt="PLAN")
        orc.run("t1", "question?")
        self.assertEqual(self.last_orc_model.systems[0], "PLAN")
        roles = [m.get("role") for m in self.last_orc_model.seen[0]]
        self.assertIn("contract", roles)
        text = " ".join(m.get("text", "")
                        for m in self.last_orc_model.seen[0])
        self.assertIn("A1", text)
        start = [r for r in self.receipts.read_all()
                 if r["action"] == "task_start"][0]
        self.assertTrue(start["detail"]["contract"].startswith("sha256:"))

    def test_failed_acceptance_downgrades_to_partial(self):
        orc = self.build(
            plan({"id": "s1", "brief": "b1", "specialist": "alpha"}),
            {"alpha": [answer("A")]},
            acceptance=(("A1", lambda cand: False),))
        result = orc.run("t1", "question?")
        self.assertEqual(result["status"], "partial")
        end = [r for r in self.receipts.read_all()
               if r["action"] == "task_end"][-1]
        self.assertEqual(end["detail"]["acceptance"], {"A1": False})
        self.assertIn("A1", end["detail"]["note"])

    def test_passing_acceptance_keeps_complete(self):
        orc = self.build(
            plan({"id": "s1", "brief": "b1", "specialist": "alpha"}),
            {"alpha": [answer("A")]},
            acceptance=(("A1", lambda cand: True),))
        result = orc.run("t1", "question?")
        self.assertEqual(result["status"], "complete")

    def test_relevant_finding_is_kept(self):
        orc = self.build(
            plan({"id": "s1", "brief": "b1", "specialist": "alpha",
                  "criteria": ["A1"]}),
            {"alpha": [answer("A")]}, contract=contract(),
            judge_prompt="J")
        result = orc.run("t1", "question?")
        self.assertEqual(result["findings"][0]["status"], "accepted")
        checks = [r for r in self.receipts.read_all()
                  if r["action"] == "relevance_check"]
        self.assertEqual(checks[0]["detail"]["verdict"], "relevant")

    def test_irrelevant_finding_is_flagged_and_excluded(self):
        orc = self.build(
            plan({"id": "s1", "brief": "b1", "specialist": "alpha",
                  "criteria": ["A1"]}),
            {"alpha": [answer("A")]}, contract=contract(),
            judge_prompt="J", judge_text="IRRELEVANT: mere listing")
        result = orc.run("t1", "question?")
        self.assertEqual(result["findings"][0]["status"], "flagged")
        self.assertEqual(result["status"], "escalated")

    def test_unparseable_judge_reply_keeps_the_finding(self):
        orc = self.build(
            plan({"id": "s1", "brief": "b1", "specialist": "alpha",
                  "criteria": ["A1"]}),
            {"alpha": [answer("A")]}, contract=contract(),
            judge_prompt="J", judge_text="maybe so")
        result = orc.run("t1", "question?")
        self.assertEqual(result["findings"][0]["status"], "accepted")
        checks = [r for r in self.receipts.read_all()
                  if r["action"] == "relevance_check"]
        self.assertEqual(checks[0]["detail"]["verdict"], "error")

    def test_judge_error_keeps_the_finding(self):
        orc = self.build(
            plan({"id": "s1", "brief": "b1", "specialist": "alpha",
                  "criteria": ["A1"]}),
            {"alpha": [answer("A")]}, contract=contract(),
            judge_prompt="J")

        class Boom:
            def complete(self, *args, **kwargs):
                raise RuntimeError("boom")

        orc.model = Boom()
        msg = AgentMessage(sender="alpha", task_id="t1", claim="c",
                           evidence=[], confidence=0.5)
        self.assertFalse(orc._judge("t1", "q?", {"id": "s1"}, msg))
        checks = [r for r in self.receipts.read_all()
                  if r["action"] == "relevance_check"]
        self.assertEqual(checks[0]["detail"]["verdict"], "error")

    def test_delegate_receipts_carry_briefs(self):
        orc = self.build(
            plan({"id": "s1", "brief": "b1", "specialist": "alpha",
                  "criteria": ["A1"]}),
            {"alpha": [answer("A")]}, contract=contract())
        orc.run("t1", "question?")
        delegates = [r for r in self.receipts.read_all()
                     if r["action"] == "delegate"]
        self.assertEqual(delegates[0]["detail"]["brief"], "b1")

    def test_synthesis_sees_the_question(self):
        orc = self.build(
            plan({"id": "s1", "brief": "b1", "specialist": "alpha"}),
            {"alpha": [answer("A")]})
        orc.run("t1", "question?")
        synth_input = self.last_orc_model.seen[1]
        self.assertTrue(any(m.get("role") == "question"
                            and "question?" in m["text"]
                            for m in synth_input))

    def test_synthesis_gets_the_format_note(self):
        orc = self.build(
            plan({"id": "s1", "brief": "b1", "specialist": "alpha"}),
            {"alpha": [answer("A")]})
        orc.run("t1", "question?")
        synth_input = self.last_orc_model.seen[-1]
        formats = [m for m in synth_input if m.get("role") == "format"]
        self.assertEqual(len(formats), 1)
        self.assertIn("quoted text", formats[0]["text"])

    def test_critic_verdicts_are_receipted_with_claims(self):
        orc = self.build(
            plan({"id": "s1", "brief": "b1", "specialist": "alpha"}),
            {"alpha": [answer("A")]})
        orc.run("t1", "question?")
        checks = [r for r in self.receipts.read_all()
                  if r["action"] == "critic_check"]
        self.assertEqual(len(checks), 1)
        self.assertEqual(checks[0]["detail"]["verdict"], "pass")
        self.assertEqual(checks[0]["detail"]["claim"], "A")

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
