import json
import shutil
import subprocess
import unittest
from pathlib import Path

import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

V2_PATH = (Path(__file__).resolve().parent.parent / "eval"
           / "code_qa_v2.json")
ROOT = V2_PATH.parent.parent


def pin_available(pin: str) -> bool:
    """The pin test needs real git history. Shallow clones and tarballs
    skip it; the freeze was verified at build time either way."""
    if shutil.which("git") is None:
        return False
    r = subprocess.run(["git", "-C", str(ROOT), "cat-file", "-t", pin],
                       capture_output=True, text=True)
    return r.returncode == 0


class V2ShapeTests(unittest.TestCase):
    def test_ten_cases_frozen(self):
        data = json.loads(V2_PATH.read_text())
        self.assertEqual(data["set"], "code-qa.v2")
        self.assertEqual(len(data["frozen_commit"]), 40)
        self.assertEqual(len(data["cases"]), 10)
        ids = [c["id"] for c in data["cases"]]
        self.assertEqual(len(set(ids)), 10)
        for case in data["cases"]:
            self.assertIn(case["type"], ("code", "summary", "unanswerable"))


class V2PinTests(unittest.TestCase):
    def test_answers_exist_at_pin(self):
        data = json.loads(V2_PATH.read_text())
        pin = data["frozen_commit"]
        if not pin_available(pin):
            self.skipTest("pin not in local git history")
        for case in data["cases"]:
            with self.subTest(case=case["id"]):
                self.check_case(pin, case)

    def check_case(self, pin, case):
        expect = case["expect"]
        if case["type"] == "code":
            body = self.show(pin, expect["path"])
            # Dotted names never appear verbatim (class and def lines
            # are separate); each part must.
            for part in expect["function"].split("."):
                self.assertIn(part, body)
            self.assertIn(expect["line_contains"], body)
        elif case["type"] == "summary":
            lo, hi = expect["span"]
            lines = self.show(pin, expect["path"]).splitlines()
            self.assertLessEqual(lo, hi)
            self.assertLessEqual(hi, len(lines))
            self.assertGreaterEqual(hi - lo + 1, expect["bullets"])
        else:
            r = subprocess.run(
                ["git", "-C", str(ROOT), "grep", "-l", "-i",
                 expect["absent"], pin],
                capture_output=True, text=True)
            prefix = pin + ":"
            found = sorted(line[len(prefix):] if line.startswith(prefix)
                           else line
                           for line in r.stdout.split()) \
                if r.returncode == 0 else []
            self.assertEqual(found, sorted(expect["except"]))

    def show(self, pin, path):
        r = subprocess.run(["git", "-C", str(ROOT), "show",
                            f"{pin}:{path}"],
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, path)
        return r.stdout


if __name__ == "__main__":
    unittest.main()
