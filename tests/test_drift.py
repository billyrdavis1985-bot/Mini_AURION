import json
import tempfile
import unittest
from pathlib import Path

import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from eval.grade_code_qa import grade  # noqa: E402
from eval.grade_drift import (DRIFT_PATH, drift_contract,  # noqa: E402
                                 frozen_match, main)


class DriftTests(unittest.TestCase):
    def test_set_file_loads_with_scope(self):
        data = json.loads(DRIFT_PATH.read_text())
        self.assertEqual(data["set"], "code-qa-drift.v1")
        self.assertEqual(data["frozen_tree"], "c55a621")
        self.assertEqual(len(data["cases"]), 3)
        for case in data["cases"]:
            self.assertIn("brief_scope", case)
            self.assertTrue(case["in_scope_paths"])
            self.assertIn("expect", case)

    def test_contract_is_per_case_scope_without_planner(self):
        case = json.loads(DRIFT_PATH.read_text())["cases"][0]
        contract = drift_contract(case, "goal")
        self.assertEqual(contract.contract_id, "code-qa-drift.v1")
        self.assertEqual(contract.in_scope_paths,
                         tuple(case["in_scope_paths"]))
        self.assertFalse(contract.decompose)
        self.assertEqual(contract.criterion_ids(), frozenset({"A1", "A2"}))

    def test_code_case_grades_like_code_qa(self):
        case = json.loads(DRIFT_PATH.read_text())["cases"][0]
        with tempfile.TemporaryDirectory(prefix="ma-drift-") as tmp:
            root = Path(tmp)
            (root / "mini_aurion").mkdir()
            (root / "mini_aurion" / "ledger.py").write_text(
                '"""Spend ledger."""\n'
                "    def reserve(self, task_id, worst_case_nano):\n")
            ok, _ = grade(
                case,
                {"status": "complete",
                 "text": "Ledger.reserve uses BEGIN IMMEDIATE"
                         " (mini_aurion/ledger.py:2)",
                 "evidence": [{"kind": "file",
                               "ref": "mini_aurion/ledger.py:2"}]},
                root)
            self.assertTrue(ok)

    def test_unanswerable_case_wants_escalation(self):
        case = [c for c in json.loads(DRIFT_PATH.read_text())["cases"]
                if c["type"] == "unanswerable"][0]
        with tempfile.TemporaryDirectory(prefix="ma-drift-") as tmp:
            ok, _ = grade(
                case,
                {"status": "escalated",
                 "text": "CANNOT_VERIFY: only in tests/",
                 "evidence": []},
                Path(tmp))
            self.assertTrue(ok)

    def test_main_refuses_without_live(self):
        self.assertEqual(main(["--tree", "x", "--out", "y"]), 2)

    def test_frozen_match_accepts_short_sha(self):
        full = "c55a62132fc08073e92e5457fb9343729656e9ca"
        self.assertTrue(frozen_match(full, "c55a621"))
        self.assertTrue(frozen_match(full, full))
        self.assertFalse(frozen_match("deadbee" + full[7:], "c55a621"))
        self.assertFalse(frozen_match(None, "c55a621"))


if __name__ == "__main__":
    unittest.main()
