"""Reviewer finding on #19, kept as an expected-failure test.

When the fix lands this test passes, unittest reports "unexpected
success", and CI fails until the marker is deleted. The test then stays
as a regression guard, per the review-findings contract.

No network, no API key, $0.
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mini_aurion.fixture import FakeModel, FakeTools  # noqa: E402
from mini_aurion.ledger import Ledger  # noqa: E402
from mini_aurion.muse import _api_message  # noqa: E402
from mini_aurion.receipts import ReceiptStore  # noqa: E402
from mini_aurion.specialist import MAX_TOOL_CHARS, Specialist  # noqa: E402
from mini_aurion.tools import ToolRegistry, ToolWrapper  # noqa: E402

CAPS = {"task_cap_nano": 10_000, "daily_cap_nano": 50_000,
        "global_cap_nano": 100_000}


class SpecialistMarkerSurvivesAdapter(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="ma-g3-")
        self.receipts = ReceiptStore(os.path.join(self.tmp.name, "r.jsonl"))
        self.ledger = Ledger(os.path.join(self.tmp.name, "l.sqlite3"), **CAPS)

    def tearDown(self):
        self.ledger.close()
        self.tmp.cleanup()

    def _specialist_tool_result(self, full: str) -> dict:
        """The tool_result message the real specialist builds for `full`."""
        backend = FakeTools({"read_file": full})
        registry = ToolRegistry()
        registry.register("read_file", "READ", 0, backend)
        tools = ToolWrapper(registry, receipts=self.receipts,
                            ledger=self.ledger, stage="trusted",
                            task_id="t1", agent_id="spec")
        model = FakeModel(
            [{"text": "", "tool_calls": [{"tool": "read_file", "args": {}}]},
             {"text": "done", "evidence": [], "confidence": 0.5}])
        Specialist("spec", model, tools,
                   model_estimate_nano=0).investigate("t1", "brief")
        results = [m for m in model.seen[1] if m.get("role") == "tool_result"]
        self.assertEqual(len(results), 1)
        return results[0]

    @unittest.expectedFailure
    def test_G3_specialist_truncation_marker_reaches_the_model(self):
        """#19 set muse.MAX_MESSAGE_CHARS = MAX_TOOL_CHARS (12000). The
        specialist emits 12000 chars *plus* its marker, so the adapter cuts
        again: the specialist's marker (with the file's true size) is
        dropped and replaced by one reporting the size of the already-cut
        text. A 20000-char file reaches the model as 'showing 12000 of
        12042 chars'. Nothing is silent, but the model is told the file is
        42 chars longer than what it saw, not 8000.

        Hits the frozen eval today: receipts.py at c55a621 is ~12.7k chars
        before line numbering. No graded answer sits past the cut, so
        scores are unaffected.

        Fix: give the adapter headroom over the specialist's output (e.g.
        MAX_MESSAGE_CHARS = MAX_TOOL_CHARS + 200, or derive it from the
        specialist constant), so one marker with the true size arrives."""
        full = "x" * 20000
        msg = self._specialist_tool_result(full)
        sent = _api_message(msg)["content"]
        self.assertIn(f"of {len(full)} chars]", sent,
                      "the file's true size never reaches the model")
        self.assertEqual(sent.count("[truncated:"), 1,
                         "cut twice: two markers, or the wrong one")
        self.assertIn("x" * MAX_TOOL_CHARS, sent)


if __name__ == "__main__":
    unittest.main()
