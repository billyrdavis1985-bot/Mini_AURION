import os
import sqlite3
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
                         {"settled_nano": 0, "inflight_nano": 4_000,
                          "by_provider": {}})
        overrun = self.ledger.settle(rid, 3_000)
        self.assertFalse(overrun)
        self.assertEqual(self.ledger.spend("t1"),
                         {"settled_nano": 3_000, "inflight_nano": 0,
                          "by_provider": {"/": 3_000}})

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


    def test_spend_reports_per_tier(self):
        first = self.ledger.reserve("t1", 4_000, provider="meta",
                                    model="m")
        second = self.ledger.reserve("t1", 4_000, provider="deepseek",
                                     model="d")
        self.ledger.settle(first, 1_000)
        self.ledger.settle(second, 2_000)
        spend = self.ledger.spend("t1")
        self.assertEqual(spend["settled_nano"], 3_000)
        self.assertEqual(spend["by_provider"],
                         {"meta/m": 1_000, "deepseek/d": 2_000})
        totals = self.ledger.totals()
        self.assertEqual(totals["by_provider"],
                         {"meta/m": 1_000, "deepseek/d": 2_000})

    def test_pre_tier_store_migrates_in_place(self):
        path = os.path.join(self.tmp.name, "old.sqlite3")
        db = sqlite3.connect(path)
        db.execute(
            "CREATE TABLE reservations (id TEXT PRIMARY KEY,"
            " task_id TEXT NOT NULL, estimate_nano INTEGER NOT NULL,"
            " state TEXT NOT NULL, actual_nano INTEGER, day TEXT NOT NULL,"
            " created_at TEXT NOT NULL)")
        db.execute(
            "INSERT INTO reservations VALUES ('old1', 't0', 500,"
            " 'settled', 500, '2026-01-01', '2026-01-01T00:00:00Z')")
        db.commit()
        db.close()
        migrated = Ledger(path, **CAPS)
        try:
            rid = migrated.reserve("t1", 4_000, provider="meta",
                                   model="m")
            migrated.settle(rid, 1_000)
            totals = migrated.totals()
            self.assertEqual(totals["settled_nano"], 1_500)
            self.assertEqual(totals["by_provider"],
                             {"/": 500, "meta/m": 1_000})
        finally:
            migrated.close()



    def test_caps_survive_reopen_and_ignore_later_constructors(self):
        self.ledger.close()
        path = os.path.join(self.tmp.name, "l.sqlite3")
        reopened = Ledger(path, task_cap_nano=1, daily_cap_nano=2,
                          global_cap_nano=3)
        try:
            self.assertEqual((reopened.task_cap, reopened.daily_cap,
                              reopened.global_cap), (10_000, 50_000, 100_000))
        finally:
            reopened.close()

    def test_open_refuses_a_path_that_has_no_ledger(self):
        with self.assertRaises(LedgerError) as ctx:
            Ledger.open(os.path.join(self.tmp.name, "missing.sqlite3"))
        self.assertEqual(ctx.exception.code, "NO_CAPS")

    def test_a_lowered_cap_bites_a_live_reserver_immediately(self):
        # The monitor's caps edit must hit the running process, not
        # the next one: reserve re-reads the caps table every call.
        path = os.path.join(self.tmp.name, "l.sqlite3")
        monitor = Ledger.open(path)
        try:
            monitor.set_caps(task=100)
            with self.assertRaises(LedgerError) as ctx:
                self.ledger.reserve("t1", 1_000)
            self.assertEqual(ctx.exception.code, "CAP_EXCEEDED")
        finally:
            monitor.close()

    def test_refund_task_releases_only_that_tasks_inflight(self):
        mine = self.ledger.reserve("t1", 4_000)
        self.ledger.reserve("t2", 2_000)
        settled = self.ledger.reserve("t1", 1_000)
        self.ledger.settle(settled, 500)
        out = self.ledger.refund_task("t1")
        self.assertEqual(out, {"count": 1, "released_nano": 4_000})
        with self.assertRaises(LedgerError) as ctx:
            self.ledger.settle(mine, 1)
        self.assertEqual(ctx.exception.code, "BAD_STATE")
        self.assertEqual(self.ledger.spend("t2")["inflight_nano"], 2_000)
if __name__ == "__main__":
    unittest.main()
