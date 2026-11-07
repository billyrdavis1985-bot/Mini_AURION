import os
import tempfile
import unittest

from mini_aurion.ledger import Ledger, LedgerError

CAPS = {"task_cap_nano": 10_000, "daily_cap_nano": 50_000,
        "global_cap_nano": 100_000}


class LedgerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="ma-ledger-")
        self.ledger = Ledger(os.path.join(self.tmp.name, "l.sqlite3"), **CAPS)

    def tearDown(self):
        self.ledger.close()
        self.tmp.cleanup()

    def test_reserve_settle_math(self):
        rid = self.ledger.reserve("t1", 4_000)
        self.assertEqual(self.ledger.spend("t1"),
                         {"settled_nano": 0, "inflight_nano": 4_000})
        overrun = self.ledger.settle(rid, 3_000)
        self.assertFalse(overrun)
        self.assertEqual(self.ledger.spend("t1"),
                         {"settled_nano": 3_000, "inflight_nano": 0})

    def test_task_cap_refuses_before_spending(self):
        self.ledger.reserve("t1", 9_000)
        with self.assertRaises(LedgerError) as ctx:
            self.ledger.reserve("t1", 2_000)
        self.assertEqual(ctx.exception.code, "CAP_EXCEEDED")
        self.assertEqual(self.ledger.spend("t1")["inflight_nano"], 9_000)

    def test_daily_cap_refuses_across_tasks(self):
        for i in range(5):
            self.ledger.reserve(f"t{i}", 10_000)
        with self.assertRaises(LedgerError) as ctx:
            self.ledger.reserve("t5", 1)
        self.assertEqual(ctx.exception.code, "CAP_EXCEEDED")

    def test_global_cap_refuses(self):
        small = Ledger(os.path.join(self.tmp.name, "g.sqlite3"),
                       task_cap_nano=100_000, daily_cap_nano=100_000,
                       global_cap_nano=5_000)
        try:
            small.reserve("t1", 5_000)
            with self.assertRaises(LedgerError) as ctx:
                small.reserve("t2", 1)
            self.assertEqual(ctx.exception.code, "CAP_EXCEEDED")
        finally:
            small.close()

    def test_refund_frees_the_reservation(self):
        rid = self.ledger.reserve("t1", 9_000)
        self.ledger.refund(rid)
        self.assertEqual(self.ledger.spend("t1")["inflight_nano"], 0)
        self.ledger.reserve("t1", 10_000)

    def test_overrun_is_recorded_not_hidden(self):
        rid = self.ledger.reserve("t1", 2_000)
        self.assertTrue(self.ledger.settle(rid, 2_500))
        self.assertEqual(self.ledger.spend("t1")["settled_nano"], 2_500)
        self.assertEqual(self.ledger.totals()["overruns"], 1)

    def test_unknown_reservation_is_refused(self):
        with self.assertRaises(LedgerError) as ctx:
            self.ledger.settle("rsv_nope", 1)
        self.assertEqual(ctx.exception.code, "UNKNOWN_RESERVATION")
        with self.assertRaises(LedgerError) as ctx:
            self.ledger.refund("rsv_nope")
        self.assertEqual(ctx.exception.code, "UNKNOWN_RESERVATION")

    def test_double_settle_is_refused(self):
        rid = self.ledger.reserve("t1", 1_000)
        self.ledger.settle(rid, 500)
        with self.assertRaises(LedgerError) as ctx:
            self.ledger.settle(rid, 500)
        self.assertEqual(ctx.exception.code, "BAD_STATE")

    def test_settle_after_refund_is_refused(self):
        rid = self.ledger.reserve("t1", 1_000)
        self.ledger.refund(rid)
        with self.assertRaises(LedgerError) as ctx:
            self.ledger.settle(rid, 500)
        self.assertEqual(ctx.exception.code, "BAD_STATE")

    def test_bad_inputs_are_refused(self):
        with self.assertRaises(LedgerError):
            self.ledger.reserve("", 100)
        with self.assertRaises(LedgerError):
            self.ledger.reserve("t1", 0)
        with self.assertRaises(LedgerError):
            self.ledger.reserve("t1", -5)
        with self.assertRaises(LedgerError):
            Ledger(os.path.join(self.tmp.name, "b.sqlite3"),
                   task_cap_nano=0, daily_cap_nano=1, global_cap_nano=1)


if __name__ == "__main__":
    unittest.main()
