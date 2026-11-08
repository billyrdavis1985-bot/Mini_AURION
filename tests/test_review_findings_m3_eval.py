"""Reviewer findings from a full-repo pass at d1b489c, kept as
regression tests.

All findings are fixed; markers were deleted with each fix, per the
review-findings contract.

H1 and H2 bear on the code-qa.v1 decision (>=4/5 per case): both can
change a case's score for reasons that are not the model's.

No network, no API key, $0.
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from eval.grade_code_qa import CASES_PATH, TreeReader, grade  # noqa: E402
from mini_aurion.critic import Critic  # noqa: E402
from mini_aurion.fixture import FakeModel, FakeTools  # noqa: E402
from mini_aurion.ledger import Ledger  # noqa: E402
from mini_aurion.memory import Memory  # noqa: E402
from mini_aurion.muse import MuseConfig, MuseError, MuseModel  # noqa: E402
from mini_aurion.orchestrator import Orchestrator  # noqa: E402
from mini_aurion.receipts import ReceiptStore  # noqa: E402
from mini_aurion.specialist import Specialist  # noqa: E402
from mini_aurion.tools import (ToolDenied, ToolFailed,  # noqa: E402
                               ToolRegistry, ToolWrapper)

CASES = {c["id"]: c for c in json.loads(CASES_PATH.read_text())["cases"]}
BIG_CAP = 10 ** 9


class _Harness(unittest.TestCase):
    """The eval's own wiring (run_live), with the model swapped out."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="ma-h-")
        self.base = Path(self._tmp.name)
        self.tree = self.base / "tree"
        self.tree.mkdir()
        self.receipts = ReceiptStore(self.base / "receipts.jsonl")
        self._open = []

    def tearDown(self):
        for thing in self._open:
            thing.close()
        self._tmp.cleanup()

    def ledger(self, cap: int = BIG_CAP) -> Ledger:
        led = Ledger(self.base / f"l{len(self._open)}.sqlite3",
                     task_cap_nano=cap, daily_cap_nano=cap,
                     global_cap_nano=cap)
        self._open.append(led)
        return led

    def orchestrator(self, model, ledger, *, backend=None, tool_est=0,
                     model_est=0) -> Orchestrator:
        memory = Memory(self.base / f"m{len(self._open)}.sqlite3",
                        file_root=self.tree, receipt_store=self.receipts)
        self._open.append(memory)
        registry = ToolRegistry()
        backend = backend or TreeReader(self.tree)
        registry.register("read_file", "READ", tool_est, backend)
        registry.register("list_dir", "READ", tool_est, backend)
        tools = ToolWrapper(registry, receipts=self.receipts, ledger=ledger,
                            stage="experimental", task_id="t",
                            agent_id="spec")
        spec = Specialist("spec", model, tools,
                          model_estimate_nano=model_est)
        return Orchestrator("orc", model, {"spec": spec},
                            Critic("critic", memory, receipts=self.receipts),
                            receipts=self.receipts, ledger=ledger,
                            model_estimate_nano=model_est)


class _DownModel:
    """What MuseModel does on a 401, 429, timeout or DNS failure."""
    last_cost_nano = 0

    def complete(self, messages, tools):
        raise MuseError("HTTP_ERROR", "HTTP 401")


class FailuresScoreAsEscalations(_Harness):

    def test_H1_a_transport_error_is_not_a_pass_on_the_unanswerable_case(self):
        """Orchestrator.run turns any exception into status 'escalated' with
        text 'CANNOT_VERIFY: orchestrator error: ...'. grade_unanswerable
        passes any escalation whose text holds CANNOT_VERIFY. So a bad key,
        a 429 or a timeout scores qa-newsletter as a PASS, and 5 failed
        calls read as '5/5, stable_4_of_5: true'.

        The code cases score the same errors as FAIL with no critic_check
        receipts, so 'read the receipts to tell loop failure from model
        failure' finds nothing to read.

        Fix: carry the cause on the result (e.g. result['error'], or a
        cause of model / cap / error), have the grader mark such runs
        invalid (neither pass nor fail), and have the report count invalid
        runs separately so the 4-of-5 rule only sees real attempts."""
        orc = self.orchestrator(_DownModel(), self.ledger())
        result = orc.run("t", CASES["qa-newsletter"]["question"])
        self.assertEqual(result["status"], "escalated")
        ok, _ = grade(CASES["qa-newsletter"], result, self.tree)
        self.assertFalse(ok, "an HTTP 401 graded as a correct escalation")

    def test_H1_a_spend_cap_refusal_is_not_a_pass_on_the_unanswerable_case(self):
        """Same path, different trigger. qa-newsletter is the last case in
        code_qa_v1.json, so if --cap-usd runs out mid-eval every remaining
        newsletter run is refused at the plan call ('CANNOT_VERIFY: plan
        refused: CAP_EXCEEDED') and grades as a PASS. The model was never
        asked."""
        orc = self.orchestrator(FakeModel([]), self.ledger(cap=1),
                                model_est=2)
        result = orc.run("t", CASES["qa-newsletter"]["question"])
        self.assertEqual(result["status"], "escalated")
        self.assertIn("refused", result["text"])
        ok, _ = grade(CASES["qa-newsletter"], result, self.tree)
        self.assertFalse(ok, "a cap refusal graded as a correct escalation")


class PartialPrefixEatsTheFirstBullet(_Harness):

    def test_H2_summary_survives_the_synthesis_fallback(self):
        """When synthesis is refused or fails the critic, the orchestrator
        returns 'PARTIAL: ' + the accepted claims. If the specialist's claim
        starts with a bullet, the first line becomes 'PARTIAL: - ...', which
        grade_summary does not count as a bullet: 6 correct bullets grade
        as 'answer has 5 bullets, need 6'.

        The fallback is the likely path, not an edge case: the synthesis
        call sees only the claim text (not the question, not the evidence
        pointers), so it usually can't produce evidence that is a subset of
        the finding's and pass the critic. Here the specialist's own answer
        grades PASS; the same answer after the fallback grades FAIL. That is
        a loop failure that would be read as a model failure on
        qa-tier-summary.

        Fix (either side): put the prefix on its own line ('PARTIAL:\\n'),
        or leave the text alone and let status='partial' carry it, or have
        the grader strip a leading 'PARTIAL: '."""
        rule_lines = [110, 112, 114, 117, 120, 122]
        (self.tree / "OPERATIONS.md").write_text(
            "\n".join(f"line {n}" for n in range(1, 130)) + "\n")
        claim = "\n".join(f"- rule {i} (OPERATIONS.md:{n})"
                          for i, n in enumerate(rule_lines, 1))
        evidence = [{"kind": "file", "ref": f"OPERATIONS.md:{n}"}
                    for n in rule_lines]
        case = CASES["qa-tier-summary"]
        # The specialist's answer on its own passes the grader.
        self.assertTrue(grade(case, {"status": "partial", "text": claim,
                                     "evidence": evidence}, self.tree)[0])
        model = FakeModel([
            {"text": "plan"},  # planner: no subtasks -> one default subtask
            {"text": "", "tool_calls": [
                {"tool": "read_file", "args": {"path": "OPERATIONS.md"}}]},
            {"text": claim, "evidence": evidence, "confidence": 0.9},
            {"text": "six rules", "evidence": []},  # synthesis -> fallback
        ])
        result = self.orchestrator(model, self.ledger()).run(
            "t", case["question"])
        self.assertEqual(result["status"], "partial")
        ok, reasons = grade(case, result, self.tree)
        self.assertTrue(ok, f"fallback broke a passing answer: {reasons}")


class MalformedToolArguments(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="ma-h3-")
        base = Path(self._tmp.name)
        self.receipts = ReceiptStore(base / "r.jsonl")
        self.led = Ledger(base / "l.sqlite3", task_cap_nano=BIG_CAP,
                          daily_cap_nano=BIG_CAP, global_cap_nano=BIG_CAP)
        registry = ToolRegistry()
        registry.register("list_dir", "READ", 0,
                          FakeTools({"list_dir": "a.txt"}))
        self.tools = ToolWrapper(registry, receipts=self.receipts,
                                 ledger=self.led, stage="experimental",
                                 task_id="t", agent_id="spec")

    def tearDown(self):
        self.led.close()
        self._tmp.cleanup()

    def test_H3_non_object_args_fail_the_call_not_the_task(self):
        """ToolWrapper.call does dict(args) while building the receipt. A
        model whose arguments decode to null, a string or a number (json
        'null' is a plausible reply for a no-argument list_dir) raises
        TypeError/ValueError. The specialist only handles NeedsApproval,
        ToolDenied and ToolFailed, so the whole task dies in the
        orchestrator's outer guard: no receipt for the attempt, a leaked
        reservation if the tool had a cost, and (via H1) a PASS on
        qa-newsletter.

        Fix: in ToolWrapper.call, refuse a non-dict args with a 'denied'
        receipt and ToolDenied (or coerce None to {} and run it). Either
        way, only the wrapper's own exceptions escape, and the attempt is
        receipted."""
        for bad in (None, "x", 5):
            with self.subTest(args=bad):
                before = len(self.receipts)
                try:
                    self.tools.call("list_dir", bad)
                except (ToolDenied, ToolFailed):
                    pass
                except Exception as exc:  # noqa: BLE001
                    self.fail(f"{type(exc).__name__} escaped the wrapper:"
                              f" {exc}")
                self.assertGreater(len(self.receipts), before,
                                   "the attempt left no receipt")

    def test_H3_object_arguments_from_the_api_do_not_crash_the_adapter(self):
        """muse.py's own rule: unparseable answers become evidence-free
        replies. _to_reply catches ValueError and AttributeError around
        json.loads(arguments), but a server that sends arguments as an
        object (some OpenAI-compatible servers do) raises TypeError, which
        escapes complete() and kills the task. Fix: accept a dict as-is,
        and treat any other non-string as an unparseable call (skip it)."""
        model = MuseModel(MuseConfig(api_key="k",
                                     base_url="http://127.0.0.1:9/v1"))
        response = {"choices": [{"message": {"content": "", "tool_calls": [
            {"function": {"name": "list_dir",
                          "arguments": {"path": "."}}}]}}]}
        try:
            reply = model._to_reply(response)
        except Exception as exc:  # noqa: BLE001
            self.fail(f"{type(exc).__name__} escaped _to_reply: {exc}")
        self.assertIsInstance(reply.get("tool_calls"), list)


class ReportedCostDropsRetries(_Harness):

    def test_H4_task_cost_matches_the_ledger_after_a_retry(self):
        """_delegate keeps only the retry's message, and _finish sums
        message.cost_nano, so the failed first attempt's spend vanishes from
        result['cost_nano']. The outer error guard reports 0 outright. The
        ledger is right (the eval reads it), but the orchestrator's own cost
        signal undercounts exactly the runs that needed a retry, which is
        what cost-per-success drift (OPERATIONS section 11) is meant to
        catch.

        Fix: report ledger.spend(task_id)['settled_nano'] in _finish and in
        the error guard, instead of summing messages."""
        (self.tree / "a.txt").write_text("hello\n")
        backend = FakeTools({"read_file": "1: hello", "list_dir": "a.txt"})
        model = FakeModel([
            {"text": "plan"},
            {"text": "", "tool_calls": [
                {"tool": "read_file", "args": {"path": "a.txt"}}]},
            {"text": "claim", "evidence": [
                {"kind": "file", "ref": "missing.txt"}]},  # critic fails
            {"text": "", "tool_calls": [
                {"tool": "read_file", "args": {"path": "a.txt"}}]},
            {"text": "claim", "evidence": [
                {"kind": "file", "ref": "a.txt"}]},        # retry passes
            {"text": "synth", "evidence": [
                {"kind": "file", "ref": "a.txt"}]},
        ])
        led = self.ledger()
        result = self.orchestrator(model, led, backend=backend,
                                   tool_est=100).run("t", "q")
        self.assertEqual(result["status"], "complete")
        self.assertEqual(result["cost_nano"],
                         led.spend("t")["settled_nano"],
                         "reported task cost leaves out the first attempt")


if __name__ == "__main__":
    unittest.main()
