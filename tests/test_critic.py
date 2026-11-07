import os
import tempfile
import unittest

from mini_aurion.critic import Critic
from mini_aurion.memory import Memory
from mini_aurion.messages import AgentMessage
from mini_aurion.receipts import ReceiptStore


class CriticTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="ma-critic-")
        self.root = os.path.join(self.tmp.name, "corpus")
        os.mkdir(self.root)
        with open(os.path.join(self.root, "a.txt"), "w") as fh:
            fh.write("real")
        with open(os.path.join(self.root, "other.txt"), "w") as fh:
            fh.write("also real")
        self.receipts = ReceiptStore(os.path.join(self.tmp.name, "r.jsonl"))
        self.mem = Memory(os.path.join(self.tmp.name, "m.sqlite3"),
                          file_root=self.root,
                          fetched_urls={"https://example.com/x"})
        self.critic = Critic("critic", self.mem, receipts=self.receipts)

    def tearDown(self):
        self.mem.close()
        self.tmp.cleanup()

    def seed_read(self, path, task_id="t1"):
        begun = self.receipts.begin(agent_id="spec", task_id=task_id,
                                    action="tool_call", tool="read_file",
                                    detail={"args": {"path": path}})
        self.receipts.settle(begun["seq"], 0)

    def msg(self, **kw):
        base = {"sender": "spec", "task_id": "t1", "claim": "X"}
        base.update(kw)
        return AgentMessage(**base)

    def test_sound_citations_pass(self):
        self.seed_read("a.txt")
        result = self.critic.check(self.msg(
            evidence=[{"kind": "file", "ref": "a.txt"},
                      {"kind": "url", "ref": "https://example.com/x"}]))
        self.assertEqual(result["verdict"], "pass")
        self.assertEqual(result["catches"], [])

    def test_cited_but_unread_file_fails(self):
        result = self.critic.check(self.msg(
            evidence=[{"kind": "file", "ref": "other.txt"}]))
        self.assertEqual(result["verdict"], "fail")
        self.assertTrue(any("never read" in c for c in result["catches"]))

    def test_spelling_variants_match(self):
        self.seed_read("./a.txt")
        result = self.critic.check(self.msg(
            evidence=[{"kind": "file", "ref": "a.txt"}]))
        self.assertEqual(result["verdict"], "pass")

    def test_read_in_another_task_does_not_count(self):
        self.seed_read("a.txt", task_id="other-task")
        result = self.critic.check(self.msg(
            evidence=[{"kind": "file", "ref": "a.txt"}]))
        self.assertEqual(result["verdict"], "fail")

    def test_missing_evidence_fails(self):
        result = self.critic.check(self.msg())
        self.assertEqual(result["verdict"], "fail")
        self.assertEqual(len(result["catches"]), 1)
        self.assertEqual(self.critic.catches, 1)

    def test_fabricated_citation_fails_with_catch(self):
        result = self.critic.check(self.msg(
            evidence=[{"kind": "file", "ref": "ghost.txt"}]))
        self.assertEqual(result["verdict"], "fail")
        self.assertIn("not found", result["catches"][0])

    def test_escalation_is_acknowledged_not_graded(self):
        result = self.critic.check(self.msg(claim="CANNOT_VERIFY: nothing",
                                            escalated=True))
        self.assertEqual(result["verdict"], "escalated")

    def test_critic_has_no_tools_canary(self):
        self.assertFalse(hasattr(self.critic, "tools"))
        self.assertFalse(hasattr(self.critic, "call"))
        # The unfetched URL must fail: proof the critic did not go fetch it.
        result = self.critic.check(self.msg(
            evidence=[{"kind": "url", "ref": "https://unfetched.test/z"}]))
        self.assertEqual(result["verdict"], "fail")


if __name__ == "__main__":
    unittest.main()
