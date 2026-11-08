"""Reviewer findings on #26 (tool-result labels + duplicate-call cache),
kept as expected-failure tests.

Each test fails today. When its fix lands, unittest reports "unexpected
success" and CI fails until that test's marker is deleted; the test then
stays as a regression guard, per the review-findings contract.

No network, no API key, $0.
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mini_aurion.critic import Critic  # noqa: E402
from mini_aurion.fixture import FakeModel, FakeTools  # noqa: E402
from mini_aurion.ledger import Ledger  # noqa: E402
from mini_aurion.memory import Memory  # noqa: E402
from mini_aurion.muse import MAX_MESSAGE_CHARS, _api_message  # noqa: E402
from mini_aurion.orchestrator import Orchestrator  # noqa: E402
from mini_aurion.receipts import ReceiptStore  # noqa: E402
from mini_aurion.specialist import Specialist  # noqa: E402
from mini_aurion.tools import ToolRegistry, ToolWrapper  # noqa: E402

CAP = 10 ** 9


class _Env(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="ma-j-")
        self.base = Path(self._tmp.name)
        self.receipts = ReceiptStore(self.base / "r.jsonl")
        self.ledger = Ledger(self.base / "l.sqlite3", task_cap_nano=CAP,
                             daily_cap_nano=CAP, global_cap_nano=CAP)

    def tearDown(self):
        self.ledger.close()
        self._tmp.cleanup()

    def wrapper(self, tools, stage="trusted") -> ToolWrapper:
        registry = ToolRegistry()
        for name, action_class, backend in tools:
            registry.register(name, action_class, 0, backend)
        return ToolWrapper(registry, receipts=self.receipts,
                           ledger=self.ledger, stage=stage, task_id="t",
                           agent_id="spec")


class CacheOnlyRepeatsReads(_Env):

    @unittest.expectedFailure
    def test_J1_a_repeated_write_runs_again(self):
        """The cache serves every action class. A second identical WRITE
        (append a line, send a message) never runs, yet its receipt settles
        'ok'. That is a settle from no return code (OPERATIONS section 5).
        #26's own test_repeat_call_is_served_cached_free_and_marked
        exercises exactly this with a WRITE tool, so the behaviour is
        currently pinned as intended. No WRITE tools are registered today,
        so nothing live is affected yet.

        Fix: cache READ-class tools only (spec['class'] == 'READ'), and
        clear the cache after any non-READ call so a later read is not
        served stale. Update #26's WRITE test to use a READ tool."""
        backend = FakeTools({"append_line": "appended"})
        tools = self.wrapper([("append_line", "WRITE", backend)])
        tools.call("append_line", {"line": "x"})
        tools.call("append_line", {"line": "x"})
        self.assertEqual(len(backend.invocations), 2,
                         "second WRITE skipped but receipted as settled ok")

    @unittest.expectedFailure
    def test_J1_a_human_approved_repeat_runs(self):
        """Same cause, at `gated`: the operator explicitly approves a second
        identical WRITE, and the cache swallows it. Approval is the one
        signal that must never be dropped silently."""
        backend = FakeTools({"append_line": "appended"})
        tools = self.wrapper([("append_line", "WRITE", backend)],
                             stage="gated")
        tools.call("append_line", {"line": "x"}, approved=True)
        tools.call("append_line", {"line": "x"}, approved=True)
        self.assertEqual(len(backend.invocations), 2,
                         "an approved WRITE never ran")


class DuplicateNoticeIsTrue(_Env):

    @unittest.expectedFailure
    def test_J2_the_retry_is_not_told_it_already_has_the_content(self):
        """The cache lives on the ToolWrapper, which is per task. The
        orchestrator's retry calls investigate() again on the same
        specialist, which starts a FRESH message history. Its first read
        returns the content plus '[duplicate: you already requested
        exactly this; no new information]', which is false: nothing in
        this context holds it. A model that believes the notice stops
        reading or escalates.

        Fix: scope the cache to one investigate() call (e.g. the
        specialist clears it at the start), or make the notice
        context-neutral."""
        root = self.base / "tree"
        root.mkdir()
        (root / "a.txt").write_text("hello\n")
        tools = self.wrapper([("read_file", "READ",
                               FakeTools({"read_file": "1: hello"}))])
        memory = Memory(self.base / "m.sqlite3", file_root=root,
                        receipt_store=self.receipts)
        self.addCleanup(memory.close)
        read = {"tool": "read_file", "args": {"path": "a.txt"}}
        model = FakeModel([
            {"text": "plan"},
            {"text": "", "tool_calls": [read]},
            {"text": "c", "evidence": [{"kind": "file", "ref": "no.txt"}]},
            {"text": "", "tool_calls": [read]},          # retry, fresh context
            {"text": "c", "evidence": [{"kind": "file", "ref": "a.txt"}]},
            {"text": "s", "evidence": [{"kind": "file", "ref": "a.txt"}]},
        ])
        spec = Specialist("spec", model, tools, model_estimate_nano=0)
        Orchestrator("orc", model, {"spec": spec},
                     Critic("critic", memory, receipts=self.receipts),
                     receipts=self.receipts, ledger=self.ledger,
                     model_estimate_nano=0).run("t", "q")
        retry_context = model.seen[4]
        self.assertEqual([m["role"] for m in retry_context],
                         ["brief", "tool_result"])
        self.assertNotIn("[duplicate", retry_context[1]["text"],
                         "fresh context told it already has this content")

    @unittest.expectedFailure
    def test_J3_the_duplicate_notice_survives_a_long_result(self):
        """The notice is appended AFTER the content, and the specialist
        then cuts at MAX_TOOL_CHARS. For any result over 12,000 chars
        (receipts.py at the frozen commit is 12,693 numbered) the notice
        is cut off, so the model gets the full file again with no hint it
        is a repeat: the case #26 was written for, on the files where
        repeats cost the most context.

        Fix: put the notice first, or (better, see review) answer a
        repeat with a short pointer instead of the content again."""
        tools = self.wrapper([("read_file", "READ",
                               FakeTools({"read_file": "x" * 13000}))])
        read = {"tool": "read_file", "args": {"path": "big.py"}}
        model = FakeModel([{"text": "", "tool_calls": [read]}] * 2
                          + [{"text": "done", "evidence": [],
                              "confidence": 0.5}])
        Specialist("spec", model, tools,
                   model_estimate_nano=0).investigate("t", "b")
        results = [m for m in model.seen[-1] if m["role"] == "tool_result"]
        self.assertEqual(len(results), 2)
        self.assertIn("[duplicate", results[1]["text"],
                      "the repeat notice was truncated away")


class LabelIsBounded(unittest.TestCase):

    @unittest.expectedFailure
    def test_J4_the_request_label_respects_the_message_cap(self):
        """_api_message truncates the text to MAX_MESSAGE_CHARS, then
        prepends '[tool_result <tool> <args>]'. Args are model-chosen, so
        a 50,000-char argument rides every later model call uncapped.

        Fix: cap the rendered args (e.g. repr cut to 200 chars, marked),
        or apply the cap after the label is added."""
        content = _api_message({"role": "tool_result", "tool": "read_file",
                                "args": {"path": "p" * 50000},
                                "text": "ok"})["content"]
        self.assertLessEqual(len(content), MAX_MESSAGE_CHARS + 400,
                             f"{len(content)} chars in one message")


if __name__ == "__main__":
    unittest.main()
