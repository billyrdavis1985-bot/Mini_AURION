import json
import os
import tempfile
import time
import unittest

from mini_aurion.contract import TaskContract
from mini_aurion.fixture import ESCALATE_TEXT, FakeModel, FakeTools
from mini_aurion.ledger import Ledger
from mini_aurion.receipts import ReceiptStore
from mini_aurion.specialist import MAX_TOOL_CHARS, Specialist
from mini_aurion.tools import ToolRegistry, ToolWrapper

CAPS = {"task_cap_nano": 10_000, "daily_cap_nano": 50_000,
        "global_cap_nano": 100_000}


class SpecialistTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="ma-spec-")
        self.receipts = ReceiptStore(os.path.join(self.tmp.name, "r.jsonl"))
        self.ledger = Ledger(os.path.join(self.tmp.name, "l.sqlite3"), **CAPS)

    def tearDown(self):
        self.ledger.close()
        self.tmp.cleanup()

    def investigate_with(self, script, tools_dict=None, classes=None,
                         **kw):
        backend = FakeTools(tools_dict or {})
        registry = ToolRegistry()
        for name in (tools_dict or {}):
            action_class, estimate = (classes or {}).get(name, ("READ", 0))
            registry.register(name, action_class, estimate, backend)
        tools = ToolWrapper(registry, receipts=self.receipts,
                            ledger=self.ledger, stage="trusted", task_id="t1",
                            agent_id="spec")
        brief = kw.pop("brief", "brief")
        estimate = kw.pop("model_estimate_nano", 0)
        spec = Specialist("spec", FakeModel(script), tools,
                          model_estimate_nano=estimate, **kw)
        return spec.investigate("t1", brief)

    def _wired(self, script, tools_dict, **kw):
        """(Specialist, FakeModel) pair, exposing the model for assertions
        on what it was shown."""
        backend = FakeTools(tools_dict)
        registry = ToolRegistry()
        for name in tools_dict:
            registry.register(name, "READ", 0, backend)
        tools = ToolWrapper(registry, receipts=self.receipts,
                            ledger=self.ledger, stage="trusted", task_id="t1",
                            agent_id="spec")
        model = FakeModel(script)
        estimate = kw.pop("model_estimate_nano", 0)
        spec = Specialist("spec", model, tools,
                          model_estimate_nano=estimate, **kw)
        return spec, model

    def test_tool_loop_builds_evidenced_message(self):
        msg = self.investigate_with(
            [{"text": "", "tool_calls": [{"tool": "read", "args": {}}]},
             {"text": "found it",
              "evidence": [{"kind": "file", "ref": "a.txt"}],
              "confidence": 0.9}],
            {"read": "bytes"})
        self.assertFalse(msg.escalated)
        self.assertEqual(msg.claim, "found it")
        self.assertEqual(msg.confidence, 0.9)
        self.assertEqual(len(msg.evidence), 1)
        kinds = [r["action"] for r in self.receipts.read_all()]
        self.assertIn("model_call", kinds)
        self.assertIn("tool_call", kinds)

    def test_low_budget_warns_model_before_final_calls(self):
        spec, model = self._wired(
            [{"text": "", "tool_calls": [{"tool": "read", "args": {}}]},
             {"text": "done", "evidence": [], "confidence": 0.5}],
            {"read": "bytes"}, max_model_calls=2, max_tool_calls=8)
        msg = spec.investigate("t1", "brief")
        self.assertFalse(msg.escalated)
        roles = [m.get("role") for m in model.seen[1]]
        self.assertIn("budget", roles)

    def test_long_tool_result_is_capped_and_marked(self):
        spec, model = self._wired(
            [{"text": "", "tool_calls": [{"tool": "read", "args": {}}]},
             {"text": "done", "evidence": [], "confidence": 0.5}],
            {"read": "x" * (MAX_TOOL_CHARS + 5)})
        spec.investigate("t1", "brief")
        results = [m for m in model.seen[1]
                   if m.get("role") == "tool_result"]
        self.assertEqual(len(results), 1)
        self.assertIn("[truncated: showing", results[0]["text"])
        self.assertTrue(results[0]["text"].startswith("x" * 100))

    def test_short_tool_result_passes_through_unmarked(self):
        spec, model = self._wired(
            [{"text": "", "tool_calls": [{"tool": "read", "args": {}}]},
             {"text": "done", "evidence": [], "confidence": 0.5}],
            {"read": "bytes"})
        spec.investigate("t1", "brief")
        results = [m for m in model.seen[1]
                   if m.get("role") == "tool_result"]
        self.assertEqual(results[0]["text"], "bytes")

    def test_tool_results_carry_their_request(self):
        spec, model = self._wired(
            [{"text": "", "tool_calls": [{"tool": "read", "args": {"p": "a"}}]},
             {"text": "done", "evidence": [], "confidence": 0.5}],
            {"read": "bytes"})
        spec.investigate("t1", "brief")
        results = [m for m in model.seen[1]
                   if m.get("role") == "tool_result"]
        self.assertEqual(results[0]["tool"], "read")
        self.assertEqual(results[0]["args"], {"p": "a"})

    def test_final_call_is_offered_no_tools(self):
        spec, model = self._wired(
            [{"text": "", "tool_calls": [{"tool": "read", "args": {}}]}] * 3,
            {"read": "bytes"}, max_model_calls=2, max_tool_calls=8)
        spec.investigate("t1", "brief")
        self.assertTrue(model.offered[0])
        self.assertEqual(model.offered[1], [])

    def test_call_after_tools_run_out_is_offered_no_tools(self):
        spec, model = self._wired(
            [{"text": "", "tool_calls": [{"tool": "read", "args": {}}]}] * 3,
            {"read": "bytes"}, max_model_calls=6, max_tool_calls=1)
        spec.investigate("t1", "brief")
        self.assertTrue(model.offered[0])
        self.assertEqual(model.offered[1], [])

    def test_model_saying_cannot_verify_escalates(self):
        msg = self.investigate_with([{"text": ESCALATE_TEXT}])
        self.assertTrue(msg.escalated)
        self.assertIn(ESCALATE_TEXT, msg.claim)

    def test_tool_budget_exhaustion_escalates(self):
        script = [{"text": "", "tool_calls": [{"tool": "r", "args": {"n": n}}]}
                  for n in range(6)]
        msg = self.investigate_with(script, {"r": "v"}, max_tool_calls=2)
        self.assertTrue(msg.escalated)
        self.assertIn("tool budget exhausted", msg.claim)

    def test_model_budget_exhaustion_escalates(self):
        script = [{"text": "", "tool_calls": [{"tool": "r", "args": {}}]}] * 6
        msg = self.investigate_with(script, {"r": "v"}, max_model_calls=2,
                       max_tool_calls=10)
        self.assertTrue(msg.escalated)
        self.assertIn("model budget exhausted", msg.claim)

    def test_tool_error_is_fed_back_not_fatal(self):
        msg = self.investigate_with(
            [{"text": "", "tool_calls": [{"tool": "missing", "args": {}}]},
             {"text": ESCALATE_TEXT}],
            {})
        self.assertTrue(msg.escalated)

    def test_scope_denial_is_fed_back_not_fatal(self):
        scope = TaskContract(contract_id="t", version=1, goal="g",
                             deliverable="d",
                             in_scope_paths=("mini_aurion/ledger.py",))
        registry = ToolRegistry()
        registry.register("read_file", "READ", 0,
                          FakeTools({"read_file": "v"}))
        tools = ToolWrapper(registry, receipts=self.receipts,
                            ledger=self.ledger, stage="trusted", task_id="t1",
                            agent_id="spec", scope=scope)
        model = FakeModel(
            [{"text": "", "tool_calls": [
                {"tool": "read_file", "args": {"path": "tests/x.py"}}]},
             {"text": "answered",
              "evidence": [{"kind": "file", "ref": "a"}]}])
        spec = Specialist("spec", model, tools, model_estimate_nano=0)
        msg = spec.investigate("t1", "brief")
        self.assertFalse(msg.escalated)
        self.assertEqual(msg.claim, "answered")
        shown = json.dumps(model.seen[1])
        self.assertIn("SCOPE_EXCLUDED", shown)
        self.assertIn("mini_aurion/ledger.py", shown)

    def test_investigator_prompt_reaches_the_model(self):
        spec, model = self._wired([{"text": "done",
                                    "evidence": [{"kind": "file",
                                                  "ref": "a"}]}],
                                   {}, investigator_prompt="SYS")
        spec.investigate("t1", "brief")
        self.assertEqual(model.systems, ["SYS"])

    def test_approval_need_escalates_with_reason(self):
        registry = ToolRegistry()
        registry.register("w", "WRITE", 0, FakeTools({"w": "v"}))
        tools = ToolWrapper(registry, receipts=self.receipts,
                            ledger=self.ledger, stage="gated", task_id="t1",
                            agent_id="spec")
        spec = Specialist("spec", FakeModel(
            [{"text": "", "tool_calls": [{"tool": "w", "args": {}}]}]), tools,
            model_estimate_nano=0, approval_timeout_seconds=0)
        msg = spec.investigate("t1", "brief")
        self.assertTrue(msg.escalated)
        self.assertIn("needs approval", msg.claim)
        self.assertIn("timed out", msg.claim)

    def test_approved_call_proceeds_after_the_pause(self):
        import threading
        registry = ToolRegistry()
        registry.register("w", "WRITE", 0, FakeTools({"w": "v"}))
        tools = ToolWrapper(registry, receipts=self.receipts,
                            ledger=self.ledger, stage="gated", task_id="t1",
                            agent_id="spec")
        spec = Specialist("spec", FakeModel(
            [{"text": "", "tool_calls": [{"tool": "w", "args": {}}]},
             {"text": "done", "evidence": []}]), tools,
            model_estimate_nano=0, approval_timeout_seconds=10)
        out = {}
        worker = threading.Thread(
            target=lambda: out.setdefault(
                "msg", spec.investigate("t1", "brief")))
        worker.start()
        queue = tools.approvals_path()
        deadline = time.time() + 10
        while time.time() < deadline:
            if queue.exists() and queue.read_text().strip():
                break
            time.sleep(0.05)
        rid = json.loads(queue.read_text().strip().split("\n")[0])["id"]
        with open(queue, "a") as fh:
            fh.write(json.dumps({"id": rid, "status": "approved"}) + "\n")
        worker.join(timeout=15)
        self.assertFalse(worker.is_alive())
        self.assertFalse(out["msg"].escalated)
        self.assertEqual(out["msg"].claim, "done")

    def test_denied_call_escalates_with_reason(self):
        registry = ToolRegistry()
        registry.register("w", "WRITE", 0, FakeTools({"w": "v"}))
        tools = ToolWrapper(registry, receipts=self.receipts,
                            ledger=self.ledger, stage="gated", task_id="t1",
                            agent_id="spec")
        with open(tools.approvals_path(), "w") as fh:
            fh.write("")
        spec = Specialist("spec", FakeModel(
            [{"text": "", "tool_calls": [{"tool": "w", "args": {}}]}]), tools,
            model_estimate_nano=0, approval_timeout_seconds=10)
        import threading
        out = {}
        worker = threading.Thread(
            target=lambda: out.setdefault(
                "msg", spec.investigate("t1", "brief")))
        worker.start()
        queue = tools.approvals_path()
        deadline = time.time() + 10
        rid = None
        while time.time() < deadline and rid is None:
            text = queue.read_text().strip() if queue.exists() else ""
            if text:
                rid = json.loads(text.split("\n")[0])["id"]
            else:
                time.sleep(0.05)
        with open(queue, "a") as fh:
            fh.write(json.dumps({"id": rid, "status": "denied"}) + "\n")
        worker.join(timeout=15)
        self.assertTrue(out["msg"].escalated)
        self.assertIn("denied", out["msg"].claim)

    def test_model_spend_is_reserved_and_settled(self):
        model = FakeModel([{"text": "done",
                            "evidence": [{"kind": "file", "ref": "a"}]}])
        model.last_cost_nano = 400
        tools = ToolWrapper(ToolRegistry(), receipts=self.receipts,
                            ledger=self.ledger, stage="trusted", task_id="t1",
                            agent_id="spec")
        spec = Specialist("spec", model, tools, model_estimate_nano=500)
        msg = spec.investigate("t1", "brief")
        self.assertFalse(msg.escalated)
        self.assertEqual(msg.cost_nano, 400)
        self.assertEqual(self.ledger.spend("t1")["settled_nano"], 400)

    def test_model_receives_registry_definitions(self):
        seen = {}

        class Capturing(FakeModel):
            def complete(self, messages, tools):
                seen["tools"] = tools
                return super().complete(messages, tools)

        registry = ToolRegistry()
        registry.register("r", "READ", 0, FakeTools({"r": "v"}),
                          description="reads files")
        tools = ToolWrapper(registry, receipts=self.receipts,
                            ledger=self.ledger, stage="trusted", task_id="t1",
                            agent_id="spec")
        spec = Specialist("spec", Capturing([{"text": "done",
                                              "evidence": []}]),
                          tools, model_estimate_nano=0)
        spec.investigate("t1", "brief")
        self.assertEqual([t["name"] for t in seen["tools"]], ["r"])
        self.assertEqual(seen["tools"][0]["description"], "reads files")

    def test_refused_model_call_escalates(self):
        tools = ToolWrapper(ToolRegistry(), receipts=self.receipts,
                            ledger=self.ledger, stage="trusted", task_id="t1",
                            agent_id="spec")
        spec = Specialist("spec", FakeModel([{"text": "done"}]), tools,
                          model_estimate_nano=99_999)
        msg = spec.investigate("t1", "brief")
        self.assertTrue(msg.escalated)
        self.assertIn("spend cap refused", msg.claim)


    def test_model_call_receipt_names_provider_and_time(self):
        self.investigate_with([{"text": "done",
                                "evidence": [{"kind": "file",
                                              "ref": "a.txt:1"}]}])
        calls = [r for r in self.receipts.read_all()
                 if r["action"] == "model_call"]
        self.assertTrue(calls)
        for call in calls:
            self.assertEqual(call["detail"]["provider"], "fixture")
            self.assertEqual(call["detail"]["model"], "fake")
            self.assertIsInstance(call["detail"]["duration_ms"], int)
            self.assertGreaterEqual(call["detail"]["duration_ms"], 0)


    def test_model_reserve_carries_provider_identity(self):
        self.investigate_with([{"text": "done",
                                "evidence": [{"kind": "file",
                                              "ref": "a.txt:1"}]}],
                              model_estimate_nano=100)
        tiers = self.ledger.spend("t1")["by_provider"]
        self.assertEqual(tiers, {"fixture/fake": 0})


if __name__ == "__main__":
    unittest.main()
