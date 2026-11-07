import json
import os
import tempfile
import unittest
from pathlib import Path

import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from eval.grade_code_qa import grade  # noqa: E402

CASES = {c["id"]: c for c in json.loads(
    (Path(__file__).resolve().parent.parent / "eval" / "code_qa_v1.json")
    .read_text())["cases"]}


def result(text, evidence=None, status="complete"):
    return {"text": text, "evidence": evidence or [], "status": status}


class GraderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="ma-grade-")
        self.tree = Path(self.tmp.name)
        mod = self.tree / "mini_aurion"
        mod.mkdir()
        (mod / "tools.py").write_text('"""x"""\n\n\n    def get(self):\n')
        (self.tree / "OPERATIONS.md").write_text(
            "\n".join(f"line {n}" for n in range(200)))

    def tearDown(self):
        self.tmp.cleanup()

    def test_code_pass(self):
        ok, reasons = grade(
            CASES["qa-unknown-tool"],
            result("ToolRegistry.get raises UNKNOWN_TOOL; caller writes"
                   " action denied",
                   [{"kind": "file", "ref": "mini_aurion/tools.py:4"}]),
            self.tree)
        self.assertTrue(ok, reasons)

    def test_code_wrong_function_fails(self):
        ok, _ = grade(
            CASES["qa-unknown-tool"],
            result("ToolWrapper.call raises UNKNOWN_TOOL denied",
                   [{"kind": "file", "ref": "mini_aurion/tools.py:4"}]),
            self.tree)
        self.assertFalse(ok)

    def test_code_cite_without_line_fails(self):
        ok, reasons = grade(
            CASES["qa-unknown-tool"],
            result("ToolRegistry.get UNKNOWN_TOOL denied",
                   [{"kind": "file", "ref": "mini_aurion/tools.py"}]),
            self.tree)
        self.assertFalse(ok)
        self.assertTrue(any("cite containing" in r for r in reasons))

    def test_code_wrong_line_fails(self):
        ok, _ = grade(
            CASES["qa-unknown-tool"],
            result("ToolRegistry.get UNKNOWN_TOOL denied",
                   [{"kind": "file", "ref": "mini_aurion/tools.py:1"}]),
            self.tree)
        self.assertFalse(ok)

    def test_code_escalation_fails(self):
        ok, _ = grade(CASES["qa-unknown-tool"],
                      result("CANNOT_VERIFY: lost", [], status="escalated"),
                      self.tree)
        self.assertFalse(ok)

    def test_summary_pass(self):
        text = "\n".join(f"- rule {n} (OPERATIONS.md:{108 + n})"
                         for n in range(6))
        evidence = [{"kind": "file", "ref": f"OPERATIONS.md:{108 + n}"}
                    for n in range(6)]
        ok, reasons = grade(CASES["qa-tier-summary"], result(text, evidence),
                            self.tree)
        self.assertTrue(ok, reasons)

    def test_summary_short_bullets_fail(self):
        ok, _ = grade(CASES["qa-tier-summary"],
                      result("- one\n- two",
                             [{"kind": "file", "ref": "OPERATIONS.md:108"}]),
                      self.tree)
        self.assertFalse(ok)

    def test_unanswerable_pass(self):
        ok, _ = grade(CASES["qa-newsletter"],
                      result("CANNOT_VERIFY: no such function", [],
                             status="escalated"),
                      self.tree)
        self.assertTrue(ok)

    def test_unanswerable_answer_fails(self):
        ok, _ = grade(CASES["qa-newsletter"],
                      result("Mailer.send in mail.py",
                             [{"kind": "file", "ref": "mail.py:1"}]),
                      self.tree)
        self.assertFalse(ok)

    def test_cases_file_is_frozen(self):
        frozen = json.loads(
            (Path(__file__).resolve().parent.parent / "eval"
             / "code_qa_v1.json").read_text())
        self.assertEqual(frozen["set"], "code-qa.v1")
        self.assertEqual(frozen["frozen_commit"],
                         "c55a62132fc08073e92e5457fb9343729656e9ca")
        self.assertEqual(len(frozen["cases"]), 5)


if __name__ == "__main__":
    unittest.main()
