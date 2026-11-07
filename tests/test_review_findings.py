"""Reviewer findings on M0 + M1, written as tests that FAIL today.

Each test pins one rule that OPERATIONS.md tags [code] but the code does not
yet enforce. They are marked expectedFailure so the suite stays green while
the gaps are open.

When a fix lands, its test starts passing and unittest reports an
"unexpected success", which FAILS the suite. That is deliberate: it forces
the fixer to delete the expectedFailure marker, so the test becomes a live
regression guard and cannot quietly stay muted.

Tests describe behavior, not implementation. A few fixes (the receipt-chain
anchor in particular) will need a new parameter or file; adjust the test's
setup to the new API, but keep the assertion.

All tests run on fixtures: no network, no API key, $0.
"""
from __future__ import annotations

import multiprocessing as mp
import os
import tempfile
import time
import unittest

from mini_aurion.critic import Critic
from mini_aurion.fixture import FakeModel, FakeTools
from mini_aurion.ledger import Ledger, LedgerError
from mini_aurion.memory import Memory, MemoryError as MemError
from mini_aurion.orchestrator import Orchestrator
from mini_aurion.receipts import ReceiptError, ReceiptStore
from mini_aurion.specialist import Specialist
from mini_aurion.tools import ToolWrapper

BIG = 10 ** 12


class _Tmp(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="ma-review-")
        self.dir = self._tmp.name
        self.root = os.path.join(self.dir, "corpus")
        os.mkdir(self.root)

    def tearDown(self):
        self._tmp.cleanup()

    def p(self, name):
        return os.path.join(self.dir, name)


# ----------------------------------------------------------------- receipts --

class ReceiptChainFindings(_Tmp):
    """OPERATIONS 5: receipts are append-only and verified on every start."""

    @unittest.expectedFailure
    def test_R1_tail_truncation_is_detected(self):
        """Deleting the newest receipts leaves a valid prefix. Replay from
        genesis cannot see it; only a head anchor kept OUTSIDE the store can."""
        rs = ReceiptStore(self.p("r.jsonl"))
        for _ in range(5):
            rs.append(agent_id="a", task_id="t", action="x")
        with open(rs.path) as fh:
            lines = fh.readlines()
        with open(rs.path, "w") as fh:
            fh.writelines(lines[:2])
        with self.assertRaises(ReceiptError):
            rs.verify()

    @unittest.expectedFailure
    def test_R2_rewritten_history_is_detected(self):
        """An unkeyed chain can be rebuilt by anyone who can write the file:
        replace a failed outcome with ok and every hash still checks."""
        rs = ReceiptStore(self.p("r.jsonl"))
        rs.append(agent_id="a", task_id="t", action="delete_prod",
                  outcome="failed")
        os.remove(rs.path)
        rs.append(agent_id="a", task_id="t", action="delete_prod",
                  outcome="ok")
        with self.assertRaises(ReceiptError):
            rs.verify()

    @unittest.expectedFailure
    def test_R3_a_pending_action_settles_exactly_once(self):
        """settle() refuses a second settlement, but the public append()
        does not, and verify() accepts the result: failed, then ok."""
        rs = ReceiptStore(self.p("r.jsonl"))
        pending = rs.begin(agent_id="a", task_id="t", action="send")
        rs.settle(pending["seq"], 1)
        try:
            rs.append(agent_id="a", task_id="t", action="settle",
                      outcome="ok", settles=pending["seq"])
        except ReceiptError:
            return  # refused at write time: also a pass
        with self.assertRaises(ReceiptError):
            rs.verify()


# ------------------------------------------------------------------- memory --

class MemoryFindings(_Tmp):
    """OPERATIONS 6: verification needs evidence and an independent owner."""

    def setUp(self):
        super().setUp()
        self.rs = ReceiptStore(self.p("r.jsonl"))
        self.mem = Memory(self.p("m.db"), file_root=self.root,
                          receipt_store=self.rs)

    def tearDown(self):
        self.mem.close()
        super().tearDown()

    @unittest.expectedFailure
    def test_M1_a_claim_with_no_sources_cannot_be_verified(self):
        """Absence of evidence is not evidence. Today a zero-source entry
        becomes 'verified' the moment any other agent says so."""
        e = self.mem.write("agent-a", "claim with no sources", [])
        with self.assertRaises(MemError):
            self.mem.verify(e["id"], "agent-b")

    @unittest.expectedFailure
    def test_M2_the_author_cannot_verify_under_a_mechanical_prefix(self):
        """Verifier identity is a caller-supplied string. The author passes
        the self-verify check by calling itself 'mechanical:<author>'."""
        open(os.path.join(self.root, "ev.txt"), "w").write("x")
        e = self.mem.write("agent-a", "x",
                           [{"kind": "file", "ref": "ev.txt"}])
        with self.assertRaises(MemError):
            self.mem.verify(e["id"], "mechanical:agent-a")

    @unittest.expectedFailure
    def test_M3_a_failed_receipt_is_not_evidence(self):
        """A receipt that settled 'failed' resolves as a valid source with no
        reason recorded. Evidence that an action failed cannot support a
        claim that it worked."""
        pending = self.rs.begin(agent_id="a", task_id="t", action="run_exp")
        failed = self.rs.settle(pending["seq"], 1)
        e = self.mem.write("agent-a", "experiment succeeded",
                           [{"kind": "receipt", "ref": failed["seq"]}])
        self.assertTrue(e["reason"], "failed receipt accepted silently")

    @unittest.expectedFailure
    def test_M4_a_dispute_is_not_cleared_by_any_other_agent(self):
        open(os.path.join(self.root, "ev.txt"), "w").write("x")
        e = self.mem.write("agent-a", "x",
                           [{"kind": "file", "ref": "ev.txt"}])
        self.mem.dispute(e["id"], "agent-c", "contradicts run 4")
        with self.assertRaises(MemError):
            self.mem.verify(e["id"], "agent-b")

    @unittest.expectedFailure
    def test_M5_expiry_flags_every_descendant_not_just_children(self):
        a = self.mem.write("a", "A", [], ttl_seconds=1)
        b = self.mem.write("a", "B", [], derived_from=[a["id"]],
                           ttl_seconds=BIG)
        c = self.mem.write("a", "C", [], derived_from=[b["id"]],
                           ttl_seconds=BIG)
        self.mem.expire_sweep(now=time.time() + 5)
        self.assertEqual(self.mem.get(b["id"])["status"], "derived-unverified")
        self.assertEqual(self.mem.get(c["id"])["status"], "derived-unverified")

    @unittest.expectedFailure
    def test_M6_evidence_changed_after_verification_is_not_still_verified(self):
        """Existence was checked, content was not. Rewriting the cited file
        after verification leaves the entry 'verified'."""
        path = os.path.join(self.root, "ev.txt")
        open(path, "w").write("supports the claim")
        e = self.mem.write("agent-a", "z", [{"kind": "file", "ref": "ev.txt"}])
        self.mem.verify(e["id"], "agent-b")
        open(path, "w").write("says the opposite")
        verified = [x["id"] for x in self.mem.recall()]
        self.assertNotIn(e["id"], verified)


# ------------------------------------------------------------------- ledger --

def _racer(path, go, cap, amount):
    led = Ledger(path, task_cap_nano=BIG, daily_cap_nano=BIG,
                 global_cap_nano=cap)
    real = led._global_live

    def slow():
        # Widen the check-then-insert window so the race is reproducible.
        # The window exists without this; it is just narrow.
        value = real()
        time.sleep(0.2)
        return value

    led._global_live = slow
    go.wait()
    try:
        led.reserve(f"t{os.getpid()}", amount)
        return 1
    except LedgerError:
        return 0
    finally:
        led.close()


class LedgerFindings(_Tmp):
    """OPERATIONS 4: caps refuse before spending."""

    @unittest.expectedFailure
    def test_L1_concurrent_reservations_cannot_exceed_the_global_cap(self):
        """reserve() checks, then inserts, with no write lock between. Four
        processes each reserving 6 against a cap of 10 all succeed."""
        path = self.p("l.db")
        Ledger(path, task_cap_nano=1, daily_cap_nano=1,
               global_cap_nano=1).close()
        ctx = mp.get_context("spawn")
        with ctx.Manager() as manager:
            go = manager.Event()
            with ctx.Pool(4) as pool:
                jobs = [pool.apply_async(_racer, (path, go, 10, 6))
                        for _ in range(4)]
                time.sleep(1.0)
                go.set()
                granted = sum(j.get(timeout=30) for j in jobs)
        self.assertLessEqual(granted, 1)


# ------------------------------------------------------------- agent loop --

class _CostlyTools(FakeTools):
    """Backend that reports a real cost, as a paid tool would."""
    last_cost_nano = 0

    def call(self, name, args):
        self.last_cost_nano = 5_000
        return super().call(name, args)


class AgentLoopFindings(_Tmp):
    """OPERATIONS 2, 4, 8, 9: stages bound actions; every spend is reserved;
    hostile or malformed agent output degrades to escalation, never a crash."""

    def setUp(self):
        super().setUp()
        self.rs = ReceiptStore(self.p("r.jsonl"))
        self.led = Ledger(self.p("l.db"), task_cap_nano=100,
                          daily_cap_nano=100, global_cap_nano=100)
        self.mem = Memory(self.p("m.db"), file_root=self.root,
                          receipt_store=self.rs)
        open(os.path.join(self.root, "a.txt"), "w").write("real")
        self.backend = _CostlyTools({"delete_everything": "DELETED"})

    def tearDown(self):
        self.led.close()
        self.mem.close()
        super().tearDown()

    def wrapper(self, stage="experimental", agent="spec"):
        return ToolWrapper(self.backend, receipts=self.rs, ledger=self.led,
                           stage=stage, task_id="t", agent_id=agent)

    def orchestrator(self, plan, spec_script, synth=None, budget=10):
        spec = Specialist("spec", FakeModel(spec_script), self.wrapper())
        script = [plan] + ([synth] if synth else [])
        return Orchestrator("orc", FakeModel(script), {"spec": spec},
                            Critic("critic", self.mem), receipts=self.rs,
                            message_budget=budget)

    @unittest.expectedFailure
    def test_A1_the_model_does_not_choose_its_own_action_class(self):
        """specialist.py reads call.get('class', 'READ'): the MODEL labels
        each tool call, and an unlabeled call is READ, which is allowed in
        experimental. A destructive tool runs in the sandbox stage."""
        spec = Specialist("spec", FakeModel([
            {"tool_calls": [{"tool": "delete_everything", "args": {}}]},
            {"text": "done", "evidence": [{"kind": "file", "ref": "a.txt"}]},
        ]), self.wrapper("experimental"))
        spec.investigate("t", "brief")
        self.assertEqual(self.backend.invocations, [])

    @unittest.expectedFailure
    def test_A2_a_tool_call_that_costs_money_is_reserved_and_settled(self):
        """estimate_nano defaults to 0 and the specialist never passes one,
        so no reservation is made and the ledger never sees the spend.
        (Model calls bypass the ledger entirely; same rule applies.)"""
        spec = Specialist("spec", FakeModel([
            {"tool_calls": [{"tool": "delete_everything", "args": {},
                             "class": "READ"}]},
            {"text": "done", "evidence": [{"kind": "file", "ref": "a.txt"}]},
        ]), self.wrapper("trusted"))
        spec.investigate("t", "brief")
        totals = self.led.totals()
        self.assertEqual(totals["settled_nano"] + totals["inflight_nano"],
                         5_000)

    @unittest.expectedFailure
    def test_A3_the_critic_rejects_a_receipt_unrelated_to_the_claim(self):
        """Any receipt seq that exists passes. Seq 1 is the task_start log
        line; citing it 'verifies' any claim at all."""
        self.rs.append(agent_id="orc", task_id="t", action="task_start")
        from mini_aurion.messages import AgentMessage
        msg = AgentMessage(sender="spec", task_id="t",
                           claim="the experiment replicated twice",
                           evidence=[{"kind": "receipt", "ref": 1}])
        self.assertEqual(Critic("critic", self.mem).check(msg)["verdict"],
                         "fail")

    @unittest.expectedFailure
    def test_A4_a_denied_tool_escalates_instead_of_crashing_the_run(self):
        """ToolDenied is not caught: the run dies and no task_end is
        written, leaving a task_start with no outcome."""
        orc = self.orchestrator(
            {"text": "plan"},
            [{"tool_calls": [{"tool": "delete_everything", "args": {},
                              "class": "WRITE"}]}])
        result = orc.run("t", "q")
        self.assertIn(result["status"], ("escalated", "partial"))
        self.assertIn("task_end", [r["action"] for r in self.rs.read_all()])

    @unittest.expectedFailure
    def test_A5_a_planner_naming_an_unknown_specialist_does_not_crash(self):
        """Planner output is untrusted input (OPERATIONS 9). An unknown
        specialist name raises KeyError and no task_end is written."""
        orc = self.orchestrator(
            {"text": "plan", "subtasks": [
                {"id": "s1", "brief": "b", "specialist": "ghost"}]}, [])
        result = orc.run("t", "q")
        self.assertIn(result["status"], ("escalated", "partial"))
        self.assertIn("task_end", [r["action"] for r in self.rs.read_all()])

    @unittest.expectedFailure
    def test_A6_the_message_budget_is_per_task(self):
        """messages_used is set once in __init__ and never reset, so a
        second task on the same orchestrator starts with a spent budget."""
        ok = {"text": "fine", "evidence": [{"kind": "file", "ref": "a.txt"}]}
        three = {"text": "p", "subtasks": [
            {"id": f"s{i}", "brief": "b", "specialist": "spec"}
            for i in range(3)]}
        spec = Specialist("spec", FakeModel([ok] * 10), self.wrapper("trusted"))
        orc = Orchestrator("orc", FakeModel([three, {"text": "s"}] * 2),
                           {"spec": spec}, Critic("critic", self.mem),
                           receipts=self.rs, message_budget=4)
        self.assertEqual(orc.run("t1", "q")["status"], "complete")
        self.assertEqual(orc.run("t2", "q")["status"], "complete")

    @unittest.expectedFailure
    def test_A7_the_synthesis_is_checked_not_just_the_findings(self):
        """The synthesizer's free text is returned as the final answer with
        the findings' evidence attached, unchecked. It can add claims no
        finding made, and they arrive looking verified.

        NOTE: fixing this changes test_orchestrator's happy path, whose
        synthesizer cites nothing. Whether a synthesis must cite, or must be
        restricted to accepted claims, is a design call for the operator."""
        orc = self.orchestrator(
            {"text": "plan"},
            [{"text": "the sky is blue",
              "evidence": [{"kind": "file", "ref": "a.txt"}]}],
            synth={"text": "The sky is blue AND the drug is safe for kids."})
        self.assertNotEqual(orc.run("t", "q")["status"], "complete")


if __name__ == "__main__":
    unittest.main()
