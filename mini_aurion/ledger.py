"""Spend ledger with reserve-then-settle caps.

Money is integer nano-USD. Before any paid call, the caller reserves a
worst-case estimate against three caps (task, daily, global). The call is
never made if any cap would break. Afterwards the reservation settles to the
actual reported cost, or is refunded if the call never happened.

An actual cost may exceed its estimate (hidden reasoning tokens, price drift).
Overruns are recorded at actual cost with an overrun flag, never silently.
"""
from __future__ import annotations

import os
import sqlite3
import time
import uuid
from pathlib import Path

NANO_PER_USD = 1_000_000_000


class LedgerError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.code = code


def _today() -> str:
    return time.strftime("%Y-%m-%d", time.gmtime())


class Ledger:
    def __init__(self, path: str | os.PathLike[str], *, task_cap_nano: int,
                 daily_cap_nano: int, global_cap_nano: int):
        for name, value in (("task_cap_nano", task_cap_nano),
                            ("daily_cap_nano", daily_cap_nano),
                            ("global_cap_nano", global_cap_nano)):
            if not isinstance(value, int) or value <= 0:
                raise LedgerError("BAD_CAP", f"{name} must be a positive int")
        self.task_cap = task_cap_nano
        self.daily_cap = daily_cap_nano
        self.global_cap = global_cap_nano
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(self.path), timeout=10.0,
                                  isolation_level=None)
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS reservations ("
            " id TEXT PRIMARY KEY, task_id TEXT NOT NULL,"
            " estimate_nano INTEGER NOT NULL, state TEXT NOT NULL,"
            " actual_nano INTEGER, day TEXT NOT NULL, created_at TEXT NOT NULL)")
        self.db.execute(
            "CREATE INDEX IF NOT EXISTS idx_res_task ON reservations(task_id)")
        self.db.execute(
            "CREATE INDEX IF NOT EXISTS idx_res_day ON reservations(day)")
        self.db.commit()

    def close(self) -> None:
        self.db.close()

    def _live_where(self) -> str:
        return "state IN ('reserved','settled')"

    def _task_live(self, task_id: str) -> int:
        row = self.db.execute(
            f"SELECT COALESCE(SUM(CASE WHEN state='settled' THEN actual_nano"
            f" ELSE estimate_nano END),0) FROM reservations"
            f" WHERE task_id=? AND {self._live_where()}",
            (task_id,)).fetchone()
        return int(row[0])

    def _day_live(self, day: str) -> int:
        row = self.db.execute(
            f"SELECT COALESCE(SUM(CASE WHEN state='settled' THEN actual_nano"
            f" ELSE estimate_nano END),0) FROM reservations"
            f" WHERE day=? AND {self._live_where()}",
            (day,)).fetchone()
        return int(row[0])

    def _global_live(self) -> int:
        row = self.db.execute(
            f"SELECT COALESCE(SUM(CASE WHEN state='settled' THEN actual_nano"
            f" ELSE estimate_nano END),0) FROM reservations"
            f" WHERE {self._live_where()}").fetchone()
        return int(row[0])

    def reserve(self, task_id: str, worst_case_nano: int) -> str:
        if not task_id or not isinstance(task_id, str):
            raise LedgerError("BAD_FIELD", "task_id must be a non-empty string")
        if not isinstance(worst_case_nano, int) or worst_case_nano <= 0:
            raise LedgerError("BAD_FIELD", "worst_case_nano must be a positive int")
        day = _today()
        # BEGIN IMMEDIATE closes the check-then-insert race: concurrent
        # reservers serialize here instead of all reading the same total.
        self.db.execute("BEGIN IMMEDIATE")
        try:
            if self._task_live(task_id) + worst_case_nano > self.task_cap:
                raise LedgerError("CAP_EXCEEDED",
                                  f"task cap {self.task_cap} would break")
            if self._day_live(day) + worst_case_nano > self.daily_cap:
                raise LedgerError("CAP_EXCEEDED",
                                  f"daily cap {self.daily_cap} would break")
            if self._global_live() + worst_case_nano > self.global_cap:
                raise LedgerError("CAP_EXCEEDED",
                                  f"global cap {self.global_cap} would break")
            rid = "rsv_" + uuid.uuid4().hex[:16]
            self.db.execute(
                "INSERT INTO reservations (id, task_id, estimate_nano, state,"
                " day, created_at) VALUES (?,?,?,?,?,?)",
                (rid, task_id, worst_case_nano, "reserved", day,
                 time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())))
        except Exception:
            self.db.execute("ROLLBACK")
            raise
        self.db.execute("COMMIT")
        return rid

    def settle(self, reservation_id: str, actual_nano: int) -> bool:
        """Settle to actual cost. Returns True if actual exceeded estimate."""
        if not isinstance(actual_nano, int) or actual_nano < 0:
            raise LedgerError("BAD_FIELD", "actual_nano must be a non-negative int")
        row = self.db.execute(
            "SELECT estimate_nano, state FROM reservations WHERE id=?",
            (reservation_id,)).fetchone()
        if row is None:
            raise LedgerError("UNKNOWN_RESERVATION", reservation_id)
        estimate, state = row
        if state != "reserved":
            raise LedgerError("BAD_STATE",
                              f"reservation is {state}, not reserved")
        self.db.execute(
            "UPDATE reservations SET state='settled', actual_nano=? WHERE id=?",
            (actual_nano, reservation_id))
        self.db.commit()
        return actual_nano > estimate

    def refund(self, reservation_id: str) -> None:
        row = self.db.execute(
            "SELECT state FROM reservations WHERE id=?",
            (reservation_id,)).fetchone()
        if row is None:
            raise LedgerError("UNKNOWN_RESERVATION", reservation_id)
        if row[0] != "reserved":
            raise LedgerError("BAD_STATE",
                              f"reservation is {row[0]}, not reserved")
        self.db.execute("UPDATE reservations SET state='refunded' WHERE id=?",
                        (reservation_id,))
        self.db.commit()

    def spend(self, task_id: str) -> dict:
        """Settled + in-flight exposure for one task, in nano-USD."""
        settled = self.db.execute(
            "SELECT COALESCE(SUM(actual_nano),0) FROM reservations"
            " WHERE task_id=? AND state='settled'", (task_id,)).fetchone()[0]
        inflight = self.db.execute(
            "SELECT COALESCE(SUM(estimate_nano),0) FROM reservations"
            " WHERE task_id=? AND state='reserved'", (task_id,)).fetchone()[0]
        return {"settled_nano": int(settled), "inflight_nano": int(inflight)}

    def totals(self) -> dict:
        settled = self.db.execute(
            "SELECT COALESCE(SUM(actual_nano),0) FROM reservations"
            " WHERE state='settled'").fetchone()[0]
        inflight = self.db.execute(
            "SELECT COALESCE(SUM(estimate_nano),0) FROM reservations"
            " WHERE state='reserved'").fetchone()[0]
        overruns = self.db.execute(
            "SELECT COUNT(*) FROM reservations WHERE state='settled'"
            " AND actual_nano > estimate_nano").fetchone()[0]
        return {"settled_nano": int(settled), "inflight_nano": int(inflight),
                "overruns": int(overruns)}
