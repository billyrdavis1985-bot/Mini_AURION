import os
import tempfile
import unittest

from mini_aurion.critic import Critic
from mini_aurion.fetcher import write_snapshot
from mini_aurion.memory import Memory
from mini_aurion.messages import AgentMessage
from mini_aurion.receipts import ReceiptStore


class CriticTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="ma-critic-")
        self.root = os.path.join(self.tmp.name, "corpus")
        os.mkdir(self.root)
        with open(os.path.join(self.root, "a.txt"), "w") as fh:
            fh.write("real file content here")
        with open(os.path.join(self.root, "other.txt"), "w") as fh:
            fh.write("also real content here")
        self.receipts = ReceiptStore(os.path.join(self.tmp.name, "r.jsonl"))
        self.mem = Memory(os.path.join(self.tmp.name, "m.sqlite3"),
                          file_root=self.root,
                          snapshot_dir=self.tmp.name,
                          mechanical_checks=frozenset({"critic"}))
        self.critic = Critic("critic", self.mem, receipts=self.receipts)
        self.page_url = "https://example.com/x"
        self.page_snap, _ = write_snapshot(
            self.tmp.name, url=self.page_url, final_url=self.page_url,
            status=200, content_type="text/plain",
            text="Container rates rose 18% in October.")

    def tearDown(self):
        self.mem.close()
        self.tmp.cleanup()

    def seed_read(self, path, task_id="t1"):
        begun = self.receipts.begin(agent_id="spec", task_id=task_id,
                                    action="tool_call", tool="read_file",
                                    detail={"args": {"path": path}})
        self.receipts.settle(begun["seq"], 0)

    def seed_fetch(self, url, snapshot, task_id="t1", ok=True):
        begun = self.receipts.begin(agent_id="spec", task_id=task_id,
                                    action="tool_call", tool="fetch_url",
                                    detail={"args": {"url": url}})
        self.receipts.settle(begun["seq"], 0 if ok else 1,
                             detail={"snapshot": snapshot})

    def msg(self, **kw):
        base = {"sender": "spec", "task_id": "t1", "claim": "X"}
        base.update(kw)
        return AgentMessage(**base)

    def test_sound_citations_pass(self):
        self.seed_read("a.txt")
        self.seed_fetch(self.page_url, self.page_snap)
        result = self.critic.check(self.msg(
            evidence=[{"kind": "file", "ref": "a.txt:1",
 "quote": "real file content"},
                      {"kind": "url", "ref": self.page_url,
                       "snapshot": self.page_snap,
                       "quote": "rates rose 18%"}]))
        self.assertEqual(result["verdict"], "pass")
        self.assertEqual(result["catches"], [])

    def test_cited_but_unread_file_fails(self):
        result = self.critic.check(self.msg(
            evidence=[{"kind": "file", "ref": "other.txt:1",
 "quote": "also real content"}]))
        self.assertEqual(result["verdict"], "fail")
        self.assertTrue(any("never read" in c for c in result["catches"]))

    def test_cited_but_unfetched_url_fails(self):
        result = self.critic.check(self.msg(
            evidence=[{"kind": "url", "ref": self.page_url,
                       "snapshot": self.page_snap,
                       "quote": "rates rose 18%"}]))
        self.assertEqual(result["verdict"], "fail")
        self.assertTrue(any("never fetched" in c
                            for c in result["catches"]))

    def test_fetch_of_another_snapshot_fails(self):
        other, _ = write_snapshot(
            self.tmp.name, url=self.page_url, final_url=self.page_url,
            status=200, content_type="text/plain",
            text="Container rates rose 18% in October. Updated.")
        self.seed_fetch(self.page_url, other)
        result = self.critic.check(self.msg(
            evidence=[{"kind": "url", "ref": self.page_url,
                       "snapshot": self.page_snap,
                       "quote": "rates rose 18%"}]))
        self.assertEqual(result["verdict"], "fail")
        self.assertTrue(any("never fetched" in c
                            for c in result["catches"]))

    def test_failed_fetch_is_not_evidence(self):
        self.seed_fetch(self.page_url, self.page_snap, ok=False)
        result = self.critic.check(self.msg(
            evidence=[{"kind": "url", "ref": self.page_url,
                       "snapshot": self.page_snap,
                       "quote": "rates rose 18%"}]))
        self.assertEqual(result["verdict"], "fail")
        self.assertTrue(any("never fetched" in c
                            for c in result["catches"]))

    def test_spelling_variants_match(self):
        self.seed_read("./a.txt")
        result = self.critic.check(self.msg(
            evidence=[{"kind": "file", "ref": "a.txt:1",
 "quote": "real file content"}]))
        self.assertEqual(result["verdict"], "pass")

    def test_read_in_another_task_does_not_count(self):
        self.seed_read("a.txt", task_id="other-task")
        result = self.critic.check(self.msg(
            evidence=[{"kind": "file", "ref": "a.txt:1",
 "quote": "real file content"}]))
        self.assertEqual(result["verdict"], "fail")

    def test_missing_evidence_fails(self):
        result = self.critic.check(self.msg())
        self.assertEqual(result["verdict"], "fail")
        self.assertEqual(len(result["catches"]), 1)
        self.assertEqual(self.critic.catches, 1)

    def test_fabricated_citation_fails_with_catch(self):
        result = self.critic.check(self.msg(
            evidence=[{"kind": "file", "ref": "ghost.txt:1"}]))
        self.assertEqual(result["verdict"], "fail")
        self.assertIn("not found", result["catches"][0])

    def test_escalation_is_acknowledged_not_graded(self):
        result = self.critic.check(self.msg(claim="CANNOT_VERIFY: nothing",
                                            escalated=True))
        self.assertEqual(result["verdict"], "escalated")

    def url_cite(self, **over):
        cite = {"kind": "url", "ref": self.page_url,
                "snapshot": self.page_snap, "quote": "rates rose 18%"}
        cite.update(over)
        return cite

    def test_findings_fact_passes_assessment_is_judgment(self):
        self.seed_fetch(self.page_url, self.page_snap)
        result = self.critic.check_findings(self.msg(
            evidence=[],
            findings=[
                {"type": "fact", "claim": "Rates rose.",
                 "evidence": [self.url_cite()]},
                {"type": "assessment", "claim": "Bad trend.",
                 "evidence": [self.url_cite()]}]))
        self.assertEqual(result["verdict"], "pass")
        self.assertEqual(
            [one["verdict"] for one in result["findings"]],
            ["pass", "judgment"])

    def test_findings_untyped_and_uncited_fail(self):
        self.seed_fetch(self.page_url, self.page_snap)
        result = self.critic.check_findings(self.msg(
            evidence=[],
            findings=[
                {"claim": "No type.", "evidence": [self.url_cite()]},
                {"type": "fact", "claim": "No cite.",
                 "evidence": []}]))
        self.assertEqual(result["verdict"], "fail")
        self.assertIn("no known type",
                      " ".join(result["findings"][0]["catches"]))
        self.assertIn("without citation",
                      " ".join(result["findings"][1]["catches"]))

    def test_findings_bad_evidence_fails(self):
        self.seed_fetch(self.page_url, self.page_snap)
        result = self.critic.check_findings(self.msg(
            evidence=[],
            findings=[{"type": "fact", "claim": "Made up.",
                       "evidence": [self.url_cite(
                           quote="something else entirely")]}]))
        self.assertEqual(result["verdict"], "fail")
        self.assertIn("quote not in snapshot",
                      " ".join(result["catches"]))

    def test_findings_unfetched_url_fails(self):
        result = self.critic.check_findings(self.msg(
            evidence=[],
            findings=[{"type": "fact", "claim": "Rates rose.",
                       "evidence": [self.url_cite()]}]))
        self.assertEqual(result["verdict"], "fail")
        self.assertIn("never fetched", " ".join(result["catches"]))

    def test_findings_empty_and_escalated(self):
        result = self.critic.check_findings(self.msg(evidence=[],
                                                     findings=[]))
        self.assertEqual(result["verdict"], "fail")
        self.assertIn("no findings filed", result["catches"])
        escalated = self.msg(evidence=[], findings=[],
                             escalated=True,
                             claim="CANNOT_VERIFY: why [brief]")
        result = self.critic.check_findings(escalated)
        self.assertEqual(result["verdict"], "escalated")

    def test_findings_forecast_passes_for_registration(self):
        self.seed_fetch(self.page_url, self.page_snap)
        result = self.critic.check_findings(self.msg(
            evidence=[],
            findings=[{"type": "forecast", "claim": "Rates will rise.",
                       "probability": 0.7, "resolves_at": "2027-06-01",
                       "criterion": "index above 100",
                       "evidence": [self.url_cite()]}]))
        self.assertEqual(result["verdict"], "pass")
        self.assertEqual(result["findings"][0]["verdict"], "pass")

    def test_critic_has_no_tools_canary(self):
        self.assertFalse(hasattr(self.critic, "tools"))
        self.assertFalse(hasattr(self.critic, "call"))
        # The unfetched URL must fail: proof the critic did not go fetch it.
        result = self.critic.check(self.msg(
            evidence=[{"kind": "url", "ref": "https://unfetched.test/z"}]))
        self.assertEqual(result["verdict"], "fail")

    def test_file_note_writes_and_verifies(self):
        self.seed_read("a.txt")
        entry = self.critic.file_note(
            "t1", "spec", "Rates rose.",
            [{"kind": "file", "ref": "a.txt:1",
              "quote": "real file content"}])
        self.assertEqual(entry["status"], "verified")
        self.assertEqual(entry["verified_by"], "critic")
        self.assertEqual([n["id"] for n in self.mem.recall()],
                         [entry["id"]])
        writes = [r for r in self.receipts.read_all()
                  if r["action"] in ("memory_write", "memory_verify")]
        self.assertEqual(len(writes), 2)
        self.assertTrue(all(r["outcome"] == "ok" for r in writes))

    def test_file_note_failure_receipts_and_returns_none(self):
        entry = self.critic.file_note(
            "t1", "spec", "Ghost claim.",
            [{"kind": "file", "ref": "missing.txt:1"}])
        self.assertIsNone(entry)
        failed = [r for r in self.receipts.read_all()
                  if r["action"] == "memory_verify"
                  and r["outcome"] == "failed"]
        self.assertEqual(len(failed), 1)
        self.assertIn("STALE_EVIDENCE", failed[0]["detail"]["error"])
        self.assertEqual(self.mem.recall(), [])

    def test_author_cannot_verify_its_own_note(self):
        from mini_aurion.memory import MemoryError
        self.seed_read("a.txt")
        entry = self.mem.write(
            "spec", "Rates rose.",
            [{"kind": "file", "ref": "a.txt:1",
              "quote": "real file content"}], task_id="t1")
        with self.assertRaises(MemoryError) as ctx:
            self.mem.verify(entry["id"], "spec")
        self.assertEqual(ctx.exception.code, "SELF_VERIFY")


if __name__ == "__main__":
    unittest.main()
