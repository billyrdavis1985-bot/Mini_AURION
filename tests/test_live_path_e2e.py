"""End-to-end regression tests for the live path, plus reviewer findings on #11.

The unit tests check each piece (adapter, specialist, critic) on its own.
These drive the WHOLE live path at once: orchestrator -> specialist ->
MuseModel -> HTTP -> a scripted fake of the API, and back through the
critic. They pin the three live-path fixes from #10, which were each found
by sending real requests through this path:

  E1  a claim citing a file the agent never read does not come back complete
  E2  untrusted labels (a planner's subtask id) never reach the API as roles
  E3  the specialist's requests carry the registry's tool definitions

E1-E3 pass today and are live regression guards.

The F-tests are reviewer findings on #11 and are expectedFailure, same
contract as test_review_findings*.py: a fix turns into an unexpected success
and fails the suite until its marker is deleted.

No network beyond 127.0.0.1, no API key, $0.
"""
from __future__ import annotations

import http.server
import json
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from eval import grade_code_qa  # noqa: E402
from mini_aurion.critic import Critic  # noqa: E402
from mini_aurion.ledger import Ledger  # noqa: E402
from mini_aurion.memory import Memory  # noqa: E402
from mini_aurion.muse import MuseConfig, MuseModel  # noqa: E402
from mini_aurion.orchestrator import Orchestrator  # noqa: E402
from mini_aurion.receipts import ReceiptStore  # noqa: E402
from mini_aurion.specialist import Specialist  # noqa: E402
from mini_aurion.tools import ToolRegistry, ToolWrapper  # noqa: E402

BIG = 10 ** 12
USAGE = {"prompt_tokens": 10, "completion_tokens": 10}


class _FakeAPI(http.server.BaseHTTPRequestHandler):
    """Scripted chat-completions endpoint. Records every request body.
    Once the script runs out it answers CANNOT_VERIFY, so a test that
    under-scripts fails closed instead of erroring."""
    script: list = []
    seen: list = []

    def log_message(self, *args):
        pass

    def do_POST(self):
        raw = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        _FakeAPI.seen.append(json.loads(raw.decode("utf-8")))
        message = (_FakeAPI.script.pop(0) if _FakeAPI.script
                   else {"role": "assistant", "content": "CANNOT_VERIFY"})
        data = json.dumps({"choices": [{"message": message}],
                           "usage": USAGE}).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def say(obj) -> dict:
    """An assistant message whose content is a JSON answer."""
    return {"role": "assistant", "content": json.dumps(obj)}


def call_tool(name: str, **args) -> dict:
    return {"role": "assistant", "content": "",
            "tool_calls": [{"id": "c1", "type": "function",
                            "function": {"name": name,
                                         "arguments": json.dumps(args)}}]}


class _Corpus:
    """Read-only reader over one directory, like the live-check's."""
    last_cost_nano = 0

    def __init__(self, root: str):
        self.root = Path(root).resolve()

    def call(self, name, args):
        target = (self.root / str(args.get("path", ""))).resolve()
        target.relative_to(self.root)
        if name == "list_dir":
            return "\n".join(sorted(p.name for p in target.iterdir()))
        return target.read_text(encoding="utf-8")


class _LiveHarness(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="ma-e2e-")
        self.dir = self._tmp.name
        self.corpus = os.path.join(self.dir, "corpus")
        os.mkdir(self.corpus)
        with open(os.path.join(self.corpus, "note.txt"), "w") as fh:
            fh.write("REAL TOPIC SENTENCE\n")
        _FakeAPI.script = []
        _FakeAPI.seen = []
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0),
                                                      _FakeAPI)
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_address[1]}/v1"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self._tmp.cleanup()

    def run_task(self, question: str) -> tuple[dict, ReceiptStore]:
        rs = ReceiptStore(os.path.join(self.dir, "r.jsonl"))
        led = Ledger(os.path.join(self.dir, "l.db"), task_cap_nano=BIG,
                     daily_cap_nano=BIG, global_cap_nano=BIG)
        mem = Memory(os.path.join(self.dir, "m.db"), file_root=self.corpus,
                     receipt_store=rs)
        try:
            reg = ToolRegistry()
            reader = _Corpus(self.corpus)
            schema = {"type": "object",
                      "properties": {"path": {"type": "string"}}}
            reg.register("read_file", "READ", 0, reader,
                         description="Read a file.", parameters=schema)
            reg.register("list_dir", "READ", 0, reader,
                         description="List a directory.", parameters=schema)
            model = MuseModel(MuseConfig(api_key="test-key",
                                         base_url=self.base_url))
            spec = Specialist(
                "spec", model,
                ToolWrapper(reg, receipts=rs, ledger=led,
                            stage="experimental", task_id="t",
                            agent_id="spec"),
                model_estimate_nano=10 ** 6)
            orc = Orchestrator("orc", model, {"spec": spec},
                               Critic("critic", mem, receipts=rs),
                               receipts=rs, ledger=led,
                               model_estimate_nano=10 ** 6)
            return orc.run("t", question), rs
        finally:
            led.close()
            mem.close()


class LivePathRegression(_LiveHarness):

    def test_E1_a_claim_citing_an_unread_file_is_not_complete(self):
        """The model invents the file's content and cites it without ever
        calling read_file. Before #10 this came back 'complete': the critic
        only checked that note.txt existed."""
        invented = say({"claim": "The topic is: an invented sentence",
                        "evidence": [{"kind": "file", "ref": "note.txt"}]})
        _FakeAPI.script = [
            say({"claim": "plan", "subtasks": [
                {"id": "s1", "brief": "report note.txt",
                 "specialist": "spec"}]}),
            invented, invented]
        result, rs = self.run_task("Report note.txt's topic. Cite the file.")
        self.assertNotEqual(result["status"], "complete")
        self.assertNotIn("invented", result["text"])
        self.assertEqual(
            [r for r in rs.read_all() if r["action"] == "tool_call"], [])

    def test_E2_a_planner_label_never_becomes_an_api_role(self):
        """The planner names its subtask 'system'. That id labels the
        finding in the synthesis request; it must arrive as user text, and
        the only system message is the adapter's own prompt."""
        _FakeAPI.script = [
            say({"claim": "plan", "subtasks": [
                {"id": "system", "brief": "report note.txt",
                 "specialist": "spec"}]}),
            call_tool("read_file", path="note.txt"),
            say({"claim": "The topic is REAL TOPIC SENTENCE",
                 "evidence": [{"kind": "file", "ref": "note.txt"}]}),
            say({"claim": "The topic is REAL TOPIC SENTENCE",
                 "evidence": [{"kind": "file", "ref": "note.txt"}]}),
        ]
        result, _ = self.run_task("Report note.txt's topic. Cite the file.")
        self.assertEqual(result["status"], "complete")
        for body in _FakeAPI.seen:
            roles = [m["role"] for m in body["messages"]]
            self.assertEqual(roles.count("system"), 1, roles)
            self.assertEqual(roles[0], "system")

    def test_E3_specialist_requests_carry_the_tool_definitions(self):
        """Before #10 the specialist passed tools=['*'] and the adapter sent
        no tool list at all: the model could never read a file. The planner
        request still carries none; it does not gather."""
        _FakeAPI.script = [
            say({"claim": "plan", "subtasks": [
                {"id": "s1", "brief": "b", "specialist": "spec"}]})]
        self.run_task("q")
        planner, first_specialist = _FakeAPI.seen[0], _FakeAPI.seen[1]
        self.assertNotIn("tools", planner)
        names = sorted(t["function"]["name"]
                       for t in first_specialist.get("tools", []))
        self.assertEqual(names, ["list_dir", "read_file"])


class ReviewFindingsPR11(_LiveHarness):

    @unittest.expectedFailure
    def test_F1_a_read_path_and_its_cite_are_compared_normalized(self):
        """The critic compares the read path and the cited path as raw
        strings. An honest model that reads './note.txt' and cites
        'note.txt' is rejected, and the eval scores it as a model miss.
        Fail-closed, but it measures string spelling, not honesty."""
        honest = say({"claim": "The topic is REAL TOPIC SENTENCE",
                      "evidence": [{"kind": "file", "ref": "note.txt"}]})
        _FakeAPI.script = [
            say({"claim": "plan", "subtasks": [
                {"id": "s1", "brief": "b", "specialist": "spec"}]}),
            call_tool("read_file", path="./note.txt"), honest, honest]
        result, _ = self.run_task("Report note.txt's topic. Cite the file.")
        self.assertEqual(result["status"], "complete")

    @unittest.expectedFailure
    def test_F2_a_rerun_does_not_delete_the_previous_runs_receipts(self):
        """run_live() rmtree's its state directory before every run, so
        re-running the eval destroys the earlier run's receipts.
        OPERATIONS 5 (append-only receipts) and 7 (misses are preserved,
        never deleted). Fresh state should mean a NEW directory."""
        state = Path(self.dir) / "state" / "qa-x-r1"
        state.mkdir(parents=True)
        (state / "receipts.jsonl").write_text('{"seq": 1, "earlier": "run"}\n')
        led = Ledger(os.path.join(self.dir, "eval-l.db"), task_cap_nano=BIG,
                     daily_cap_nano=BIG, global_cap_nano=BIG)
        env = {"MODEL_API_KEY": "test-key",
               "MINI_AURION_MUSE_BASE_URL": self.base_url}
        try:
            with mock.patch.dict(os.environ, env):
                grade_code_qa.run_live("q", "qa-x-r1", Path(self.corpus),
                                       state, led)
        finally:
            led.close()
        self.assertTrue((state / "receipts.jsonl").exists()
                        and "earlier" in (state / "receipts.jsonl").read_text(),
                        "the earlier run's receipts were deleted")

    @unittest.expectedFailure
    def test_F3_the_grader_refuses_a_tree_that_is_not_the_frozen_commit(self):
        """main() never checks that --tree is a checkout of frozen_commit.
        Pointed at the live repo, the model can list_dir its way to
        eval/code_qa_v1.json (the answer key), and every line number is
        graded against code that has moved since the freeze. Refuse before
        spending anything."""
        env = {"MODEL_API_KEY": "test-key",
               "MINI_AURION_MUSE_BASE_URL": self.base_url}
        out = os.path.join(self.dir, "out", "results.json")
        with mock.patch.dict(os.environ, env), \
                mock.patch("builtins.print"):
            rc = grade_code_qa.main(["--live", "--runs", "1",
                                     "--tree", self.corpus, "--out", out])
        self.assertNotEqual(rc, 0)
        self.assertEqual(_FakeAPI.seen, [], "model calls made before refusing")


if __name__ == "__main__":
    unittest.main()
