"""Verified-only synthesis: the brief is built from verified notes.

The synthesizer's prompt holds verified notes only (never raw claims),
every cited note id must resolve to this task's verified set, and zero
verified notes refuses before the synthesis call is spent. Unverified
text never enters the synthesis, even quoted.
"""
import json
import os
import tempfile
import unittest

from mini_aurion.critic import Critic
from mini_aurion.fixture import FakeModel
from mini_aurion.ledger import Ledger
from mini_aurion.memory import Memory
from mini_aurion.orchestrator import Orchestrator
from mini_aurion.receipts import ReceiptStore
from mini_aurion.specialist import Specialist
from mini_aurion.tools import ToolRegistry, ToolWrapper

CAPS = {"task_cap_nano": 100_000_000, "daily_cap_nano": 500_000_000,
        "global_cap_nano": 1_000_000_000}

A_EVIDENCE = [{"kind": "file", "ref": "a.txt:1",
               "quote": "alpha file content"}]
B_EVIDENCE = [{"kind": "file", "ref": "b.txt:1",
               "quote": "beta file content"}]
PLANTED_TEXT = "PLANTED-UNVERIFIED-ZZZ do not synthesize this"


def _answer(text, ref, quote):
    return {"text": text,
            "evidence": [{"kind": "file", "ref": ref, "quote": quote}],
            "confidence": 0.9}


class CitingModel(FakeModel):
    """Orc model that cites this task's live verified notes. Note ids are
    random per run, so a fixed script cannot cite them; this model reads
    the store at synthesis time, like an honest live model would."""

    def __init__(self, mem, task_id, plan_subs):
        super().__init__([{"text": "plan", "subtasks": plan_subs}])
        self._mem = mem
        self._task = task_id

    def complete(self, messages, tools, system_prompt=None):
        if self.script:
            return super().complete(messages, tools, system_prompt)
        notes = [e for e in self._mem.recall()
                 if e.get("task_id") == self._task]
        ids = " ".join(f"[{n['id']}]" for n in notes)
        evidence = []
        for note in notes:
            evidence.extend(note["sources"])
        return {"text": f"Combined per {ids}", "evidence": evidence,
                "tool_calls": [], "confidence": 0.9, "subtasks": []}


class VerifiedSynthTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="ma-syn-")
        self.root = os.path.join(self.tmp.name, "corpus")
        os.mkdir(self.root)
        with open(os.path.join(self.root, "a.txt"), "w") as fh:
            fh.write("alpha file content here")
        with open(os.path.join(self.root, "b.txt"), "w") as fh:
            fh.write("beta file content here")
        self.receipts = ReceiptStore(os.path.join(self.tmp.name, "r.jsonl"))
        self.ledger = Ledger(os.path.join(self.tmp.name, "l.sqlite3"),
                             **CAPS)
        self.mem = Memory(os.path.join(self.tmp.name, "m.sqlite3"),
                          file_root=self.root,
                          mechanical_checks=frozenset({"critic"}))

    def tearDown(self):
        self.ledger.close()
        self.mem.close()
        self.tmp.cleanup()

    def _specialists(self, scripts):
        out = {}
        for name, script in scripts.items():
            tools = ToolWrapper(ToolRegistry(), receipts=self.receipts,
                                ledger=self.ledger, stage="trusted",
                                task_id="t1", agent_id=name)
            out[name] = Specialist(name, FakeModel(script), tools,
                                   model_estimate_nano=0)
        return out

    def _seed_read(self, path):
        begun = self.receipts.begin(agent_id="spec", task_id="t1",
                                    action="tool_call", tool="read_file",
                                    detail={"args": {"path": path}})
        self.receipts.settle(begun["seq"], 0)

    def _orc(self, orc_model, specialists, **kwargs):
        return Orchestrator(
            "orc", orc_model, specialists,
            Critic("critic", self.mem, receipts=self.receipts),
            receipts=self.receipts, ledger=self.ledger,
            message_budget=10, model_estimate_nano=0, **kwargs)

    def _plant_unverified(self):
        planted = self.mem.write(
            "intruder", PLANTED_TEXT,
            [{"kind": "file", "ref": "zzz.txt:1", "quote": "nope"}],
            task_id="t1")
        self.assertEqual(planted["status"], "unverified")
        return planted

    def _two_subs(self):
        return [{"id": "s1", "brief": "b1", "specialist": "alpha"},
                {"id": "s2", "brief": "b2", "specialist": "beta"}]

    def _two_scripts(self):
        return {"alpha": [_answer("Claim A", "a.txt:1",
                                  "alpha file content")],
                "beta": [_answer("Claim B", "b.txt:1",
                                 "beta file content")]}

    def test_prompt_holds_verified_notes_only(self):
        self._seed_read("a.txt")
        self._seed_read("b.txt")
        planted = self._plant_unverified()
        orc_model = FakeModel(
            [{"text": "plan", "subtasks": self._two_subs()},
             {"text": "A and B", "evidence": A_EVIDENCE + B_EVIDENCE}])
        orc = self._orc(orc_model, self._specialists(self._two_scripts()))
        result = orc.run("t1", "question?")
        self.assertEqual(result["status"], "complete")
        # The planted note reaches neither the prompt nor the brief.
        seen_text = json.dumps(orc_model.seen)
        self.assertNotIn(PLANTED_TEXT, seen_text)
        self.assertNotIn(planted["id"], seen_text)
        self.assertNotIn(PLANTED_TEXT, result["text"])
        self.assertNotIn(planted["id"], result["text"])
        # The verified notes do reach the synthesis prompt, id-tagged.
        synth_seen = json.dumps(orc_model.seen[1])
        self.assertIn("Claim A", synth_seen)
        self.assertIn("Claim B", synth_seen)
        self.assertIn("[mem_", synth_seen)

    def test_cite_to_unverified_note_refuses(self):
        self._seed_read("a.txt")
        self._seed_read("b.txt")
        planted = self._plant_unverified()
        orc_model = FakeModel(
            [{"text": "plan", "subtasks": self._two_subs()},
             {"text": f"Per [{planted['id']}]: {PLANTED_TEXT}",
              "evidence": A_EVIDENCE + B_EVIDENCE}])
        orc = self._orc(orc_model, self._specialists(self._two_scripts()))
        result = orc.run("t1", "question?")
        self.assertEqual(result["status"], "partial")
        # Fallback carries verified notes only: the planted text and id
        # appear nowhere in the brief.
        self.assertNotIn(PLANTED_TEXT, result["text"])
        self.assertNotIn(planted["id"], result["text"])
        self.assertIn("Claim A", result["text"])
        self.assertIn("Claim B", result["text"])
        task_end = [r for r in self.receipts.read_all()
                    if r["action"] == "task_end"][-1]
        self.assertIn("verified set", task_end["detail"]["note"])
        self.assertIn(planted["id"], task_end["detail"]["note"])

    def test_zero_verified_notes_refuses_before_synth_call(self):
        self._seed_read("a.txt")
        # A specialist named "critic" trips self-verification at filing:
        # the finding is accepted (evidence is good) but no verified
        # note exists, so synthesis must refuse without spending a call.
        orc_model = FakeModel(
            [{"text": "plan", "subtasks": [
                {"id": "s1", "brief": "b1",
                 "specialist": "critic"}]},
             {"text": "unreachable", "evidence": A_EVIDENCE}])
        tools = ToolWrapper(ToolRegistry(), receipts=self.receipts,
                            ledger=self.ledger, stage="trusted",
                            task_id="t1", agent_id="critic")
        specs = {"critic": Specialist(
            "critic",
            FakeModel([_answer("Claim A", "a.txt:1",
                               "alpha file content")]),
            tools, model_estimate_nano=0)}
        orc = self._orc(orc_model, specs)
        result = orc.run("t1", "question?")
        self.assertEqual(
            [f["status"] for f in result["findings"]], ["accepted"])
        self.assertIsNone(result["findings"][0].get("note_id"))
        self.assertEqual(result["status"], "partial")
        self.assertIn("no verified notes", result["text"])
        self.assertEqual(orc_model.calls, 1)

    def test_good_note_cites_complete(self):
        self._seed_read("a.txt")
        self._seed_read("b.txt")
        orc_model = CitingModel(self.mem, "t1", self._two_subs())
        orc = self._orc(orc_model, self._specialists(self._two_scripts()))
        result = orc.run("t1", "question?")
        self.assertEqual(result["status"], "complete")
        note_ids = [f["note_id"] for f in result["findings"]]
        self.assertEqual(len(note_ids), 2)
        self.assertTrue(all(n.startswith("mem_") for n in note_ids))
        for note_id in note_ids:
            self.assertIn(note_id, result["text"])


if __name__ == "__main__":
    unittest.main()
