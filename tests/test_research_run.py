"""Research task path: envelope parse, run_research, verify_findings.

All fixture, $0, no network: the investigator is scripted, fetches
are canned, and snapshots are written to a temp dir the critic
reads back. The live demo is docs/demos/B2.md, not these tests.
"""
import json
import os
import tempfile
import unittest

from mini_aurion.contract import TaskContract
from mini_aurion.critic import Critic
from mini_aurion.fetcher import write_snapshot
from mini_aurion.fixture import FakeModel
from mini_aurion.ledger import Ledger
from mini_aurion.memory import Memory
from mini_aurion.orchestrator import Orchestrator
from mini_aurion.receipts import ReceiptStore
from mini_aurion.research import (RESEARCH_INVESTIGATOR_PROMPT,
                                  parse_research_reply)
from mini_aurion.specialist import Specialist
from mini_aurion.tools import ToolRegistry, ToolWrapper

CAPS = {"task_cap_nano": 100_000, "daily_cap_nano": 500_000,
        "global_cap_nano": 1_000_000}
URL = "https://example.com/quantum-milestones"
TEXT = ("IonQ reported 36 algorithmic qubits in March 2026, with a "
        "roadmap to 64 by year end.")
QUOTE = "IonQ reported 36 algorithmic qubits in March 2026"


def contract():
    return TaskContract(contract_id="research.v1", version=1,
                        goal="q", deliverable="d")


def cite(snapshot):
    return {"kind": "url", "ref": URL, "snapshot": snapshot,
            "quote": QUOTE}


def fact(snapshot):
    return {"type": "fact",
            "claim": "IonQ reported 36 algorithmic qubits in March 2026.",
            "evidence": [cite(snapshot)]}


def forecast(snapshot):
    return {"type": "forecast",
            "claim": "IonQ reaches 64 algorithmic qubits by 2027-01-01.",
            "probability": 0.4, "resolves_at": "2027-01-01",
            "criterion": "an IonQ release states at least 64 AQ",
            "evidence": [cite(snapshot)]}


class ScriptedFetch:
    """Canned fetch_url backend: names its snapshot hash on the
    settle receipt, as FetchBackend does for real fetches."""

    def __init__(self, snapshot):
        self.snapshot = snapshot
        self.last_cost_nano = 0
        self.last_receipt_detail = None

    def call(self, name, args):
        self.last_receipt_detail = {"snapshot": self.snapshot}
        return {"text": TEXT, "snapshot": self.snapshot,
                "final_url": URL, "status": 200,
                "content_type": "text/plain"}


class ResearchRunTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="ma-research-")
        self.state = self.tmp.name
        self.receipts = ReceiptStore(
            os.path.join(self.state, "receipts.jsonl"))
        self.ledger = Ledger(os.path.join(self.state, "l.sqlite3"),
                             **CAPS)
        self.mem = Memory(os.path.join(self.state, "m.sqlite3"),
                          file_root=self.state,
                          receipt_store=self.receipts,
                          snapshot_dir=self.state,
                          mechanical_checks=frozenset({"critic"}))
        self.snapshot, _ = write_snapshot(
            self.state, url=URL, final_url=URL, status=200,
            content_type="text/plain", text=TEXT)

    def tearDown(self):
        self.ledger.close()
        self.mem.close()
        self.tmp.cleanup()

    def build(self, investigator, verifier):
        registry = ToolRegistry()
        registry.register("fetch_url", "READ", 0,
                          ScriptedFetch(self.snapshot),
                          description="Fetch a page.",
                          parameters={"type": "object"})
        tools = ToolWrapper(registry, receipts=self.receipts,
                            ledger=self.ledger, stage="experimental",
                            task_id="rq1", agent_id="spec", scope=None)
        spec = Specialist(
            "spec", investigator, tools, model_estimate_nano=0,
            investigator_prompt=RESEARCH_INVESTIGATOR_PROMPT)
        return Orchestrator(
            "orc", FakeModel(), {"spec": spec},
            Critic("critic", self.mem, receipts=self.receipts),
            receipts=self.receipts, ledger=self.ledger,
            model_estimate_nano=0, contract=contract(),
            verifier_model=verifier), tools

    def predictions(self):
        return os.path.join(self.state, "predictions.jsonl")

    def test_supported_run_registers_its_forecast(self):
        reply = json.dumps({"findings": [fact(self.snapshot),
                                         forecast(self.snapshot)]})
        investigator = FakeModel([
            {"text": "", "tool_calls": [
                {"tool": "fetch_url", "args": {"url": URL}}]},
            {"text": reply}])
        verifier = FakeModel([{"text": "SUPPORTED: quotes match"},
                              {"text": "They match.\nSUPPORTED"}] * 2)
        orc, _ = self.build(investigator, verifier)
        out = orc.run_research(
            "rq1", "which milestones?", contract=contract(),
            predictions_path=self.predictions(),
            models=["fixture/investigator", "fixture/verifier"])
        self.assertEqual(out["status"], "complete")
        self.assertEqual(out["registered"], 1)
        self.assertEqual([f["support"] for f in out["findings"]],
                         ["supported", "supported"])
        # The brief keeps the register fields: a replay from the
        # brief must pass the same D2 gate the brief passed.
        self.assertEqual(out["findings"][1]["probability"], 0.4)
        self.assertEqual(out["findings"][1]["resolves_at"], "2027-01-01")
        self.assertTrue(out["findings"][1]["criterion"])
        rows = open(self.predictions(), encoding="utf-8").read().strip()
        row = json.loads(rows.split("\n")[0])
        self.assertEqual(row["probability"], 0.4)
        self.assertEqual(row["snapshots"], [self.snapshot])
        self.assertEqual(row["task_id"], "rq1")

    def test_unsupported_claim_is_flagged_not_dropped(self):
        reply = json.dumps({"findings": [fact(self.snapshot),
                                         forecast(self.snapshot)]})
        investigator = FakeModel([
            {"text": "", "tool_calls": [
                {"tool": "fetch_url", "args": {"url": URL}}]},
            {"text": reply}])
        verifier = FakeModel(
            [{"text": "SUPPORTED: quotes match"},
             {"text": "They match.\nSUPPORTED"},
             {"text": "UNSUPPORTED: past the evidence"},
             {"text": "Past it.\nUNSUPPORTED"}])
        orc, _ = self.build(investigator, verifier)
        out = orc.run_research(
            "rq1", "which milestones?", contract=contract(),
            predictions_path=self.predictions(),
            models=["fixture/investigator", "fixture/verifier"])
        self.assertEqual(out["status"], "partial")
        self.assertEqual(out["findings"][1]["support"], "unsupported")
        self.assertTrue(out["findings"][1]["support_reason"])
        # Flagged but recorded: the register keeps what was claimed.
        self.assertEqual(out["registered"], 1)
        self.assertTrue(out["findings"][1]["registered"])

    def test_supported_fact_filed_flagged_forecast_not(self):
        reply = json.dumps({"findings": [fact(self.snapshot),
                                         forecast(self.snapshot)]})
        investigator = FakeModel([
            {"text": "", "tool_calls": [
                {"tool": "fetch_url", "args": {"url": URL}}]},
            {"text": reply}])
        verifier = FakeModel(
            [{"text": "SUPPORTED: quotes match"},
             {"text": "They match.\nSUPPORTED"},
             {"text": "UNSUPPORTED: past the evidence"},
             {"text": "Past it.\nUNSUPPORTED"}])
        orc, _ = self.build(investigator, verifier)
        out = orc.run_research(
            "rq1", "which milestones?", contract=contract(),
            predictions_path=self.predictions(),
            models=["fixture/investigator", "fixture/verifier"])
        notes = self.mem.recall()
        self.assertEqual(len(notes), 1)
        self.assertEqual(notes[0]["text"], out["findings"][0]["claim"])
        self.assertEqual(notes[0]["status"], "verified")
        self.assertEqual(out["findings"][0]["note_id"], notes[0]["id"])
        self.assertIsNone(out["findings"][1].get("note_id"))

    def test_judgment_never_filed_even_when_supported(self):
        judged = {"type": "assessment",
                  "claim": "Rates will keep rising.",
                  "reasoning": "two quarters of growth",
                  "evidence": [cite(self.snapshot)]}
        reply = json.dumps({"findings": [judged]})
        investigator = FakeModel([
            {"text": "", "tool_calls": [
                {"tool": "fetch_url", "args": {"url": URL}}]},
            {"text": reply}])
        verifier = FakeModel([{"text": "SUPPORTED: quotes match"},
                              {"text": "They match.\nSUPPORTED"}])
        orc, _ = self.build(investigator, verifier)
        out = orc.run_research(
            "rq1", "which milestones?", contract=contract(),
            predictions_path=self.predictions(), models=["m"])
        self.assertEqual(out["findings"][0]["support"], "supported")
        self.assertEqual(self.mem.recall(), [])
        self.assertNotIn("note_id", out["findings"][0])

    def test_unverified_note_excluded_from_default_recall(self):
        entry = self.mem.write("spec", "Draft claim.",
                               [cite(self.snapshot)], task_id="rq1")
        self.assertEqual(self.mem.recall(), [])
        self.assertEqual(
            [n["id"] for n in
             self.mem.recall(include_unverified=True)], [entry["id"]])

    def test_malformed_reply_gets_one_retry(self):
        reply = json.dumps({"findings": [fact(self.snapshot)]})
        investigator = FakeModel([
            {"text": "", "tool_calls": [
                {"tool": "fetch_url", "args": {"url": URL}}]},
            {"text": "here is what I found..."},
            {"text": reply}])
        verifier = FakeModel([{"text": "SUPPORTED: quotes match"},
                              {"text": "They match.\nSUPPORTED"}])
        orc, _ = self.build(investigator, verifier)
        out = orc.run_research(
            "rq1", "which milestones?", contract=contract(),
            predictions_path=self.predictions(), models=["m"])
        self.assertEqual(out["status"], "complete")
        # Two investigator passes: the failed envelope plus the retry.
        answered = [r for r in self.receipts.read_all()
                    if r["action"] == "model_call"
                    and r["agent_id"] == "spec"]
        self.assertEqual(len(answered), 3)

    def test_escalation_stays_an_escalation(self):
        orc, _ = self.build(FakeModel(), FakeModel())
        out = orc.run_research(
            "rq1", "which milestones?", contract=contract(),
            predictions_path=self.predictions(), models=["m"])
        self.assertEqual(out["status"], "escalated")
        self.assertIn("escalated", out["error"])

    def test_contract_scope_does_not_gate_fetch_url(self):
        """The scope gate only sees path args; fetch_url takes a url,
        so a research contract never blocks a fetch. D5 refusals are
        the fetcher's own job."""
        orc, tools = self.build(FakeModel(), FakeModel())
        tools.scope = contract()
        out = tools.call("fetch_url", {"url": URL})
        self.assertEqual(out["result"]["snapshot"], self.snapshot)


class ParseTests(unittest.TestCase):
    def test_valid_envelope(self):
        findings, error = parse_research_reply(
            json.dumps({"findings": [{"type": "fact"}]}))
        self.assertIsNone(error)
        self.assertEqual(findings, [{"type": "fact"}])

    def test_garbage_and_wrong_envelopes(self):
        _, error = parse_research_reply("here is what I found...")
        self.assertEqual(error, "reply is not JSON")
        _, error = parse_research_reply(json.dumps([1, 2]))
        self.assertEqual(error, "reply is not a JSON object")
        _, error = parse_research_reply(json.dumps({"findings": []}))
        self.assertEqual(error, "reply carries no findings list")


class VerifyFindingsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="ma-verify-")
        self.state = self.tmp.name
        self.receipts = ReceiptStore(
            os.path.join(self.state, "receipts.jsonl"))
        self.ledger = Ledger(os.path.join(self.state, "l.sqlite3"),
                             **CAPS)
        self.mem = Memory(os.path.join(self.state, "m.sqlite3"),
                          file_root=self.state,
                          receipt_store=self.receipts,
                          snapshot_dir=self.state,
                          mechanical_checks=frozenset({"critic"}))
        self.snapshot, _ = write_snapshot(
            self.state, url=URL, final_url=URL, status=200,
            content_type="text/plain", text=TEXT)
        registry = ToolRegistry()
        registry.register("fetch_url", "READ", 0,
                          ScriptedFetch(self.snapshot),
                          description="Fetch a page.",
                          parameters={"type": "object"})
        tools = ToolWrapper(registry, receipts=self.receipts,
                            ledger=self.ledger, stage="experimental",
                            task_id="rq1", agent_id="spec", scope=None)
        tools.call("fetch_url", {"url": URL})
        self.verifier = FakeModel()
        self.orc = Orchestrator(
            "orc", FakeModel(), {},
            Critic("critic", self.mem, receipts=self.receipts),
            receipts=self.receipts, ledger=self.ledger,
            model_estimate_nano=0, verifier_model=self.verifier)

    def tearDown(self):
        self.ledger.close()
        self.mem.close()
        self.tmp.cleanup()

    def test_replay_checks_d1_on_the_brief_task(self):
        self.verifier.script = [
            {"text": "SUPPORTED: quotes match"},
            {"text": "They match.\nSUPPORTED"}]
        out = self.orc.verify_findings(
            "verify-1", [fact(self.snapshot)], brief_task_id="rq1")
        self.assertEqual(out["findings"][0]["d1"], "pass")
        self.assertEqual(out["findings"][0]["support"], "supported")

    def test_forecast_replay_keeps_its_register_fields(self):
        from eval.run_research import _replay_finding
        brief_entry = {"type": "forecast", "claim": "c",
                       "probability": 0.4, "resolves_at": "2027-01-01",
                       "criterion": "k", "evidence": [],
                       "critic": "pass", "support": "unsupported",
                       "support_reason": "r", "registered": True}
        replayed = _replay_finding(brief_entry)
        self.assertEqual(replayed["probability"], 0.4)
        self.assertEqual(replayed["resolves_at"], "2027-01-01")
        self.assertEqual(replayed["criterion"], "k")
        self.assertNotIn("support", replayed)

    def test_bogus_snapshot_is_refused_without_a_call(self):
        bad = fact("0" * 64)
        out = self.orc.verify_findings(
            "verify-1", [bad], brief_task_id="rq1")
        self.assertEqual(out["findings"][0]["d1"], "fail")
        self.assertIsNone(out["findings"][0]["support"])
        self.assertEqual(self.verifier.calls, 0)

    def test_planted_overreach_is_flagged(self):
        planted = {"type": "fact",
                   "claim": "IonQ leads every quantum metric worldwide.",
                   "evidence": [cite(self.snapshot)]}
        self.verifier.script = [
            {"text": "UNSUPPORTED: the quote names one result"},
            {"text": "One result only.\nUNSUPPORTED"}]
        out = self.orc.verify_findings(
            "verify-1", [planted], brief_task_id="rq1")
        self.assertEqual(out["findings"][0]["d1"], "pass")
        self.assertEqual(out["findings"][0]["support"], "unsupported")


if __name__ == "__main__":
    unittest.main()
