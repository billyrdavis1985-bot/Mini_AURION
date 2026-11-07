"""Provenance-tracked memory with write-time evidence checks.

Every entry names its author, sources, and verification status. Source
pointers resolve mechanically at write time: a missing file, an unknown
receipt seq, or an unfetched URL lands the entry as `unverified` with the
reason recorded. No model judgment is involved.

Owner rule: only a DIFFERENT agent than the author, or a mechanical check,
can set an entry to `verified`. Self-verification is refused.

Expiry: `unverified` entries expire after their TTL. Entries derived from an
expired entry are flagged `derived-unverified`, never quietly kept.
`disputed` entries are quarantined from retrieval.
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
import uuid
from pathlib import Path

DEFAULT_TTL_SECONDS = 7 * 24 * 3600

STATUSES = ("unverified", "verified", "disputed", "expired",
            "derived-unverified")


class MemoryError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.code = code


class Memory:
    def __init__(self, path: str | os.PathLike[str], *,
                 file_root: str | os.PathLike[str],
                 receipt_store=None, fetched_urls: set[str] | None = None):
        self.path = Path(path)
        self.root = Path(file_root).resolve()
        self.receipts = receipt_store
        self.fetched = fetched_urls if fetched_urls is not None else set()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(self.path))
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS entries ("
            " id TEXT PRIMARY KEY, author TEXT NOT NULL, text TEXT NOT NULL,"
            " sources TEXT NOT NULL, status TEXT NOT NULL,"
            " verified_by TEXT, reason TEXT,"
            " created_at REAL NOT NULL, expires_at REAL NOT NULL,"
            " derived_from TEXT NOT NULL)")
        self.db.commit()

    def close(self) -> None:
        self.db.close()

    def _resolve(self, source: dict) -> str | None:
        """Return None if the pointer resolves, else a reason string."""
        kind = source.get("kind")
        ref = source.get("ref")
        if kind == "file":
            if not isinstance(ref, str) or not ref:
                return "file ref must be a non-empty string"
            candidate = (self.root / ref).resolve()
            try:
                candidate.relative_to(self.root)
            except ValueError:
                return f"file escapes root: {ref!r}"
            if not candidate.is_file():
                return f"file not found: {ref!r}"
            return None
        if kind == "receipt":
            if self.receipts is None:
                return "no receipt store configured"
            if not isinstance(ref, int):
                return "receipt ref must be a seq int"
            seqs = {r.get("seq") for r in self.receipts.read_all()}
            if ref not in seqs:
                return f"unknown receipt seq: {ref}"
            return None
        if kind == "url":
            if ref not in self.fetched:
                return f"url was not fetched: {ref!r}"
            return None
        return f"unknown source kind: {kind!r}"

    def write(self, author: str, text: str, sources: list[dict], *,
              derived_from: list[str] | None = None,
              ttl_seconds: int = DEFAULT_TTL_SECONDS) -> dict:
        if not author or not isinstance(author, str):
            raise MemoryError("BAD_FIELD", "author must be a non-empty string")
        if not text or not isinstance(text, str):
            raise MemoryError("BAD_FIELD", "text must be a non-empty string")
        if not isinstance(sources, list):
            raise MemoryError("BAD_FIELD", "sources must be a list")
        derived_from = list(derived_from or [])
        for parent in derived_from:
            row = self.db.execute(
                "SELECT status FROM entries WHERE id=?", (parent,)).fetchone()
            if row is None:
                raise MemoryError("UNKNOWN_ENTRY", f"unknown parent {parent!r}")
        failures = []
        for source in sources:
            if not isinstance(source, dict):
                failures.append("source must be an object")
                continue
            reason = self._resolve(source)
            if reason is not None:
                failures.append(reason)
        entry_id = "mem_" + uuid.uuid4().hex[:16]
        now = time.time()
        if failures:
            status, reason = "unverified", "; ".join(failures)
        else:
            status, reason = "unverified", ""
        tainted = [p for p in derived_from if self._is_tainted(p)]
        if tainted:
            status = "derived-unverified"
            note = f"derived from expired/unverified: {', '.join(tainted)}"
            reason = f"{reason}; {note}" if reason else note
        self.db.execute(
            "INSERT INTO entries (id, author, text, sources, status,"
            " verified_by, reason, created_at, expires_at, derived_from)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (entry_id, author, text, json.dumps(sources), status, None,
             reason, now, now + ttl_seconds, json.dumps(derived_from)))
        self.db.commit()
        return self.get(entry_id)

    def _is_tainted(self, entry_id: str) -> bool:
        row = self.db.execute(
            "SELECT status FROM entries WHERE id=?", (entry_id,)).fetchone()
        return row is not None and row[0] in ("expired", "derived-unverified",
                                              "disputed")

    def get(self, entry_id: str) -> dict:
        row = self.db.execute(
            "SELECT id, author, text, sources, status, verified_by, reason,"
            " created_at, expires_at, derived_from FROM entries WHERE id=?",
            (entry_id,)).fetchone()
        if row is None:
            raise MemoryError("UNKNOWN_ENTRY", f"unknown entry {entry_id!r}")
        return {"id": row[0], "author": row[1], "text": row[2],
                "sources": json.loads(row[3]), "status": row[4],
                "verified_by": row[5], "reason": row[6],
                "created_at": row[7], "expires_at": row[8],
                "derived_from": json.loads(row[9])}

    def verify(self, entry_id: str, verifier: str) -> dict:
        """Mark verified. Verifier must differ from the author, or be mechanical
        (verifier ids starting with 'mechanical:'). Sources re-resolve first."""
        entry = self.get(entry_id)
        if entry["status"] == "verified":
            raise MemoryError("BAD_STATE", "entry is already verified")
        if verifier == entry["author"]:
            raise MemoryError("SELF_VERIFY",
                              "author cannot verify its own entry")
        if not (verifier.startswith("mechanical:") or verifier):
            raise MemoryError("BAD_FIELD", "verifier must be non-empty")
        stale = []
        for source in entry["sources"]:
            reason = self._resolve(source)
            if reason is not None:
                stale.append(reason)
        if stale:
            self.db.execute(
                "UPDATE entries SET status='disputed', reason=? WHERE id=?",
                ("stale evidence at verify: " + "; ".join(stale), entry_id))
            self.db.commit()
            raise MemoryError("STALE_EVIDENCE", "; ".join(stale))
        self.db.execute(
            "UPDATE entries SET status='verified', verified_by=? WHERE id=?",
            (verifier, entry_id))
        self.db.commit()
        return self.get(entry_id)

    def dispute(self, entry_id: str, by: str, reason: str) -> dict:
        self.get(entry_id)
        self.db.execute(
            "UPDATE entries SET status='disputed', reason=? WHERE id=?",
            (f"disputed by {by}: {reason}", entry_id))
        self.db.commit()
        return self.get(entry_id)

    def expire_sweep(self, now: float | None = None) -> dict:
        """Expire stale entries; flag their dependents. Returns counts."""
        now = time.time() if now is None else now
        rows = self.db.execute(
            "SELECT id FROM entries WHERE status='unverified'"
            " AND expires_at <= ?", (now,)).fetchall()
        expired = [r[0] for r in rows]
        for entry_id in expired:
            self.db.execute("UPDATE entries SET status='expired' WHERE id=?",
                            (entry_id,))
        flagged = 0
        if expired:
            dependents = self.db.execute(
                "SELECT id, derived_from FROM entries"
                " WHERE status NOT IN ('expired','disputed')").fetchall()
            for entry_id, raw in dependents:
                parents = json.loads(raw)
                if any(p in expired for p in parents):
                    self.db.execute(
                        "UPDATE entries SET status='derived-unverified',"
                        " reason=reason || ' | parent expired' WHERE id=?",
                        (entry_id,))
                    flagged += 1
        self.db.commit()
        return {"expired": len(expired), "flagged": flagged}

    def recall(self, *, include_unverified: bool = False) -> list[dict]:
        """Retrieve entries. Disputed and expired are never returned."""
        rows = self.db.execute(
            "SELECT id FROM entries WHERE status NOT IN"
            " ('disputed','expired') ORDER BY created_at").fetchall()
        out = []
        for (entry_id,) in rows:
            entry = self.get(entry_id)
            if entry["status"] == "verified" or (
                    include_unverified and entry["status"] in
                    ("unverified", "derived-unverified")):
                out.append(entry)
        return out
