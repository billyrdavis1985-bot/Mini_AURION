import json
import os
import tempfile
import unittest

from mini_aurion.receipts import ReceiptError, ReceiptStore


class ReceiptTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="ma-receipts-")
        self.store = ReceiptStore(os.path.join(self.tmp.name, "r.jsonl"))

    def tearDown(self):
        self.tmp.cleanup()

    def test_empty_store_verifies_with_zero(self):
        self.assertEqual(self.store.verify(), 0)
        self.assertEqual(len(self.store), 0)

    def test_append_then_verify_chain(self):
        r1 = self.store.append(agent_id="orc", task_id="t1", action="delegate")
        r2 = self.store.append(agent_id="spec", task_id="t1", action="read",
                               tool="read_file", cost_nano=1250, outcome="ok")
        self.assertEqual(r1["seq"], 1)
        self.assertEqual(r2["seq"], 2)
        self.assertEqual(r2["prev_hash"], r1["hash"])
        self.assertEqual(self.store.verify(), 2)
        self.assertEqual(len(self.store), 2)

    def test_receipt_carries_required_fields(self):
        r = self.store.append(agent_id="a", task_id="t", action="act",
                              detail={"k": "v"})
        for key in ("schema", "seq", "ts", "agent_id", "task_id", "action",
                    "cost_nano", "policy_version", "outcome", "prev_hash", "hash"):
            self.assertIn(key, r)

    def test_tampered_line_breaks_verification(self):
        self.store.append(agent_id="a", task_id="t", action="one")
        self.store.append(agent_id="a", task_id="t", action="two")
        path = self.store.path
        lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
        lines[0] = lines[0].replace('"one"', '"ONE"')
        path.write_text("".join(lines), encoding="utf-8")
        with self.assertRaises(ReceiptError) as ctx:
            self.store.verify()
        self.assertIn(ctx.exception.code, ("BAD_HASH", "CHAIN_BROKEN"))

    def test_truncated_tail_is_detected_by_the_anchor(self):
        self.store.append(agent_id="a", task_id="t", action="one")
        self.store.append(agent_id="a", task_id="t", action="two")
        path = self.store.path
        lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
        path.write_text(lines[0], encoding="utf-8")
        with self.assertRaises(ReceiptError):
            self.store.verify()
        with self.assertRaises(ReceiptError) as ctx:
            self.store.append(agent_id="a", task_id="t", action="three")
        self.assertEqual(ctx.exception.code, "CHAIN_REWOUND")

    def test_blank_lines_are_ignored(self):
        self.store.append(agent_id="a", task_id="t", action="one")
        with open(self.store.path, "a", encoding="utf-8") as fh:
            fh.write("\n\n")
        self.assertEqual(self.store.verify(), 1)

    def test_empty_ids_are_refused(self):
        with self.assertRaises(ReceiptError) as ctx:
            self.store.append(agent_id="", task_id="t", action="act")
        self.assertEqual(ctx.exception.code, "BAD_FIELD")

    def test_negative_cost_is_refused(self):
        with self.assertRaises(ReceiptError) as ctx:
            self.store.append(agent_id="a", task_id="t", action="act",
                              cost_nano=-1)
        self.assertEqual(ctx.exception.code, "BAD_FIELD")

    def test_oversize_receipt_is_refused(self):
        with self.assertRaises(ReceiptError) as ctx:
            self.store.append(agent_id="a", task_id="t", action="act",
                              detail={"blob": "x" * 70000})
        self.assertEqual(ctx.exception.code, "TOO_LARGE")

    def test_unserializable_detail_is_refused(self):
        with self.assertRaises(ReceiptError) as ctx:
            self.store.append(agent_id="a", task_id="t", action="act",
                              detail={"bad": object()})
        self.assertEqual(ctx.exception.code, "NOT_JSON")


    def test_fast_append_matches_slow_scan_results(self):
        for i in range(50):
            self.store.append(agent_id="orc", task_id="t1",
                              action=f"step-{i}")
        self.assertEqual(self.store.verify(), 50)
        anchor = json.loads(open(str(self.store.path) + ".anchor").read())
        self.assertEqual(anchor["max_seq"], 50)

    def test_crash_between_line_and_anchor_heals_on_next_append(self):
        first = self.store.append(agent_id="orc", task_id="t1",
                                  action="one")
        # A crash after the line write but before the anchor write
        # leaves a receipt the anchor never saw; the next append
        # must keep its seq and continue the chain, not reuse it.
        anchor_path = str(self.store.path) + ".anchor"
        before = json.loads(open(anchor_path).read())
        assert before["max_seq"] == 1
        raw = {"schema": "mini-aurion.receipt.v1", "seq": 2,
               "task_id": "t1", "agent_id": "orc", "action": "two",
               "tool": "", "outcome": "ok", "policy_version": "m0",
               "prev_hash": first["hash"],
               "hash": "sha256:" + "0" * 64, "cost_nano": 0,
               "ts": "2026-01-01T00:00:00Z", "detail": {}}
        with open(str(self.store.path), "a") as fh:
            fh.write(json.dumps(raw, sort_keys=True) + "\n")
        third = self.store.append(agent_id="orc", task_id="t1",
                                  action="three")
        self.assertEqual(third["seq"], 3)
        self.assertEqual(third["prev_hash"], raw["hash"])

    def test_index_tails_newest_first_and_rebuilds(self):
        for i in range(5):
            self.store.append(agent_id="a", task_id="t1", action=f"s{i}")
        self.assertEqual(self.store.index_count(), 5)
        tail = self.store.index_tail(limit=2)
        self.assertEqual([r["seq"] for r in tail], [5, 4])
        self.assertEqual(tail[0]["action"], "s4")
        os.unlink(str(self.store.index_path))
        self.assertEqual(self.store.index_count(), 0)
        self.assertEqual(self.store.index_tail(), [])
        self.assertEqual(self.store.index_rebuild(), 5)
        self.assertEqual(self.store.index_count(), 5)


class SettleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="ma-settle-")
        self.store = ReceiptStore(os.path.join(self.tmp.name, "r.jsonl"))

    def tearDown(self):
        self.tmp.cleanup()

    def test_begin_records_pending(self):
        r = self.store.begin(agent_id="spec", task_id="t1", action="fetch",
                             tool="web_fetch")
        self.assertEqual(r["outcome"], "pending")
        self.assertEqual(r["seq"], 1)
        self.assertEqual(self.store.verify(), 1)

    def test_zero_return_code_settles_ok(self):
        begun = self.store.begin(agent_id="spec", task_id="t1", action="fetch")
        settled = self.store.settle(begun["seq"], 0, cost_nano=500)
        self.assertEqual(settled["outcome"], "ok")
        self.assertEqual(settled["settles"], begun["seq"])
        self.assertEqual(settled["task_id"], "t1")
        self.assertEqual(settled["cost_nano"], 500)
        self.assertEqual(self.store.verify(), 2)

    def test_nonzero_return_code_settles_failed(self):
        begun = self.store.begin(agent_id="spec", task_id="t1", action="fetch")
        settled = self.store.settle(begun["seq"], 3)
        self.assertEqual(settled["outcome"], "failed")
        self.assertEqual(self.store.verify(), 2)

    def test_failed_action_stays_failed_in_chain(self):
        begun = self.store.begin(agent_id="spec", task_id="t1", action="fetch")
        self.store.settle(begun["seq"], 1)
        receipts = self.store.read_all()
        self.assertEqual(receipts[0]["outcome"], "pending")
        self.assertEqual(receipts[1]["outcome"], "failed")

    def test_settle_unknown_seq_is_refused(self):
        with self.assertRaises(ReceiptError) as ctx:
            self.store.settle(99, 0)
        self.assertEqual(ctx.exception.code, "UNKNOWN_SEQ")

    def test_double_settle_is_refused(self):
        begun = self.store.begin(agent_id="a", task_id="t", action="act")
        self.store.settle(begun["seq"], 0)
        with self.assertRaises(ReceiptError) as ctx:
            self.store.settle(begun["seq"], 0)
        self.assertEqual(ctx.exception.code, "ALREADY_SETTLED")

    def test_settle_of_non_pending_is_refused(self):
        direct = self.store.append(agent_id="a", task_id="t", action="act")
        with self.assertRaises(ReceiptError) as ctx:
            self.store.settle(direct["seq"], 0)
        self.assertEqual(ctx.exception.code, "NOT_PENDING")

    def test_tampered_settlement_breaks_verification(self):
        begun = self.store.begin(agent_id="a", task_id="t", action="act")
        self.store.settle(begun["seq"], 1)
        path = self.store.path
        text = path.read_text(encoding="utf-8")
        path.write_text(text.replace('"failed"', '"ok"'), encoding="utf-8")
        with self.assertRaises(ReceiptError):
            self.store.verify()

    def test_verify_on_start_passes_clean_and_refuses_tampered(self):
        from mini_aurion.receipts import verify_on_start
        self.store.begin(agent_id="a", task_id="t", action="act")
        self.assertEqual(verify_on_start(self.store.path), 1)
        with open(self.store.path, "a", encoding="utf-8") as fh:
            fh.write("not json\n")
        with self.assertRaises(ReceiptError):
            verify_on_start(self.store.path)


if __name__ == "__main__":
    unittest.main()
