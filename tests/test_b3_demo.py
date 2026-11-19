"""B3 demo runner: frozen question, two seats, serial/parallel legs.

The demo wiring runs here end to end on fixtures ($0): two scripted
specialists read a fixture tree, file verified notes, and synthesize,
once serial and once parallel. The live legs differ only in the models
the router hands out.
"""
import json
import tempfile
import unittest
from pathlib import Path

import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from eval.demo_b3 import (PART_CASES, QUESTION, _wall_clock,  # noqa: E402
                             build_brief, run_leg)
from eval.grade_code_qa import (EXACTNESS_NOTE, QUOTE_NOTE,  # noqa: E402
                                TreeReader)
from mini_aurion.contract import load_contract  # noqa: E402
from mini_aurion.fixture import FakeModel  # noqa: E402
from mini_aurion.router import Router, load_routing_table  # noqa: E402

EVAL_DIR = Path(__file__).resolve().parent.parent / "eval"

FROZEN_QUESTION = (
    "Part 1: Which function refuses unregistered tools, and what receipt "
    "does the caller write for the refusal? Cite path:line. Part 2: Which "
    "function serializes concurrent spend reservations so caps cannot "
    "race? Cite path:line.")

CLAIM_1 = ("ToolRegistry.get refuses unregistered tools; the caller "
           "writes an UNKNOWN_TOOL denied receipt.")
CLAIM_2 = ("Ledger.reserve serializes concurrent spend reservations "
           "with SQLite BEGIN IMMEDIATE.")

EV_1 = [{"kind": "file", "ref": "mini_aurion/tools.py:2",
         "quote": "def get(self, name):"}]
EV_2 = [{"kind": "file", "ref": "mini_aurion/ledger.py:2",
         "quote": "def reserve("}]


def _tool_call(tool, **args):
    return {"text": "", "tool_calls": [{"tool": tool, "args": args}]}


def _answer(text, evidence):
    return {"text": text, "evidence": evidence, "confidence": 0.9}


class FakeRouter:
    """Router stand-in holding scripted models per seat. run_leg only
    needs model_for/table_hash; the live router supplies real models."""

    def __init__(self, models):
        self._models = models

    def model_for(self, seat):
        return self._models[seat]

    def table_hash(self):
        return "sha256:fakeroutingtable"


class B3DemoTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="ma-b3-")
        self.root = Path(self.tmp.name)
        tree = self.root / "tree" / "mini_aurion"
        tree.mkdir(parents=True)
        (tree / "tools.py").write_text(
            "# fixture tools\ndef get(self, name):\n"
            "    raise UnknownTool(\"UNKNOWN_TOOL denied\")\n")
        (tree / "ledger.py").write_text(
            "# fixture ledger\ndef reserve(self, task_id):\n"
            "    self.db.execute(\"BEGIN IMMEDIATE\")\n")
        self.tree = self.root / "tree"
        cases = {c["id"]: c for c in json.loads(
            (EVAL_DIR / "code_qa_v1.json").read_text())["cases"]}
        self.cases = {cid: cases[cid] for cid in PART_CASES}
        self.contract = load_contract(
            EVAL_DIR / "contract-b3-demo-v1.json", QUESTION)

    def tearDown(self):
        self.tmp.cleanup()

    def _leg_models(self):
        plan = {"text": "plan", "subtasks": [
            {"id": "s1", "brief": "answer part 1", "specialist": "spec-a",
             "criteria": ["A1"]},
            {"id": "s2", "brief": "answer part 2", "specialist": "spec-b",
             "criteria": ["A1"]}]}
        judge = {"text": "RELEVANT: answers it"}
        synth = {"text": f"{CLAIM_1} {CLAIM_2}",
                 "evidence": EV_1 + EV_2}
        spec_a = [_tool_call("list_dir", path="."),
                  _tool_call("read_file",
                             path="mini_aurion/tools.py"),
                  _answer(CLAIM_1, EV_1)]
        spec_b = [_tool_call("list_dir", path="."),
                  _tool_call("read_file",
                             path="mini_aurion/ledger.py"),
                  _answer(CLAIM_2, EV_2)]
        return {"specialist": FakeModel(spec_a),
                "specialist_b": FakeModel(spec_b),
                "orchestrator": FakeModel([plan, judge, judge, synth])}

    def _brief(self):
        return build_brief(QUESTION, self.tree)

    def test_brief_matches_code_qa_loop_assembly(self):
        brief = build_brief(QUESTION, self.tree)
        self.assertTrue(brief.startswith(QUESTION))
        self.assertIn(EXACTNESS_NOTE, brief)
        self.assertIn(QUOTE_NOTE, brief)
        self.assertIn("Top-level layout", brief)

    def test_question_is_frozen_word_for_word(self):
        self.assertEqual(QUESTION, FROZEN_QUESTION)
        raw = json.loads(
            (EVAL_DIR / "contract-b3-demo-v1.json").read_text())
        self.assertEqual(raw["goal_template"], FROZEN_QUESTION)

    def test_demo_contract_decomposes(self):
        self.assertEqual(self.contract.contract_id, "b3-demo.v1")
        self.assertTrue(self.contract.decompose)
        self.assertEqual(self.contract.goal, QUESTION)
        self.assertEqual(set(self.contract.criterion_ids()), {"A1", "A2"})

    def test_demo_routing_has_two_specialist_seats(self):
        table = load_routing_table(EVAL_DIR / "routing-b3.json")
        router = Router(table, environ={"MODEL_API_KEY": "fixture"})
        models = [router.model_for(seat) for seat in
                  ("specialist", "specialist_b", "orchestrator")]
        self.assertEqual(len({id(m) for m in models}), 3)
        self.assertTrue(table["same_provider_ok"])
        self.assertEqual(router.table_hash(), router.table_hash())

    def test_serial_and_parallel_legs_agree(self):
        results = {}
        for tag, workers in (("serial", 1), ("parallel", 2)):
            task_id = f"b3t-{tag}"
            results[tag] = run_leg(
                QUESTION, self._brief(), task_id, self.tree,
                self.root / task_id, self.contract, self.cases,
                FakeRouter(self._leg_models()),
                10_000_000_000, workers, tag)
        for tag in ("serial", "parallel"):
            leg = results[tag]
            self.assertEqual(leg["status"], "complete", (tag, leg))
            self.assertEqual(
                leg["findings"], [("s1", "accepted"), ("s2", "accepted")])
            self.assertEqual(leg["notes_verified"], 2)
            for snippet in ("ToolRegistry.get", "UNKNOWN_TOOL", "denied",
                            "Ledger.reserve", "BEGIN IMMEDIATE"):
                self.assertIn(snippet, leg["text"])
        self.assertEqual(results["serial"]["text"],
                         results["parallel"]["text"])

    def test_wall_clock_reads_receipts(self):
        receipts = self.root / "r.jsonl"
        receipts.write_text(
            '{"action": "task_start", "ts": "2026-11-19T00:00:00Z"}\n'
            '{"action": "model_call",'
            ' "detail": {"duration_ms": 1200}}\n'
            '{"action": "model_call",'
            ' "detail": {"duration_ms": 800}}\n'
            '{"action": "task_end", "ts": "2026-11-19T00:00:05Z"}\n')
        self.assertEqual(_wall_clock(receipts),
                         {"wall_ms": 5000, "work_ms": 2000})


if __name__ == "__main__":
    unittest.main()
