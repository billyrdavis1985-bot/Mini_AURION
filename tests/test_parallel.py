"""Parallel delegation: chain, caps, attribution, and merge order on threads.

max_workers=1 is the default and keeps the old serial path untouched;
above 1, subtasks delegate on threads while findings still merge in
subtask order. Every test here runs for real on threads ($0 fixtures):
a test that never fails without the locks proves nothing, so the gate
and sleep below force the interleavings the locks exist for.
"""
import os
import tempfile
import threading
import time
import unittest

from mini_aurion.critic import Critic
from mini_aurion.fixture import FakeModel
from mini_aurion.ledger import Ledger, LedgerError
from mini_aurion.memory import Memory
from mini_aurion.orchestrator import Orchestrator
from mini_aurion.receipts import ReceiptStore
from mini_aurion.specialist import Specialist
from mini_aurion.tools import ToolRegistry, ToolWrapper

CAPS = {"task_cap_nano": 100_000_000, "daily_cap_nano": 500_000_000,
        "global_cap_nano": 1_000_000_000}


A_EVIDENCE = [{"kind": "file", "ref": "a.txt:1",
               "quote": "alpha file content"}]
B_EVIDENCE = [{"kind": "file", "ref": "b.txt:1",
               "quote": "beta file content"}]


def _answer(text, ref, quote):
    return {"text": text,
            "evidence": [{"kind": "file", "ref": ref, "quote": quote}],
            "confidence": 0.9}


class GateModel(FakeModel):
    """FakeModel that inverts finish order: the 'slow' side waits for an
    event the 'fast' side sets. Completion order is recorded so the test
    can prove the inversion happened (and the merge still followed the
    plan, not the finish)."""

    def __init__(self, script, *, gate=None, wait=None, done=None, tag=""):
        super().__init__(script)
        self._gate = gate
        self._wait = wait
        self._done = done
        self._tag = tag
        self._done_mu = threading.Lock()

    def complete(self, messages, tools, system_prompt=None):
        if self._wait is not None:
            self._wait.wait(timeout=30)
        reply = super().complete(messages, tools, system_prompt)
        if self._done is not None:
            # Record BEFORE setting the gate: the waiter's append lands
            # strictly after, so completion order is deterministic.
            with self._done_mu:
                self._done.append(self._tag)
            if self._gate is not None:
                self._gate.set()
        elif self._gate is not None:
            self._gate.set()
        return reply


class CostBackend:
    """Backend that reports a different cost per call, then stays in the
    call past the other thread's report. Two threads sharing one wrapper
    must still settle each call with its own cost; without the wrapper
    lock the first thread's read-back deterministically sees the second
    thread's report."""

    def __init__(self):
        self.last_cost_nano = 0
        self.last_receipt_detail = None
        self._mu = threading.Lock()
        self._n = 0

    def call(self, name, args):
        with self._mu:
            self._n += 1
            mine = self._n * 100
        # Report first, return late: the barrier-synced peer reports
        # during this sleep, before our caller reads last_cost_nano.
        self.last_cost_nano = mine
        time.sleep(0.05)
        return {"mine": mine}


class ParallelTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="ma-par-")
        self.root = os.path.join(self.tmp.name, "corpus")
        os.mkdir(self.root)
        with open(os.path.join(self.root, "a.txt"), "w") as fh:
            fh.write("alpha file content here")
        with open(os.path.join(self.root, "b.txt"), "w") as fh:
            fh.write("beta file content here")
        self.receipts = ReceiptStore(os.path.join(self.tmp.name, "r.jsonl"))
        self.ledger = Ledger(os.path.join(self.tmp.name, "l.sqlite3"),
                             **CAPS)
        self.mem = Memory(os.path.join(self.tmp.name, "m.sqlite3"),
                          file_root=self.root,
                          mechanical_checks=frozenset({"critic"}))

    def tearDown(self):
        self.ledger.close()
        self.mem.close()
        self.tmp.cleanup()

    def _specialists(self, scripts):
        out = {}
        for name, script in scripts.items():
            if isinstance(script, FakeModel):
                model = script
            else:
                model = FakeModel(script)
            tools = ToolWrapper(ToolRegistry(), receipts=self.receipts,
                                ledger=self.ledger, stage="trusted",
                                task_id="t1", agent_id=name)
            out[name] = Specialist(name, model, tools,
                                   model_estimate_nano=0)
        return out

    def _seed_read(self, path):
        begun = self.receipts.begin(agent_id="spec", task_id="t1",
                                    action="tool_call", tool="read_file",
                                    detail={"args": {"path": path}})
        self.receipts.settle(begun["seq"], 0)

    def _orc(self, specialists, orc_script, **kwargs):
        return Orchestrator(
            "orc", FakeModel(orc_script), specialists,
            Critic("critic", self.mem, receipts=self.receipts),
            receipts=self.receipts, ledger=self.ledger,
            message_budget=10, model_estimate_nano=0, **kwargs)

    def _dense_seqs(self):
        seqs = [r["seq"] for r in self.receipts.read_all()]
        self.assertEqual(sorted(seqs), list(range(1, len(seqs) + 1)))

    def test_three_workers_keep_chain_order_and_exact_catches(self):
        self._seed_read("a.txt")
        self._seed_read("b.txt")
        specs = self._specialists({
            "alpha": [_answer("A", "a.txt:1", "alpha file content")],
            # Never-read cite: fails once (one catch), then the retry
            # finds the script empty and escalates honestly.
            "beta": [_answer("B", "b.txt:1", "beta file content")],
            "gamma": [_answer("G", "zzz.txt:1", "nothing here ever")]})
        subs = [{"id": "s1", "brief": "b1", "specialist": "alpha"},
                {"id": "s2", "brief": "b2", "specialist": "beta"},
                {"id": "s3", "brief": "b3", "specialist": "gamma"}]
        orc = self._orc(
            specs,
            [{"text": "plan", "subtasks": subs},
             {"text": "A and B",
              "evidence": A_EVIDENCE + B_EVIDENCE}],
            max_workers=3)
        result = orc.run("t1", "question?")
        self.assertEqual(
            [f["subtask"] for f in result["findings"]], ["s1", "s2", "s3"])
        self.assertEqual(result["findings"][2]["status"], "escalated")
        # Partial: the escalated sibling keeps it from completing.
        self.assertEqual(result["status"], "partial")
        # One bad finding, one catch string, counted exactly once
        # across the three delegate threads.
        self.assertEqual(orc.critic.catches, 1)
        # verify() raises on any torn line or broken link.
        self.assertEqual(self.receipts.verify(),
                         len(self.receipts.read_all()))
        self._dense_seqs()
        # Both accepted findings filed verified notes (PR1 survives
        # threads); the escalated one filed nothing.
        notes = [e for e in self.mem.recall(include_unverified=True)
                 if e["task_id"] == "t1"]
        self.assertEqual(len(notes), 2)
        self.assertTrue(all(n["status"] == "verified" for n in notes))

    def test_merge_follows_subtask_order_not_finish_order(self):
        self._seed_read("a.txt")
        self._seed_read("b.txt")
        arrived = threading.Event()
        done: list = []
        specs = self._specialists({
            "slow": GateModel(
                [_answer("SLOW", "a.txt:1", "alpha file content")],
                wait=arrived, done=done, tag="slow"),
            "fast": GateModel(
                [_answer("FAST", "b.txt:1", "beta file content")],
                gate=arrived, done=done, tag="fast")})
        subs = [{"id": "s1", "brief": "b1", "specialist": "slow"},
                {"id": "s2", "brief": "b2", "specialist": "fast"}]
        orc = self._orc(
            specs,
            [{"text": "plan", "subtasks": subs},
             {"text": "SLOW and FAST",
              "evidence": A_EVIDENCE + B_EVIDENCE}],
            max_workers=2)
        result = orc.run("t1", "question?")
        # The inversion really happened...
        self.assertEqual(done, ["fast", "slow"])
        # ...and the merge ignored it.
        self.assertEqual(
            [f["subtask"] for f in result["findings"]], ["s1", "s2"])
        self.assertEqual(result["status"], "complete")
        self._dense_seqs()

    def test_default_is_one_worker_and_matches_parallel_order(self):
        self._seed_read("a.txt")
        self._seed_read("b.txt")
        subs = [{"id": "s1", "brief": "b1", "specialist": "alpha"},
                {"id": "s2", "brief": "b2", "specialist": "beta"}]
        scripts = {"alpha": [_answer("A", "a.txt:1", "alpha file content")],
                   "beta": [_answer("B", "b.txt:1", "beta file content")]}
        orc = self._orc(
            self._specialists(scripts),
            [{"text": "plan", "subtasks": subs},
             {"text": "A and B", "evidence": A_EVIDENCE + B_EVIDENCE}])
        self.assertEqual(orc.max_workers, 1)
        serial = orc.run("t1", "question?")
        self.assertEqual(
            [f["subtask"] for f in serial["findings"]], ["s1", "s2"])
        self.assertEqual(serial["status"], "complete")

    def test_caps_hold_under_contention(self):
        ledger = Ledger(os.path.join(self.tmp.name, "race.sqlite3"),
                        task_cap_nano=1000, daily_cap_nano=10_000,
                        global_cap_nano=100_000)
        self.addCleanup(ledger.close)
        barrier = threading.Barrier(12)
        outcomes: list = []
        mu = threading.Lock()

        def worker():
            barrier.wait(timeout=30)
            try:
                ledger.reserve("t-race", 200)
                ok = True
            except LedgerError:
                ok = False
            with mu:
                outcomes.append(ok)

        threads = [threading.Thread(target=worker) for _ in range(12)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
        # 5 x 200 fills the 1000 cap; the other 7 refuse, exactly.
        self.assertEqual(sorted(outcomes),
                         [False] * 7 + [True] * 5)
        self.assertEqual(
            ledger.spend("t-race")["inflight_nano"], 1000)

    def test_memory_write_verify_clean_under_threads(self):
        barrier = threading.Barrier(8)
        errors: list = []
        mu = threading.Lock()

        def worker(i):
            try:
                barrier.wait(timeout=30)
                entry = self.mem.write(
                    f"author-{i}", f"note {i}",
                    [{"kind": "file", "ref": "a.txt:1",
                      "quote": "alpha file content"}],
                    task_id="t1")
                got = self.mem.verify(entry["id"], "critic",
                                      mechanical=True)
                assert got["status"] == "verified", got
            except Exception as exc:  # noqa: BLE001 - collected, then raised
                with mu:
                    errors.append(exc)

        threads = [threading.Thread(target=worker, args=(i,))
                   for i in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
        self.assertEqual(errors, [])
        notes = [e for e in self.mem.recall(include_unverified=True)
                 if e["task_id"] == "t1"]
        self.assertEqual(len(notes), 8)
        self.assertTrue(all(n["status"] == "verified" for n in notes))

    def test_shared_wrapper_settles_each_call_with_its_own_cost(self):
        registry = ToolRegistry()
        registry.register("probe", "READ", 1000, CostBackend(),
                          description="cost-attribution probe")
        wrapper = ToolWrapper(registry, receipts=self.receipts,
                              ledger=self.ledger, stage="trusted",
                              task_id="t1", agent_id="spec")
        barrier = threading.Barrier(2)
        results: dict = {}
        errors: list = []
        mu = threading.Lock()

        def worker(i):
            try:
                barrier.wait(timeout=30)
                results[i] = wrapper.call("probe", {})
            except Exception as exc:  # noqa: BLE001 - collected, then raised
                with mu:
                    errors.append(exc)

        threads = [threading.Thread(target=worker, args=(i,))
                   for i in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
        self.assertEqual(errors, [])
        self.assertEqual(len(results), 2)
        for reply in results.values():
            # Each reply's settled cost is the cost ITS backend call
            # reported, not the other thread's.
            self.assertEqual(reply["cost_nano"],
                             reply["result"]["mine"])


if __name__ == "__main__":
    unittest.main()
