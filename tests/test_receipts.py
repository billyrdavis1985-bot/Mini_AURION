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

    def test_truncated_tail_breaks_nothing_but_count_is_honest(self):
        self.store.append(agent_id="a", task_id="t", action="one")
        self.store.append(agent_id="a", task_id="t", action="two")
        path = self.store.path
        lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
        path.write_text(lines[0], encoding="utf-8")
        self.assertEqual(self.store.verify(), 1)
        self.assertEqual(len(self.store), 1)

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
