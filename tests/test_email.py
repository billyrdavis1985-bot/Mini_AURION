import json
import unittest
from pathlib import Path

import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

EMAIL_PATH = (Path(__file__).resolve().parent.parent / "eval"
              / "email_triage_v1.json")
LABELS = frozenset({"legitimate", "promotional", "malicious",
                    "needs-human"})


class EmailSetTests(unittest.TestCase):
    def test_set_shape(self):
        data = json.loads(EMAIL_PATH.read_text())
        self.assertEqual(data["set"], "email-triage.v1")
        self.assertEqual(len(data["cases"]), 5)
        root = EMAIL_PATH.parent.parent
        for case in data["cases"]:
            self.assertEqual(case["type"], "classify")
            self.assertIn(case["expect"]["label"], LABELS)
            self.assertIn("brief_scope", case)
            self.assertIn("untrusted data", case["brief_scope"])
            fixture = root / case["expect"]["path"]
            self.assertTrue(fixture.is_file(), case["id"])
            self.assertIn(case["expect"]["line_contains"],
                          fixture.read_text())


if __name__ == "__main__":
    unittest.main()
