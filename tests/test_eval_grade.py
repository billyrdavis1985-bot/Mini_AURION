import json
import os
import tempfile
import unittest
from pathlib import Path

import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from eval.grade_code_qa import (TreeReader, grade, out_inside_tree,  # noqa: E402
                                   repo_state, tree_clean)

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

    def test_code_dot_slash_cite_passes(self):
        ok, reasons = grade(
            CASES["qa-unknown-tool"],
            result("ToolRegistry.get raises UNKNOWN_TOOL; caller writes"
                   " action denied",
                   [{"kind": "file", "ref": "./mini_aurion/tools.py:4"}]),
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

    def test_list_dir_shows_layout(self):
        reader = TreeReader(self.tree)
        top = reader.call("list_dir", {"path": "."}).splitlines()
        self.assertIn("mini_aurion/", top)
        self.assertIn("OPERATIONS.md", top)
        sub = reader.call("list_dir", {"path": "mini_aurion"}).splitlines()
        self.assertEqual(sub, ["tools.py"])

    def test_list_dir_escape_is_refused(self):
        reader = TreeReader(self.tree)
        with self.assertRaises(PermissionError):
            reader.call("list_dir", {"path": ".."})

    def test_list_dir_on_file_is_refused(self):
        reader = TreeReader(self.tree)
        with self.assertRaises(NotADirectoryError):
            reader.call("list_dir", {"path": "OPERATIONS.md"})

    def test_unknown_tool_is_refused(self):
        reader = TreeReader(self.tree)
        with self.assertRaises(KeyError):
            reader.call("delete_tree", {})

    def test_repo_state_reports_clean_and_dirty(self):
        import shutil
        import subprocess
        if shutil.which("git") is None:
            self.skipTest("git is not installed")
        repo = Path(self.tmp.name) / "repo"
        repo.mkdir()
        run = lambda *a: subprocess.run(
            ["git", "-C", str(repo), *a], capture_output=True, text=True,
            timeout=15)
        run("init", "-q")
        run("config", "user.email", "t@t.t")
        run("config", "user.name", "t")
        (repo / "f.txt").write_text("v1")
        run("add", "f.txt")
        run("commit", "-qm", "one")
        head, dirty = repo_state(repo)
        self.assertFalse(dirty)
        self.assertEqual(len(head), 40)
        (repo / "f.txt").write_text("v2")
        _, dirty = repo_state(repo)
        self.assertTrue(dirty)
        (repo / "untracked.txt").write_text("x")
        run("checkout", "-q", "--", "f.txt")
        _, dirty = repo_state(repo)
        self.assertFalse(dirty)

    def test_tree_clean_rejects_edits_and_untracked(self):
        import shutil
        import subprocess
        if shutil.which("git") is None:
            self.skipTest("git is not installed")
        repo = Path(self.tmp.name) / "repo"
        repo.mkdir()
        run = lambda *a: subprocess.run(
            ["git", "-C", str(repo), *a], capture_output=True, text=True,
            timeout=15)
        run("init", "-q")
        run("config", "user.email", "t@t.t")
        run("config", "user.name", "t")
        (repo / "tools.py").write_text("v1")
        run("add", "tools.py")
        run("commit", "-qm", "one")
        self.assertTrue(tree_clean(repo))
        (repo / "tools.py").write_text("v2")
        self.assertFalse(tree_clean(repo))
        run("checkout", "-q", "--", "tools.py")
        (repo / "code_qa_v1.json").write_text("{}")
        self.assertFalse(tree_clean(repo))

    def test_out_inside_tree(self):
        tree = Path(self.tmp.name) / "tree"
        self.assertTrue(out_inside_tree(tree / "o.json", tree))
        self.assertTrue(out_inside_tree(tree, tree))
        self.assertFalse(out_inside_tree(Path(self.tmp.name) / "o.json",
                                         tree))

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
