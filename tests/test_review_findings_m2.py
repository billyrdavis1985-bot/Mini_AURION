"""Reviewer findings on M2 (PR #3), written as tests that FAIL today.

Same contract as test_review_findings.py: each test is marked
expectedFailure, so the suite stays green while the gap is open. A fix makes
the test pass, unittest reports an "unexpected success", and the suite fails
until the marker is deleted. A fixed test cannot stay muted.

The M2 fixes closed every M0/M1 test as written. These probe the same rules
from a slightly different angle, where the fix holds against the original
test but not against the attack it stood for.

All tests run on fixtures: no network, no API key, $0.
"""
from __future__ import annotations

import os
import tempfile
import unittest

from mini_aurion.critic import Critic
from mini_aurion.fixture import FakeModel, FakeTools
from mini_aurion.ledger import Ledger
from mini_aurion.memory import Memory, MemoryError as MemError
from mini_aurion.orchestrator import Orchestrator
from mini_aurion.receipts import ReceiptError, ReceiptStore
from mini_aurion.specialist import Specialist
from mini_aurion.tools import ToolRegistry, ToolWrapper

BIG = 10 ** 12


class _Tmp(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="ma-review-m2-")
        self.dir = self._tmp.name
        self.root = os.path.join(self.dir, "corpus")
        os.mkdir(self.root)
        for name in ("a.txt", "other.txt"):
            with open(os.path.join(self.root, name), "w") as fh:
                fh.write(name)
        self.rs = ReceiptStore(self.p("r.jsonl"))
        self.led = Ledger(self.p("l.db"), task_cap_nano=BIG,
                          daily_cap_nano=BIG, global_cap_nano=BIG)
        self.mem = Memory(self.p("m.db"), file_root=self.root,
                          receipt_store=self.rs)

    def tearDown(self):
        self.led.close()
        self.mem.close()
        self._tmp.cleanup()

    def p(self, name):
        return os.path.join(self.dir, name)

    def spent(self) -> int:
        totals = self.led.totals()
        return totals["settled_nano"] + totals["inflight_nano"]


class _SilentBackend(FakeTools):
    """A paid backend that never reports what a call cost."""


class _PricedModel(FakeModel):
    """A model that reports a real cost per call, as a live client would."""

    def complete(self, messages, tools):
        self.last_cost_nano = 5_000
        return super().complete(messages, tools)


class M2Findings(_Tmp):

    @unittest.expectedFailure
    def test_B1_a_missing_anchor_with_receipts_present_is_an_error(self):
        """The anchor sits next to the receipts. Truncate the log AND delete
        the anchor, and verify() treats 'no anchor' as 'nothing to check'.
        Absence of the anchor is not evidence the chain is whole."""
        for _ in range(5):
            self.rs.append(agent_id="a", task_id="t", action="x")
        with open(self.rs.path) as fh:
            lines = fh.readlines()
        with open(self.rs.path, "w") as fh:
            fh.writelines(lines[:2])
        os.remove(self.rs.anchor_path)
        with self.assertRaises(ReceiptError):
            self.rs.verify()

    @unittest.expectedFailure
    def test_B3_an_unregistered_mechanical_verifier_is_refused(self):
        """mechanical=True is a flag any caller can pass, so the prefix
        check moved trust onto a boolean. A mechanical verifier should be a
        check registered in code; an unregistered id is refused even with
        the flag set."""
        e = self.mem.write("agent-a", "x", [{"kind": "file", "ref": "a.txt"}])
        with self.assertRaises(MemError):
            self.mem.verify(e["id"], "mechanical:agent-a", mechanical=True)

    @unittest.expectedFailure
    def test_B4_an_unreported_cost_settles_at_the_worst_case_not_zero(self):
        """tools.py reads getattr(backend, 'last_cost_nano', 0): a backend
        that reports nothing settles at $0. Unknown cost must be charged at
        the registered worst case."""
        reg = ToolRegistry()
        reg.register("paid_search", "READ", 5_000,
                     _SilentBackend({"paid_search": "r"}))
        ToolWrapper(reg, receipts=self.rs, ledger=self.led, stage="trusted",
                    task_id="t", agent_id="s").call("paid_search", {})
        self.assertGreaterEqual(self.spent(), 5_000)

    @unittest.expectedFailure
    def test_B5_model_calls_are_metered_by_default(self):
        """model_estimate_nano defaults to 0, and 0 means 'do not reserve'.
        A specialist built with defaults spends 5,000 per model call and
        the ledger records nothing. Metering must not be opt-in: either the
        estimate is required, or construction refuses without it."""
        try:
            spec = Specialist(
                "spec",
                _PricedModel([{"text": "fine",
                               "evidence": [{"kind": "file",
                                             "ref": "a.txt"}]}]),
                ToolWrapper(ToolRegistry(), receipts=self.rs, ledger=self.led,
                            stage="trusted", task_id="t", agent_id="spec"))
        except (TypeError, ValueError):
            return  # refused to build without an estimate: also a pass
        spec.investigate("t", "brief")
        self.assertGreaterEqual(self.spent(), 5_000)

    @unittest.expectedFailure
    def test_B6_synthesis_cannot_cite_evidence_no_finding_cited(self):
        """The synthesis passes the critic by citing any file that exists.
        At minimum, its evidence must come from the accepted findings; a
        pointer only the synthesizer introduced is new, unchecked support.

        This closes the mechanical half only. A synthesis that cites a
        finding's own file while adding a claim that file never made still
        passes; that needs a design decision (restrict synthesis to accepted
        claims, or a judge that may flag but never clear)."""
        spec = Specialist(
            "spec",
            FakeModel([{"text": "the sky is blue",
                        "evidence": [{"kind": "file", "ref": "a.txt"}]}]),
            ToolWrapper(ToolRegistry(), receipts=self.rs, ledger=self.led,
                        stage="trusted", task_id="t", agent_id="spec"))
        orc = Orchestrator(
            "orc",
            FakeModel([{"text": "plan"},
                       {"text": "The sky is blue AND the drug is safe.",
                        "evidence": [{"kind": "file", "ref": "other.txt"}]}]),
            {"spec": spec}, Critic("critic", self.mem), receipts=self.rs,
            ledger=self.led)
        self.assertNotEqual(orc.run("t", "q")["status"], "complete")


if __name__ == "__main__":
    unittest.main()
