"""Reviewer findings on B2 (#79 to #84), kept as expected-failure tests.

Each test fails today. When its fix lands, unittest reports "unexpected
success" and CI fails until that test's marker is deleted; the test then
stays as a regression guard, per the review-findings contract.

No network, no API key, $0.
"""
from __future__ import annotations

import ipaddress
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mini_aurion.contract import Criterion, TaskContract  # noqa: E402
from mini_aurion.critic import Critic  # noqa: E402
from mini_aurion.fetcher import address_is_public, write_snapshot  # noqa: E402
from mini_aurion.fixture import FakeModel  # noqa: E402
from mini_aurion.ledger import Ledger  # noqa: E402
from mini_aurion.memory import Memory  # noqa: E402
from mini_aurion.messages import AgentMessage  # noqa: E402
from mini_aurion.orchestrator import Orchestrator  # noqa: E402
from mini_aurion.receipts import ReceiptStore  # noqa: E402
from mini_aurion.specialist import Specialist  # noqa: E402
from mini_aurion.tools import ToolRegistry, ToolWrapper  # noqa: E402

CAP = 10 ** 9


class FetchAddressGate(unittest.TestCase):

    def test_M1_shared_address_space_is_not_public(self):
        """address_is_public refuses loopback, private, link-local,
        multicast, reserved and unspecified. It allows 100.64.0.0/10
        (RFC 6598 shared address space), which ipaddress does not call
        private but does not call global either. Tailscale and other
        mesh VPNs hand every machine in the operator's own network an
        address in that range, so a fetched page saying "read
        http://100.x.y.z:8787/" reaches another of the operator's
        machines. D5's intent is "globally routable only".

        Fix: require ip.is_global as well as the current refusals
        (is_global alone admits 64:ff9b::/96, which the current list
        already refuses)."""
        for ip in ("100.64.0.1", "100.127.255.254"):
            with self.subTest(ip=ip):
                self.assertFalse(ipaddress.ip_address(ip).is_global)
                self.assertFalse(address_is_public(ip),
                                 f"{ip} is shared address space, not public")


class RedirectedCitations(unittest.TestCase):

    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="ma-m2-")
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name)
        self.receipts = ReceiptStore(self.base / "r.jsonl")
        self.memory = Memory(self.base / "m.db", file_root=self.base,
                             receipt_store=self.receipts,
                             snapshot_dir=self.base)
        self.addCleanup(self.memory.close)

    def test_M2_citing_the_final_url_of_a_redirect_counts_as_fetched(self):
        """fetch_url follows redirects (http to https is the common
        case) and returns final_url in its result; the snapshot records
        both URLs and memory's D1 accepts either. The critic's
        read-to-cite check compares the cited ref only with the URL the
        model *requested* (the tool_call args), so a model that cites
        the final URL the tool told it about gets "cited but never
        fetched in this task". A correct brief fails D1 on any redirect.

        Fix: in Critic._was_fetched, also match the settle receipt's
        detail.final_url (the wrapper already records it)."""
        digest, _ = write_snapshot(
            self.base, url="http://ex.test/p", final_url="https://ex.test/p",
            status=200, content_type="text/html",
            text="the committee agreed to a twelve percent rise")
        begun = self.receipts.begin(
            agent_id="spec", task_id="t", action="tool_call",
            tool="fetch_url", detail={"args": {"url": "http://ex.test/p"}})
        self.receipts.settle(begun["seq"], 0, detail={
            "snapshot": digest, "final_url": "https://ex.test/p",
            "status": 200})
        evidence = {"kind": "url", "ref": "https://ex.test/p",
                    "snapshot": digest, "quote": "twelve percent rise"}
        self.assertIsNone(self.memory.check_source(evidence, "t"))
        out = Critic("c", self.memory, receipts=self.receipts).check(
            AgentMessage(sender="s", task_id="t", claim="c",
                         evidence=[evidence]))
        self.assertEqual(out["verdict"], "pass", out["catches"])


class _Down:
    provider = "deepseek"
    model = "x"
    calls = 0
    last_cost_nano = 0

    def complete(self, *args, **kwargs):
        raise RuntimeError("HTTP 529 overloaded")

    def worst_case_nano(self, messages):
        return 0


class _OneFetch:
    last_cost_nano = 0

    def __init__(self, digest):
        self.digest = digest
        self.last_receipt_detail = None

    def call(self, name, args):
        self.last_receipt_detail = {"snapshot": self.digest,
                                    "final_url": args["url"], "status": 200}
        return {"text": "..", "snapshot": self.digest}


class ResearchRunOutcomes(unittest.TestCase):

    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="ma-m3-")
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name)
        self.receipts = ReceiptStore(self.base / "r.jsonl")
        self.ledger = Ledger(self.base / "l.db", task_cap_nano=CAP,
                             daily_cap_nano=CAP, global_cap_nano=CAP)
        self.addCleanup(self.ledger.close)
        self.memory = Memory(self.base / "m.db", file_root=self.base,
                             receipt_store=self.receipts,
                             snapshot_dir=self.base)
        self.addCleanup(self.memory.close)
        self.digest, _ = write_snapshot(
            self.base, url="https://ex.test/a", final_url="https://ex.test/a",
            status=200, content_type="text/plain",
            text="rates rose eighteen percent in october")
        self.contract = TaskContract(
            contract_id="research.v1", version=1, goal="q", deliverable="d",
            acceptance=(Criterion("A1", "mechanical", "facts cited"),
                        Criterion("A2", "mechanical", "forecasts registered")))

    def _run(self, verifier, acceptance=()):
        registry = ToolRegistry()
        registry.register("fetch_url", "READ", 0, _OneFetch(self.digest))
        tools = ToolWrapper(registry, receipts=self.receipts,
                            ledger=self.ledger, stage="experimental",
                            task_id="t", agent_id="spec", scope=self.contract)
        answer = json.dumps({"findings": [{
            "type": "fact", "claim": "Rates rose 18% in October.",
            "evidence": [{"kind": "url", "ref": "https://ex.test/a",
                          "snapshot": self.digest,
                          "quote": "rose eighteen percent in october"}]}]})
        model = FakeModel([
            {"text": "", "tool_calls": [{"tool": "fetch_url",
                                         "args": {"url": "https://ex.test/a"}}]},
            {"text": answer}])
        spec = Specialist("spec", model, tools, model_estimate_nano=0)
        orc = Orchestrator("orc", model, {"spec": spec},
                           Critic("c", self.memory, receipts=self.receipts),
                           receipts=self.receipts, ledger=self.ledger,
                           model_estimate_nano=0, contract=self.contract,
                           verifier_model=verifier, acceptance=acceptance)
        return orc.run_research("t", "q", contract=self.contract,
                                predictions_path=self.base / "p.jsonl",
                                models=["m"])

    def test_M3_a_verifier_outage_is_an_invalid_run_not_a_flag(self):
        """check_support turns a verifier error into verdict 'error',
        which run_research then treats like 'unsupported': the fact is
        flagged and the task ends 'escalated' with no error and no cause.
        H1 and L1 settled that a provider outage is an invalid run, not a
        result. Here it is also the planted-claim experiment's blind
        spot: a verifier seat that is down would record the planted
        claim as "flagged" and the genuine facts as "flagged" too.

        Fix: when any support check returns 'error', end the run with
        cause='error' (and say so in result['error']); the runner then
        marks the brief invalid, as the code-QA grader does."""
        result = self._run(_Down())
        self.assertEqual(result["findings"][0]["support"], "error")
        self.assertEqual(result.get("cause"), "error",
                         f"outage reported as status={result['status']!r}"
                         f" error={result['error']!r}")

    def test_M4_research_status_comes_from_the_contract_acceptance(self):
        """The research contract declares A1 and A2 as mechanical
        acceptance criteria (OPERATIONS section 12: done means
        accepted), but run_research never evaluates self.acceptance;
        task_end carries no per-criterion results and status is computed
        from the findings list instead. A failing criterion cannot turn
        'complete' into 'partial' here, as _finish does for code-QA.

        Fix: evaluate acceptance in _research_end exactly as _finish
        does, record the results in task_end, and downgrade status."""
        class Ok:
            provider = "meta"
            model = "x"
            calls = 0
            last_cost_nano = 0

            def complete(self, *args, **kwargs):
                return {"text": "SUPPORTED: says so", "tool_calls": []}

            def worst_case_nano(self, messages):
                return 0

        result = self._run(Ok(), acceptance=(("A1", lambda c: True),
                                             ("A2", lambda c: False)))
        self.assertEqual(result["status"], "partial",
                         "a failed acceptance criterion did not downgrade")
        end = [r for r in self.receipts.read_all()
               if r["action"] == "task_end"][-1]
        self.assertIn("acceptance", end["detail"])


if __name__ == "__main__":
    unittest.main()
