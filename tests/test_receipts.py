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


if __name__ == "__main__":
    unittest.main()
