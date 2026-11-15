import os
import tempfile
import time
import unittest

from mini_aurion.fetcher import write_snapshot
from mini_aurion.memory import Memory, MemoryError
from mini_aurion.receipts import ReceiptStore


class MemoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="ma-memory-")
        self.root = os.path.join(self.tmp.name, "corpus")
        os.mkdir(self.root)
        with open(os.path.join(self.root, "paper.txt"), "w") as fh:
            fh.write("real content")
        self.receipts = ReceiptStore(os.path.join(self.tmp.name, "r.jsonl"))
        begun = self.receipts.begin(agent_id="a", task_id="t", action="act")
        self.rcpt = self.receipts.settle(begun["seq"], 0)
        self.mem = Memory(os.path.join(self.tmp.name, "m.sqlite3"),
                          file_root=self.root, receipt_store=self.receipts,
                          snapshot_dir=self.tmp.name)
        self.page_url = "https://example.com/x"
        self.page_text = "Container rates rose 18% in October."
        self.page_snap, _ = write_snapshot(
            self.tmp.name, url=self.page_url, final_url=self.page_url,
            status=200, content_type="text/plain", text=self.page_text)

    def tearDown(self):
        self.mem.close()
        self.tmp.cleanup()

    def test_real_citations_pass(self):
        entry = self.mem.write("spec", "claim",
                               [{"kind": "file", "ref": "paper.txt:1",
                               "quote": "real content"},
                                {"kind": "receipt", "ref": self.rcpt["seq"]},
                                {"kind": "url", "ref": self.page_url,
                                 "snapshot": self.page_snap,
                                 "quote": "rates rose 18%"}])
        self.assertEqual(entry["status"], "unverified")
        self.assertEqual(entry["reason"], "")

    def test_quote_must_match_its_line(self):
        good = {"kind": "file", "ref": "paper.txt:1",
                "quote": "real content"}
        self.assertEqual(
            self.mem.write("s", "c", [good])["reason"], "")
        bad = dict(good, quote="something else entirely")
        self.assertIn("quote not on its cited line",
                      self.mem.write("s", "c", [bad])["reason"])
        short = dict(good, quote="real")
        self.assertIn("12+ char quote",
                      self.mem.write("s", "c", [short])["reason"])
        long = dict(good, quote="x" * 201)
        self.assertIn("over 200 chars",
                      self.mem.write("s", "c", [long])["reason"])
        bare = {"kind": "file", "ref": "paper.txt:1"}
        self.assertIn("12+ char quote",
                      self.mem.write("s", "c", [bare])["reason"])
        anchored = {"kind": "file", "ref": "paper.txt"}
        self.assertIn("line anchor",
                      self.mem.write("s", "c", [anchored])["reason"])
        far = dict(good, ref="paper.txt:9")
        self.assertIn("out of range",
                      self.mem.write("s", "c", [far])["reason"])

    def test_quote_ignores_indent_not_case(self):
        with open(os.path.join(self.root, "ind.py"), "w") as fh:
            fh.write('    def GetThingNow():\n')
        bare = {"kind": "file", "ref": "ind.py:1",
                "quote": "def GetThingNow():"}
        self.assertEqual(
            self.mem.write("s", "c", [bare])["reason"], "")
        verbatim = dict(bare, quote="    def GetThingNow():")
        self.assertEqual(
            self.mem.write("s", "c", [verbatim])["reason"], "")
        recased = dict(bare, quote="def getthingnow():")
        self.assertIn("quote not on its cited line",
                      self.mem.write("s", "c", [recased])["reason"])
        blank = dict(bare, quote=" " * 12)
        self.assertIn("quote not on its cited line",
                      self.mem.write("s", "c", [blank])["reason"])

    def test_fabricated_file_is_caught(self):
        entry = self.mem.write("spec", "claim",
                               [{"kind": "file", "ref": "nope.txt:1"}])
        self.assertEqual(entry["status"], "unverified")
        self.assertIn("not found", entry["reason"])

    def test_line_anchored_ref_resolves(self):
        entry = self.mem.write("spec", "claim",
                               [{"kind": "file", "ref": "paper.txt:1",
                               "quote": "real content"}])
        self.assertEqual(entry["reason"], "")

    def test_bad_line_anchor_is_caught(self):
        entry = self.mem.write("spec", "claim",
                               [{"kind": "file", "ref": "paper.txt:x"}])
        self.assertIn("bad line anchor", entry["reason"])

    def test_path_escape_is_caught(self):
        entry = self.mem.write("spec", "claim",
                               [{"kind": "file", "ref": "../m.sqlite3:1"}])
        self.assertIn("escapes root", entry["reason"])

    def test_fabricated_receipt_is_caught(self):
        entry = self.mem.write("spec", "claim",
                               [{"kind": "receipt", "ref": 999}])
        self.assertIn("unknown receipt seq", entry["reason"])

    def test_log_line_receipt_is_not_evidence(self):
        log = self.receipts.append(agent_id="a", task_id="t", action="note")
        entry = self.mem.write("spec", "claim",
                               [{"kind": "receipt", "ref": log["seq"]}])
        self.assertIn("not settled work", entry["reason"])

    def test_other_tasks_receipt_is_not_evidence(self):
        entry = self.mem.write("spec", "claim",
                               [{"kind": "receipt", "ref": self.rcpt["seq"]}],
                               task_id="other-task")
        self.assertIn("belongs to task", entry["reason"])

    def test_url_quote_must_match_its_snapshot(self):
        good = {"kind": "url", "ref": self.page_url,
                "snapshot": self.page_snap, "quote": "rates rose 18%"}
        self.assertEqual(
            self.mem.write("s", "c", [good])["reason"], "")
        bad = dict(good, quote="something else entirely")
        self.assertIn("quote not in snapshot",
                      self.mem.write("s", "c", [bad])["reason"])
        short = dict(good, quote="rates")
        self.assertIn("12+ char quote",
                      self.mem.write("s", "c", [short])["reason"])
        long = dict(good, quote="x" * 201)
        self.assertIn("over 200 chars",
                      self.mem.write("s", "c", [long])["reason"])
        bare = {"kind": "url", "ref": self.page_url}
        self.assertIn("12+ char quote",
                      self.mem.write("s", "c", [bare])["reason"])
        unshaped = dict(good, snapshot="../evil")
        self.assertIn("snapshot hash",
                      self.mem.write("s", "c", [unshaped])["reason"])
        missing = dict(good, snapshot="0" * 64)
        self.assertIn("no snapshot",
                      self.mem.write("s", "c", [missing])["reason"])
        other = dict(good, ref="https://other.test/y")
        self.assertIn("another URL",
                      self.mem.write("s", "c", [other])["reason"])

    def test_tampered_snapshot_is_caught(self):
        path = os.path.join(self.tmp.name, "snapshots",
                            f"{self.page_snap}.json")
        with open(path, "a") as fh:
            fh.write("tampered")
        cite = {"kind": "url", "ref": self.page_url,
                "snapshot": self.page_snap, "quote": "rates rose 18%"}
        self.assertIn("does not match its hash",
                      self.mem.write("s", "c", [cite])["reason"])

    def test_url_quote_ignores_layout_whitespace(self):
        snap, _ = write_snapshot(
            self.tmp.name, url="https://example.com/y",
            final_url="https://example.com/y", status=200,
            content_type="text/plain", text="rates\n  rose   18%")
        cite = {"kind": "url", "ref": "https://example.com/y",
                "snapshot": snap, "quote": "rates rose   18%!"}
        self.assertIn("quote not in snapshot",
                      self.mem.write("s", "c", [cite])["reason"])
        cite = dict(cite, quote="rates   rose 18%")
        self.assertEqual(
            self.mem.write("s", "c", [cite])["reason"], "")

    def test_other_agent_can_verify(self):
        entry = self.mem.write("spec", "claim",
                               [{"kind": "file", "ref": "paper.txt:1",
                               "quote": "real content"}])
        verified = self.mem.verify(entry["id"], "critic")
        self.assertEqual(verified["status"], "verified")
        self.assertEqual(verified["verified_by"], "critic")

    def test_mechanical_check_can_verify(self):
        mem = Memory(os.path.join(self.tmp.name, "m2.sqlite3"),
                     file_root=self.root, receipt_store=self.receipts,
                     mechanical_checks=frozenset({"citation-resolve"}))
        try:
            entry = mem.write("spec", "claim",
                              [{"kind": "file", "ref": "paper.txt:1",
                               "quote": "real content"}])
            verified = mem.verify(entry["id"], "citation-resolve",
                                  mechanical=True)
            self.assertEqual(verified["status"], "verified")
        finally:
            mem.close()

    def test_unregistered_mechanical_check_is_refused(self):
        entry = self.mem.write("spec", "claim",
                               [{"kind": "file", "ref": "paper.txt:1",
                               "quote": "real content"}])
        with self.assertRaises(MemoryError) as ctx:
            self.mem.verify(entry["id"], "citation-resolve", mechanical=True)
        self.assertEqual(ctx.exception.code, "UNREGISTERED_CHECK")

    def test_mechanical_prefix_without_flag_is_refused(self):
        entry = self.mem.write("spec", "claim",
                               [{"kind": "file", "ref": "paper.txt:1",
                               "quote": "real content"}])
        with self.assertRaises(MemoryError) as ctx:
            self.mem.verify(entry["id"], "mechanical:spec")
        self.assertEqual(ctx.exception.code, "MECHANICAL_SPOOF")

    def test_self_verify_is_refused(self):
        entry = self.mem.write("spec", "claim",
                               [{"kind": "file", "ref": "paper.txt:1",
                               "quote": "real content"}])
        with self.assertRaises(MemoryError) as ctx:
            self.mem.verify(entry["id"], "spec")
        self.assertEqual(ctx.exception.code, "SELF_VERIFY")
        self.assertEqual(self.mem.get(entry["id"])["status"], "unverified")

    def test_stale_evidence_at_verify_marks_disputed(self):
        entry = self.mem.write("spec", "claim",
                               [{"kind": "file", "ref": "paper.txt:1",
                               "quote": "real content"}])
        os.unlink(os.path.join(self.root, "paper.txt"))
        with self.assertRaises(MemoryError) as ctx:
            self.mem.verify(entry["id"], "critic")
        self.assertEqual(ctx.exception.code, "STALE_EVIDENCE")
        self.assertEqual(self.mem.get(entry["id"])["status"], "disputed")

    def test_disputed_entries_are_quarantined_from_recall(self):
        entry = self.mem.write("spec", "claim",
                               [{"kind": "file", "ref": "paper.txt:1",
                               "quote": "real content"}])
        self.mem.dispute(entry["id"], "critic", "wrong")
        self.assertEqual(self.mem.recall(), [])
        self.assertEqual(self.mem.recall(include_unverified=True), [])

    def test_recall_defaults_to_verified_only(self):
        self.mem.write("spec", "unverified claim",
                       [{"kind": "file", "ref": "paper.txt:1",
                               "quote": "real content"}])
        good = self.mem.write("spec", "good claim",
                              [{"kind": "file", "ref": "paper.txt:1",
                               "quote": "real content"}])
        self.mem.verify(good["id"], "critic")
        self.assertEqual([e["id"] for e in self.mem.recall()], [good["id"]])
        self.assertEqual(len(self.mem.recall(include_unverified=True)), 2)

    def test_expiry_flags_dependents(self):
        parent = self.mem.write("spec", "parent",
                                [{"kind": "file", "ref": "paper.txt:1",
                               "quote": "real content"}],
                                ttl_seconds=1)
        child = self.mem.write("spec", "child",
                               [{"kind": "file", "ref": "paper.txt:1",
                               "quote": "real content"}],
                               derived_from=[parent["id"]])
        self.assertEqual(child["status"], "unverified")
        result = self.mem.expire_sweep(now=time.time() + 100)
        self.assertEqual(result, {"expired": 1, "flagged": 1})
        self.assertEqual(self.mem.get(parent["id"])["status"], "expired")
        self.assertEqual(self.mem.get(child["id"])["status"],
                         "derived-unverified")

    def test_deriving_from_expired_parent_flags_immediately(self):
        parent = self.mem.write("spec", "parent",
                                [{"kind": "file", "ref": "paper.txt:1",
                               "quote": "real content"}],
                                ttl_seconds=1)
        self.mem.expire_sweep(now=time.time() + 100)
        child = self.mem.write("spec", "child",
                               [{"kind": "file", "ref": "paper.txt:1",
                               "quote": "real content"}],
                               derived_from=[parent["id"]])
        self.assertEqual(child["status"], "derived-unverified")

    def test_unknown_parent_is_refused(self):
        with self.assertRaises(MemoryError) as ctx:
            self.mem.write("spec", "child",
                           [{"kind": "file", "ref": "paper.txt:1",
                               "quote": "real content"}],
                           derived_from=["mem_nope"])
        self.assertEqual(ctx.exception.code, "UNKNOWN_ENTRY")

    def test_unknown_source_kind_is_caught(self):
        entry = self.mem.write("spec", "claim",
                               [{"kind": "vibes", "ref": "trust me"}])
        self.assertIn("unknown source kind", entry["reason"])


if __name__ == "__main__":
    unittest.main()
