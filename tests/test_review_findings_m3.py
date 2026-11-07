"""Reviewer findings on #15-#17, written as tests that FAIL today.

Same contract as the other test_review_findings*.py files: expectedFailure
keeps the suite green; a fix turns into an unexpected success and fails the
suite until its marker is deleted.

No network beyond 127.0.0.1, no API key, $0.
"""
from __future__ import annotations

import http.server
import json
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from eval.grade_code_qa import TreeReader, tree_clean  # noqa: E402
from mini_aurion.muse import MuseConfig, MuseModel  # noqa: E402
from mini_aurion.specialist import MAX_TOOL_CHARS  # noqa: E402


class _Capture(http.server.BaseHTTPRequestHandler):
    seen: list = []

    def log_message(self, *args):
        pass

    def do_POST(self):
        raw = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        _Capture.seen.append(json.loads(raw.decode("utf-8")))
        data = json.dumps({"choices": [{"message": {
            "role": "assistant", "content": "CANNOT_VERIFY"}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1}}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


class AdapterTruncation(unittest.TestCase):

    def setUp(self):
        _Capture.seen = []
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0),
                                                      _Capture)
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       daemon=True)
        self.thread.start()
        port = self.server.server_address[1]
        self.model = MuseModel(MuseConfig(
            api_key="test-key", base_url=f"http://127.0.0.1:{port}/v1"))

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    @unittest.expectedFailure
    def test_G1_a_tool_result_the_specialist_kept_reaches_the_model(self):
        """#17 lifted the specialist's per-result cap to 12000 and promised
        truncation is 'always marked, never silent'. muse._api_message then
        cuts every message at 8000 with no marker, so the model never sees
        the specialist's text past 8000 and is never told it is missing.
        Either the adapter carries what the specialist kept, or it marks
        its own cut."""
        text = "x" * (MAX_TOOL_CHARS - 20) + "TAIL-SENTINEL"
        self.model.complete([{"role": "tool_result", "text": text}], [])
        sent = _Capture.seen[0]["messages"][-1]["content"]
        self.assertTrue("TAIL-SENTINEL" in sent or "truncated" in sent,
                        f"silently cut to {len(sent)} chars")


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(cwd), *args], check=True,
                   capture_output=True, text=True)


class IgnoredFilesInTheEvalTree(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="ma-tree-")
        self.tree = Path(self._tmp.name)
        _git(self.tree, "init", "-q")
        _git(self.tree, "config", "user.email", "t@example.invalid")
        _git(self.tree, "config", "user.name", "t")
        (self.tree / ".gitignore").write_text("var/\n")
        (self.tree / "code.py").write_text("def f():\n    pass\n")
        _git(self.tree, "add", "-A")
        _git(self.tree, "commit", "-q", "-m", "frozen")
        # What live-check leaves behind if it is ever run inside the tree.
        leaked = self.tree / "var" / "live-check"
        leaked.mkdir(parents=True)
        (leaked / "answers.json").write_text('{"answer": "leaked"}')

    def tearDown(self):
        self._tmp.cleanup()

    @unittest.expectedFailure
    def test_G2_ignored_files_are_neither_clean_nor_visible(self):
        """tree_clean() runs `git status --porcelain`, which hides ignored
        paths, and the frozen tree ignores var/ (where live-check writes).
        The tree passes as clean while list_dir shows var/ to the model.
        Either the clean check sees ignored files, or the reader only
        exposes files tracked at the frozen commit."""
        reader = TreeReader(self.tree)
        try:
            listing = reader.call("list_dir", {"path": "."})
            leaked = reader.call("read_file",
                                 {"path": "var/live-check/answers.json"})
        except Exception:
            listing, leaked = "", ""
        visible = "var" in listing or "leaked" in leaked
        self.assertFalse(tree_clean(self.tree) and visible,
                         "tree passes as clean while the model can read var/")


if __name__ == "__main__":
    unittest.main()
