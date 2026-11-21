"""Checkpoints + resume: a killed task continues, never restarts.

After the plan lands and after every settled finding, the
orchestrator receipts a checkpoint (plan, settled verdicts, contract
hash, message count). resume_task verifies the chain, refuses ended /
checkpoint-less / contract-changed tasks, and re-delegates only what
never settled — on the same task id, ledger, and memory. Kill is
simulated in-process: the exception escapes without task_end, exactly
what SIGKILL leaves behind.
"""
import json
import os
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from eval.demo_b3 import QUESTION as B3_QUESTION  # noqa: E402
from eval.demo_b3 import resume_leg  # noqa: E402
from eval.demo_b3 import main as demo_main  # noqa: E402
from eval.grade_code_qa import CASES_PATH, CONTRACT_PATH  # noqa: E402
from eval.grade_code_qa import ResumeRefused, resume_live  # noqa: E402
from eval.grade_code_qa import main as grade_main  # noqa: E402
from mini_aurion.contract import (Criterion, TaskContract,  # noqa: E402
                                  contract_hash, load_contract)
from mini_aurion.critic import Critic  # noqa: E402
from mini_aurion.fixture import FakeModel  # noqa: E402
from mini_aurion.ledger import Ledger  # noqa: E402
from mini_aurion.memory import Memory  # noqa: E402
from mini_aurion.orchestrator import Orchestrator  # noqa: E402
from mini_aurion.receipts import ReceiptStore  # noqa: E402
from mini_aurion.specialist import Specialist  # noqa: E402
from mini_aurion.tools import ToolRegistry, ToolWrapper  # noqa: E402

EVAL_DIR = Path(__file__).resolve().parent.parent / "eval"
CAPS = {"task_cap_nano": 100_000, "daily_cap_nano": 500_000,
        "global_cap_nano": 1_000_000}
GOOD_EVIDENCE = [{"kind": "file", "ref": "a.txt:1",
                  "quote": "real file content"}]
SYNTH = {"text": "done", "evidence": GOOD_EVIDENCE}
JUDGE_OK = {"text": "RELEVANT: answers it"}


def plan(*subtasks):
    return {"text": "plan", "subtasks": list(subtasks)}


def answer(text, evidence=None):
    return {"text": text, "evidence": evidence or GOOD_EVIDENCE,
            "confidence": 0.9}


def sub(sid, criteria, specialist="alpha"):
    return {"id": sid, "brief": f"brief {sid}", "specialist": specialist,
            "criteria": list(criteria)}


def contract(ids=("A1", "A2"), decompose=True, version=1):
    return TaskContract(
        contract_id="t", version=version, goal="g", deliverable="d",
        acceptance=tuple(Criterion(i, "mechanical", i) for i in ids),
        decompose=decompose)


class KillSimulated(Exception):
    pass


class KillModel(FakeModel):
    """Dies on one call like SIGKILL: no receipt, no settle, no
    task_end. costs scripts last_cost_nano per survived call so
    resumed spend can be compared against a clean run."""

    def __init__(self, script, *, die_on, costs=None):
        super().__init__(script)
        self.die_on = die_on
        self.costs = list(costs or [])

    def complete(self, messages, tools, system_prompt=None):
        if self.calls == self.die_on:
            raise KillSimulated(f"call {self.calls}")
        if self.costs:
            self.last_cost_nano = self.costs.pop(0)
        return super().complete(messages, tools, system_prompt)


class CostModel(FakeModel):
    def __init__(self, script, costs):
        super().__init__(script)
        self.costs = list(costs)

    def complete(self, messages, tools, system_prompt=None):
        if self.costs:
            self.last_cost_nano = self.costs.pop(0)
        return super().complete(messages, tools, system_prompt)


class CheckpointTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="ma-ckpt-")
        self.root = os.path.join(self.tmp.name, "corpus")
        os.mkdir(self.root)
        with open(os.path.join(self.root, "a.txt"), "w") as fh:
            fh.write("real file content here")
        self.receipts_path = os.path.join(self.tmp.name, "r.jsonl")
        self.ledger_path = os.path.join(self.tmp.name, "l.sqlite3")
        self.mem_path = os.path.join(self.tmp.name, "m.sqlite3")
        self._open()

    def _open(self):
        self.receipts = ReceiptStore(self.receipts_path)
        self.ledger = Ledger(self.ledger_path, **CAPS)
        self.mem = Memory(self.mem_path, file_root=self.root,
                          mechanical_checks=frozenset({"critic"}))

    def _reopen(self):
        """Close and reopen every store: the resume leg runs in what
        is, store-wise, a new process with no in-memory state."""
        self.ledger.close()
        self.mem.close()
        self._open()

    def tearDown(self):
        self.ledger.close()
        self.mem.close()
        self.tmp.cleanup()

    def _orc(self, orc_model, specialists, contra, budget=10):
        return Orchestrator(
            "orc", orc_model, specialists,
            Critic("critic", self.mem, receipts=self.receipts),
            receipts=self.receipts, ledger=self.ledger,
            message_budget=budget, model_estimate_nano=0, contract=contra)

    def _spec(self, name, model, task_id="t1", estimate=0):
        tools = ToolWrapper(ToolRegistry(), receipts=self.receipts,
                            ledger=self.ledger, stage="trusted",
                            task_id=task_id, agent_id=name)
        return Specialist(name, model, tools,
                          model_estimate_nano=estimate)

    def _preread(self, task_id="t1"):
        begun = self.receipts.begin(
            agent_id="spec", task_id=task_id, action="tool_call",
            tool="read_file", detail={"args": {"path": "a.txt"}})
        self.receipts.settle(begun["seq"], 0)

    def _killed_run(self, orc, task_id, brief):
        """Mirror run()'s preamble, then _run_inner without the
        guard: KillSimulated escapes exactly like SIGKILL — the
        chain stops mid-delegate with no task_end."""
        orc.messages_used = 0
        orc._ckpt_plan = []
        orc._ckpt_settled = {}
        orc._ckpt_refused = []
        orc._ckpt_goal = brief
        detail: dict = {"question": brief[:500]}
        if orc.contract is not None:
            detail["contract"] = contract_hash(orc.contract)
        orc.receipts.append(agent_id=orc.agent_id, task_id=task_id,
                            action="task_start", detail=detail, outcome="ok")
        with self.assertRaises(KillSimulated):
            orc._run_inner(task_id, brief)

    def _checkpoints(self, task_id="t1"):
        return [r for r in self.receipts.read_all()
                if r["task_id"] == task_id and r["action"] == "checkpoint"]

    def _clean_run(self, task_id="t1", costs=None):
        """Two accepted subtasks, straight through. Returns (text,
        settled spend, messages used) for resume comparison."""
        contra = contract()
        orc_model = FakeModel(
            [plan(sub("s1", ["A1"]), sub("s2", ["A2"])), SYNTH])
        spec = self._spec(
            "alpha", CostModel([answer("claim one"), answer("claim two")],
                               costs or [0, 0]),
            task_id=task_id, estimate=1000 if costs else 0)
        orc = self._orc(orc_model, {"alpha": spec}, contra)
        self._preread(task_id)
        result = orc.run(task_id, "brief text")
        self.assertEqual(result["status"], "complete")
        return (result["text"],
                self.ledger.spend(task_id)["settled_nano"],
                orc.messages_used)

    def test_plan_checkpoint_records_plan_contract_and_goal(self):
        self._clean_run()
        points = self._checkpoints()
        self.assertEqual([p["detail"]["phase"] for p in points],
                         ["plan", "progress", "progress"])
        first = points[0]["detail"]
        self.assertEqual([s["id"] for s in first["plan"]], ["s1", "s2"])
        self.assertEqual(first["settled"], [])
        self.assertEqual(first["refused"], [])
        self.assertEqual(first["contract"],
                         contract_hash(contract()))
        self.assertEqual(first["messages_used"], 0)
        self.assertEqual(first["goal"], "brief text")

    def test_progress_checkpoints_carry_settled_notes(self):
        self._clean_run()
        points = self._checkpoints()
        last = points[-1]["detail"]
        self.assertEqual([s["subtask"] for s in last["settled"]],
                         ["s1", "s2"])
        for record in last["settled"]:
            self.assertEqual(record["status"], "accepted")
            entry = self.mem.get(record["note_id"])
            self.assertEqual(entry["status"], "verified")
            self.assertEqual(entry["task_id"], "t1")

    def test_no_contract_writes_no_checkpoint(self):
        orc = self._orc(FakeModel([SYNTH]),
                        {"alpha": self._spec(
                            "alpha", FakeModel([answer("c")]))},
                        None)
        self._preread()
        orc.run("t1", "brief text")
        self.assertEqual(self._checkpoints(), [])

    def test_resume_completes_killed_run_identically(self):
        clean_text, clean_spend, clean_messages = self._clean_run(
            costs=[300, 400])
        # Second arena, same scripts: die on the specialist's second
        # call, mid-s2, after s1 settled and checkpointed.
        tmp = tempfile.TemporaryDirectory(prefix="ma-ckpt-kill-")
        self.addCleanup(tmp.cleanup)
        root = os.path.join(tmp.name, "corpus")
        os.mkdir(root)
        with open(os.path.join(root, "a.txt"), "w") as fh:
            fh.write("real file content here")
        receipts = ReceiptStore(os.path.join(tmp.name, "r.jsonl"))
        ledger = Ledger(os.path.join(tmp.name, "l.sqlite3"), **CAPS)
        mem = Memory(os.path.join(tmp.name, "m.sqlite3"), file_root=root,
                     mechanical_checks=frozenset({"critic"}))
        self.addCleanup(ledger.close)
        self.addCleanup(mem.close)
        tools = ToolWrapper(ToolRegistry(), receipts=receipts,
                            ledger=ledger, stage="trusted", task_id="t1",
                            agent_id="alpha")
        spec = Specialist(
            "alpha",
            KillModel([answer("claim one"), answer("claim two")],
                      die_on=1, costs=[300, 400]),
            tools, model_estimate_nano=1000)
        orc = Orchestrator(
            "orc", FakeModel([plan(sub("s1", ["A1"]), sub("s2", ["A2"]))]),
            {"alpha": spec}, Critic("critic", mem, receipts=receipts),
            receipts=receipts, ledger=ledger, message_budget=10,
            model_estimate_nano=0, contract=contract())
        begun = receipts.begin(
            agent_id="spec", task_id="t1", action="tool_call",
            tool="read_file", detail={"args": {"path": "a.txt"}})
        receipts.settle(begun["seq"], 0)
        orc.messages_used = 0
        orc._ckpt_plan = []
        orc._ckpt_settled = {}
        orc._ckpt_refused = []
        orc._ckpt_goal = "brief text"
        receipts.append(agent_id="orc", task_id="t1", action="task_start",
                        detail={"question": "brief text",
                                "contract": contract_hash(contract())},
                        outcome="ok")
        with self.assertRaises(KillSimulated):
            orc._run_inner("t1", "brief text")
        actions = [r["action"] for r in receipts.read_all()
                   if r["task_id"] == "t1"]
        self.assertNotIn("task_end", actions)
        self.assertEqual(actions.count("checkpoint"), 2)
        # New-process resume: reopened stores, fresh scripts for the
        # unfinished subtask and the synthesis.
        ledger.close()
        mem.close()
        receipts = ReceiptStore(os.path.join(tmp.name, "r.jsonl"))
        ledger = Ledger(os.path.join(tmp.name, "l.sqlite3"), **CAPS)
        mem = Memory(os.path.join(tmp.name, "m.sqlite3"), file_root=root,
                     mechanical_checks=frozenset({"critic"}))
        self.addCleanup(ledger.close)
        self.addCleanup(mem.close)
        tools = ToolWrapper(ToolRegistry(), receipts=receipts,
                            ledger=ledger, stage="trusted", task_id="t1",
                            agent_id="alpha")
        spec = Specialist(
            "alpha", CostModel([answer("claim two")], [400]), tools,
            model_estimate_nano=1000)
        orc = Orchestrator(
            "orc", FakeModel([SYNTH]), {"alpha": spec},
            Critic("critic", mem, receipts=receipts),
            receipts=receipts, ledger=ledger, message_budget=10,
            model_estimate_nano=0, contract=contract())
        result = orc.resume_task("t1")
        self.assertEqual(result["status"], "complete")
        self.assertEqual(result["text"], clean_text)
        self.assertEqual(ledger.spend("t1")["settled_nano"], clean_spend)
        self.assertEqual(orc.messages_used, clean_messages)
        self.assertEqual(result["replayed"], ["s1"])
        self.assertEqual(result["re_delegated"], ["s2"])
        delegates = [r["detail"]["subtask"] for r in receipts.read_all()
                     if r["action"] == "delegate"
                     and r["detail"].get("reason") is None]
        self.assertEqual(delegates.count("s1"), 1)
        self.assertEqual(delegates.count("s2"), 2)
        resumes = [r for r in receipts.read_all()
                   if r["action"] == "resume"]
        self.assertEqual(len(resumes), 1)
        self.assertEqual(resumes[0]["detail"]["replayed"], ["s1"])
        self.assertEqual(resumes[0]["detail"]["re_delegating"], ["s2"])

    def test_resume_refuses_ended_task(self):
        self._clean_run()
        before = len(self.receipts.read_all())
        orc = self._orc(FakeModel([SYNTH]),
                        {"alpha": self._spec(
                            "alpha", FakeModel([answer("c")]))},
                        contract())
        result = orc.resume_task("t1")
        self.assertEqual(result["status"], "refused")
        self.assertIn("already ended", result["reason"])
        self.assertEqual(len(self.receipts.read_all()), before)

    def test_resume_refuses_without_checkpoint(self):
        self.receipts.append(agent_id="orc", task_id="t1",
                             action="task_start",
                             detail={"question": "q"}, outcome="ok")
        orc = self._orc(FakeModel([]), {}, contract())
        result = orc.resume_task("t1")
        self.assertEqual(result["status"], "refused")
        self.assertIn("no checkpoint", result["reason"])

    def test_resume_needs_contract(self):
        contra = contract()
        orc = self._orc(
            FakeModel([plan(sub("s1", ["A1"])), SYNTH]),
            {"alpha": self._spec("alpha", FakeModel([answer("c")]))},
            contra)
        self._preread()
        orc.run("t1", "brief text")
        bare = self._orc(FakeModel([SYNTH]),
                         {"alpha": self._spec(
                             "alpha", FakeModel([answer("c")]))},
                         None)
        result = bare.resume_task("t1")
        self.assertEqual(result["status"], "refused")
        self.assertIn("contract", result["reason"])

    def test_resume_refuses_contract_change(self):
        contra = contract()
        spec = self._spec(
            "alpha", KillModel([answer("c")], die_on=0), estimate=0)
        orc = self._orc(
            FakeModel([plan(sub("s1", ["A1"]))]), {"alpha": spec}, contra)
        self._preread()
        self._killed_run(orc, "t1", "brief text")
        self._reopen()
        orc = self._orc(
            FakeModel([SYNTH]),
            {"alpha": self._spec("alpha", FakeModel([answer("c")]))},
            contract(version=99))
        result = orc.resume_task("t1")
        self.assertEqual(result["status"], "refused")
        self.assertIn("contract changed", result["reason"])

    def test_resume_refuses_broken_chain(self):
        contra = contract()
        spec = self._spec(
            "alpha", KillModel([answer("c")], die_on=0), estimate=0)
        orc = self._orc(
            FakeModel([plan(sub("s1", ["A1"]))]), {"alpha": spec}, contra)
        self._preread()
        self._killed_run(orc, "t1", "brief text")
        lines = Path(self.receipts_path).read_text(
            encoding="utf-8").splitlines()
        rec = json.loads(lines[2])
        rec["detail"]["tampered"] = True
        lines[2] = json.dumps(rec)
        Path(self.receipts_path).write_text("\n".join(lines) + "\n",
                                            encoding="utf-8")
        self._reopen()
        orc = self._orc(
            FakeModel([SYNTH]),
            {"alpha": self._spec("alpha", FakeModel([answer("c")]))},
            contra)
        result = orc.resume_task("t1")
        self.assertEqual(result["status"], "refused")
        self.assertIn("chain broken", result["reason"])

    def test_resume_replays_refused_and_retries_escalated(self):
        contra = contract(ids=("A1", "A2", "A3"))
        planned = plan(sub("s1", ["A1"]), {"id": "s2", "brief": "b2",
                                           "specialist": "alpha"},
                       sub("s3", ["A2"]), sub("s4", ["A3"]))
        # s1 answers, s2 is refused at the plan, s3's script is
        # exhausted (CANNOT_VERIFY, escalated), s4 dies mid-call.
        spec = self._spec(
            "alpha", KillModel([answer("claim one")], die_on=2),
            estimate=0)
        orc = self._orc(FakeModel([planned]), {"alpha": spec}, contra)
        self._preread()
        self._killed_run(orc, "t1", "brief text")
        self._reopen()
        spec = self._spec(
            "alpha", FakeModel([answer("claim three"),
                                answer("claim four")]))
        orc = self._orc(FakeModel([SYNTH]), {"alpha": spec}, contra)
        result = orc.resume_task("t1")
        # Partial, not complete: the refused subtask keeps accepted
        # from equaling findings, exactly as on a fresh run.
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["replayed"], ["s1"])
        self.assertEqual(result["re_delegated"], ["s3", "s4"])
        by_id = {f["subtask"]: f["status"] for f in result["findings"]}
        self.assertEqual(by_id,
                         {"s2": "refused", "s1": "accepted",
                          "s3": "accepted", "s4": "accepted"})
        delegates = [r["detail"]["subtask"] for r in self.receipts.read_all()
                     if r["action"] == "delegate"
                     and r["detail"].get("reason") is None]
        self.assertEqual(delegates.count("s3"), 2)


class _Router:
    def __init__(self, models):
        self._models = models

    def model_for(self, seat):
        return self._models[seat]

    def table_hash(self):
        return "sha256:fixture"


class ResumeRunnerTests(unittest.TestCase):
    """The --resume-from entry points, end to end on fixtures: killed
    state dirs built with the real contracts, resumed through the
    real runner functions."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="ma-ckpt-run-")
        self.root = os.path.join(self.tmp.name, "corpus")
        os.mkdir(self.root)
        with open(os.path.join(self.root, "a.txt"), "w") as fh:
            fh.write("real file content here")
        self.state = Path(self.tmp.name) / "state"
        self.state.mkdir()
        self.cases = json.loads(CASES_PATH.read_text())["cases"]

    def tearDown(self):
        self.tmp.cleanup()

    def _preread(self, receipts, task_id):
        begun = receipts.begin(
            agent_id="spec", task_id=task_id, action="tool_call",
            tool="read_file", detail={"args": {"path": "a.txt"}})
        receipts.settle(begun["seq"], 0)

    def _kill_codeqa(self, task_id, brief):
        """Killed single-subtask code-QA chain in self.state, under
        the real frozen contract template."""
        contra = replace(load_contract(CONTRACT_PATH, ""), goal=brief)
        receipts = ReceiptStore(self.state / "receipts.jsonl")
        ledger = Ledger(self.state / "ledger.sqlite3", **CAPS)
        mem = Memory(self.state / "memory.sqlite3", file_root=self.root,
                     mechanical_checks=frozenset({"critic"}))
        tools = ToolWrapper(ToolRegistry(), receipts=receipts,
                            ledger=ledger, stage="trusted", task_id=task_id,
                            agent_id="spec")
        spec = Specialist(
            "spec", KillModel([answer("killed claim")], die_on=0),
            tools, model_estimate_nano=0)
        orc = Orchestrator(
            "orc", FakeModel([]), {"spec": spec},
            Critic("critic", mem, receipts=receipts),
            receipts=receipts, ledger=ledger, message_budget=10,
            model_estimate_nano=0, contract=contra)
        self._preread(receipts, task_id)
        orc.messages_used = 0
        orc._ckpt_plan = []
        orc._ckpt_settled = {}
        orc._ckpt_refused = []
        orc._ckpt_goal = brief
        receipts.append(
            agent_id="orc", task_id=task_id, action="task_start",
            detail={"question": brief[:500],
                    "contract": contract_hash(contra)}, outcome="ok")
        with self.assertRaises(KillSimulated):
            orc._run_inner(task_id, brief)
        ledger.close()
        mem.close()
        (self.state / "proc.json").write_text(json.dumps(
            {"task_id": task_id,
             "ledger": str(self.state / "ledger.sqlite3"),
             "routing": None, "status": "killed"}), encoding="utf-8")

    def test_grade_main_refuses_resume_with_cases(self):
        rc = grade_main(["--tree", "t", "--out", "o",
                         "--resume-from", "s", "--cases", "x"])
        self.assertEqual(rc, 2)

    def test_demo_main_refuses_resume_with_legs(self):
        rc = demo_main(["--tree", "t", "--routing", "r", "--out", "o",
                        "--resume-from", "s", "--legs", "serial"])
        self.assertEqual(rc, 2)

    def test_resume_live_completes_killed_task(self):
        self._kill_codeqa("qa-newsletter-r1", "resume brief text")
        template = load_contract(CONTRACT_PATH, "")
        result = resume_live(
            self.state, Path(self.root), template, self.cases, 100_000,
            spec_model=FakeModel([answer("resumed claim")]),
            orc_model=FakeModel([JUDGE_OK, SYNTH]))
        # A1 fails on fixture text, so partial — the mechanics, not
        # the grade, are under test.
        self.assertEqual(result["status"], "partial")
        self.assertTrue(result["resumed"])
        self.assertEqual(result["replayed"], [])
        self.assertEqual(result["re_delegated"], ["s1"])
        proc = json.loads((self.state / "proc.json").read_text(
            encoding="utf-8"))
        self.assertEqual(proc["status"], "ended")
        actions = [r["action"] for r in ReceiptStore(
            self.state / "receipts.jsonl").read_all()]
        self.assertEqual(actions.count("resume"), 1)
        self.assertEqual(actions.count("task_end"), 1)

    def test_resume_live_refuses_ended_task(self):
        self._kill_codeqa("qa-newsletter-r1", "resume brief text")
        template = load_contract(CONTRACT_PATH, "")
        resume_live(
            self.state, Path(self.root), template, self.cases, 100_000,
            spec_model=FakeModel([answer("resumed claim")]),
            orc_model=FakeModel([JUDGE_OK, SYNTH]))
        with self.assertRaises(ResumeRefused) as ctx:
            resume_live(
                self.state, Path(self.root), template, self.cases,
                100_000,
                spec_model=FakeModel([answer("resumed claim")]),
                orc_model=FakeModel([JUDGE_OK, SYNTH]))
        self.assertIn("already ended", str(ctx.exception))

    def test_resume_live_refuses_unknown_state(self):
        template = load_contract(CONTRACT_PATH, "")
        with self.assertRaises(ResumeRefused):
            resume_live(self.state, Path(self.root), template, self.cases,
                        100_000, spec_model=FakeModel([]),
                        orc_model=FakeModel([]))

    def _kill_leg(self, task_id):
        """Killed two-part demo leg in self.state: p1 accepted and
        checkpointed, p2 dead on its first model call."""
        contra = load_contract(EVAL_DIR / "contract-b3-demo-v1.json",
                               B3_QUESTION)
        receipts = ReceiptStore(self.state / "receipts.jsonl")
        ledger = Ledger(self.state / "ledger.sqlite3", **CAPS)
        mem = Memory(self.state / "memory.sqlite3", file_root=self.root,
                     mechanical_checks=frozenset({"critic"}))
        specs = {}
        for name, model in (
                ("spec-a", FakeModel([answer("claim one")])),
                ("spec-b", KillModel([answer("claim two")], die_on=0))):
            tools = ToolWrapper(
                ToolRegistry(), receipts=receipts, ledger=ledger,
                stage="trusted", task_id=task_id, agent_id=name)
            specs[name] = Specialist(name, model, tools,
                                     model_estimate_nano=0)
        orc = Orchestrator(
            "orc", FakeModel([plan(
                {"id": "p1", "brief": "b1", "specialist": "spec-a",
                 "criteria": ["A1"]},
                {"id": "p2", "brief": "b2", "specialist": "spec-b",
                 "criteria": ["A2"]})]),
            specs, Critic("critic", mem, receipts=receipts),
            receipts=receipts, ledger=ledger, message_budget=10,
            model_estimate_nano=0, contract=contra)
        self._preread(receipts, task_id)
        orc.messages_used = 0
        orc._ckpt_plan = []
        orc._ckpt_settled = {}
        orc._ckpt_refused = []
        orc._ckpt_goal = "leg brief"
        receipts.append(
            agent_id="orc", task_id=task_id, action="task_start",
            detail={"question": "leg brief",
                    "contract": contract_hash(contra)}, outcome="ok")
        with self.assertRaises(KillSimulated):
            orc._run_inner(task_id, "leg brief")
        ledger.close()
        mem.close()
        (self.state / "proc.json").write_text(
            json.dumps({"task_id": task_id, "status": "killed"}),
            encoding="utf-8")

    def test_resume_leg_completes_killed_leg(self):
        self._kill_leg("x-serial")
        contra = load_contract(EVAL_DIR / "contract-b3-demo-v1.json",
                               B3_QUESTION)
        cases = {c["id"]: c for c in self.cases}
        router = _Router({"specialist": FakeModel([]),
                          "specialist_b": FakeModel([answer("claim two")]),
                          "orchestrator": FakeModel([JUDGE_OK, SYNTH])})
        leg = resume_leg(self.state, Path(self.root), contra, cases,
                         router, 100_000)
        self.assertEqual(leg["status"], "partial")
        self.assertTrue(leg["resumed"])
        self.assertEqual(leg["replayed"], ["p1"])
        self.assertEqual(leg["re_delegated"], ["p2"])
        self.assertEqual(leg["findings"],
                         [("p1", "accepted"), ("p2", "accepted")])
        # Both notes verified: the pre-kill one survived the reopened
        # memory, the resumed one filed fresh.
        self.assertEqual(leg["notes_verified"], 2)
