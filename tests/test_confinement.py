"""Store confinement: WRITE tools can never address the state dir.

The receipts, ledger, memory, and proc files are what the agent is
judged by; a WRITE whose "path" resolves inside the task's store dir
is denied with a receipt, before policy is even consulted. No WRITE
tool ships yet, so these tests register a fixture one — the gate
guards a class with zero members, deliberately.
"""
import os
import tempfile
import unittest

from mini_aurion.ledger import Ledger
from mini_aurion.receipts import ReceiptStore
from mini_aurion.tools import ToolDenied, ToolRegistry, ToolWrapper

CAPS = {"task_cap_nano": 100_000_000, "daily_cap_nano": 500_000_000,
        "global_cap_nano": 1_000_000_000}


class RecordingBackend:
    def __init__(self):
        self.calls = []
        self.last_cost_nano = 0
        self.last_receipt_detail = None

    def call(self, name, args):
        self.calls.append((name, dict(args)))
        return {"ok": True}


class ConfinementTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="ma-conf-")
        self.state = os.path.join(self.tmp.name, "state")
        os.mkdir(self.state)
        self.receipts = ReceiptStore(os.path.join(self.state, "r.jsonl"))
        self.ledger = Ledger(os.path.join(self.state, "l.sqlite3"),
                             **CAPS)
        self.backend = RecordingBackend()
        registry = ToolRegistry()
        registry.register("write_file", "WRITE", 0, self.backend,
                          description="fixture WRITE tool")
        registry.register("read_file", "READ", 0, self.backend,
                          description="fixture READ tool")
        self.registry = registry

    def tearDown(self):
        self.ledger.close()
        self.tmp.cleanup()

    def _wrapper(self, **kwargs):
        kwargs.setdefault("state_dir", self.state)
        return ToolWrapper(self.registry, receipts=self.receipts,
                           ledger=self.ledger, stage="trusted",
                           task_id="t1", agent_id="spec", **kwargs)

    def _denials(self):
        return [r for r in self.receipts.read_all()
                if r["action"] == "denied"]

    def test_absolute_store_path_denied_before_backend(self):
        wrapper = self._wrapper()
        target = os.path.join(self.state, "receipts.jsonl")
        with self.assertRaises(ToolDenied) as ctx:
            wrapper.call("write_file", {"path": target})
        self.assertEqual(ctx.exception.code, "STORE_PROTECTED")
        self.assertEqual(self.backend.calls, [])
        denials = self._denials()
        self.assertEqual(len(denials), 1)
        self.assertIn("STORE_PROTECTED", denials[0]["detail"]["reason"])

    def test_each_store_file_denied(self):
        wrapper = self._wrapper()
        for name in ("receipts.jsonl", "receipts.jsonl.anchor",
                     "ledger.sqlite3", "memory.sqlite3", "proc.json"):
            target = os.path.join(self.state, name)
            with self.assertRaises(ToolDenied, msg=name):
                wrapper.call("write_file", {"path": target})
        self.assertEqual(self.backend.calls, [])

    def test_state_dir_itself_denied(self):
        wrapper = self._wrapper()
        with self.assertRaises(ToolDenied):
            wrapper.call("write_file", {"path": self.state})
        self.assertEqual(self.backend.calls, [])

    def test_cwd_relative_spelling_denied(self):
        wrapper = self._wrapper()
        rel = os.path.relpath(os.path.join(self.state, "r.jsonl"))
        with self.assertRaises(ToolDenied):
            wrapper.call("write_file", {"path": rel})
        self.assertEqual(self.backend.calls, [])

    def test_sibling_path_allowed(self):
        wrapper = self._wrapper()
        outside = os.path.join(self.tmp.name, "notes.txt")
        reply = wrapper.call("write_file", {"path": outside})
        self.assertEqual(reply["result"], {"ok": True})
        self.assertEqual(len(self.backend.calls), 1)
        self.assertEqual(self._denials(), [])

    def test_sibling_prefix_is_not_inside(self):
        # state-evil shares a string prefix with state; it is not
        # inside it. Startswith without the separator would misfire.
        wrapper = self._wrapper()
        outside = self.state + "-evil"
        wrapper.call("write_file", {"path": outside})
        self.assertEqual(len(self.backend.calls), 1)

    def test_read_tools_ignore_the_gate(self):
        wrapper = self._wrapper()
        target = os.path.join(self.state, "r.jsonl")
        wrapper.call("read_file", {"path": target})
        self.assertEqual(len(self.backend.calls), 1)
        self.assertEqual(self._denials(), [])

    def test_gate_off_without_state_dir(self):
        wrapper = self._wrapper(state_dir=None)
        target = os.path.join(self.state, "r.jsonl")
        wrapper.call("write_file", {"path": target})
        self.assertEqual(len(self.backend.calls), 1)


if __name__ == "__main__":
    unittest.main()
