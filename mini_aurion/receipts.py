"""Hash-chained append-only action receipts.

Every agent action records who did it, under which task and policy, what it
cost, and what happened. Each receipt links to the previous receipt's hash,
so tampering or truncation is detectable by replay.

Single-writer: one process appends to a receipt file. Concurrent writers are
not supported in Milestone 0 and must coordinate outside this module.

Pending/settle: actions with real-world effects are recorded with begin()
(outcome "pending"), then settled with settle() from the action's ACTUAL
return code. Agents never set outcomes; only return codes do. Settlement is
itself a receipt (append-only: nothing is ever mutated), linked by seq.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
from pathlib import Path

RECEIPT_SCHEMA = "mini-aurion.receipt.v1"
MAX_RECEIPT_BYTES = 64 * 1024


class ReceiptError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.code = code


def _canonical(obj: object) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True).encode("utf-8")


def _digest(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


class ReceiptStore:
    """Append-only JSONL receipt file with a hash chain."""

    def __init__(self, path: str | os.PathLike[str]):
        self.path = Path(path)
        self.anchor_path = Path(str(path) + ".anchor")
        self.index_path = Path(str(path) + ".index.db")

    def __len__(self) -> int:
        if not self.path.exists():
            return 0
        count = 0
        with open(self.path, "r", encoding="utf-8") as fh:
            for line in fh:
                if line.strip():
                    count += 1
        return count

    def _tail_hash(self) -> str:
        last = self._tail_line()
        if last is None:
            return "genesis"
        try:
            return json.loads(last)["hash"]
        except (ValueError, KeyError) as exc:
            raise ReceiptError("CHAIN_BROKEN",
                               f"tail line is not a receipt: {exc}") from exc

    def _next_seq(self) -> tuple[int, str]:
        """Next seq and prev hash, constant-time on the hot path.

        When the tail line matches the head anchor, seq and hash come
        from the anchor — no scan. Any mismatch (crash between line
        and anchor write, external edit) falls back to the full scan,
        which errors precisely through the existing checks.
        """
        anchor = self._read_anchor()
        tail = self._tail_line()
        if tail is not None and anchor is not None:
            try:
                parsed = json.loads(tail)
                if parsed.get("seq") == anchor["max_seq"] and \
                        parsed.get("hash") == anchor["hash"]:
                    return anchor["max_seq"] + 1, anchor["hash"]
            except ValueError:
                pass
        if tail is None:
            return 1, "genesis"
        return len(self) + 1, self._tail_hash()

    def _tail_line(self) -> str | None:
        """Last non-empty line without scanning the file. A polling
        monitor plus per-append full rereads made every run quadratic
        (EXPERIMENTS F6); the tail is all append ever needs."""
        try:
            size = self.path.stat().st_size
        except OSError:
            return None
        if size == 0:
            return None
        with open(self.path, "rb") as fh:
            chunk_size = min(size, MAX_RECEIPT_BYTES + 2)
            fh.seek(size - chunk_size)
            chunk = fh.read(chunk_size)
        lines = [ln for ln in chunk.decode("utf-8", "replace").split("\n")
                 if ln.strip()]
        if lines and (len(lines) > 1 or chunk_size == size):
            return lines[-1]
        # Last line exceeds one receipt: fall back to the full scan
        # (corrupt or foreign file; the slow path errors precisely).
        last = None
        with open(self.path, "r", encoding="utf-8") as fh:
            for line in fh:
                if line.strip():
                    last = line.strip()
        return last

    def append(self, *, agent_id: str, task_id: str, action: str,
               tool: str = "", detail: dict | None = None,
               cost_nano: int = 0, policy_version: str = "m0",
               outcome: str = "ok", settles: int | None = None) -> dict:
        for name, value in (("agent_id", agent_id), ("task_id", task_id),
                            ("action", action)):
            if not value or not isinstance(value, str):
                raise ReceiptError("BAD_FIELD", f"{name} must be a non-empty string")
        if not isinstance(cost_nano, int) or cost_nano < 0:
            raise ReceiptError("BAD_FIELD", "cost_nano must be a non-negative int")
        seq, prev_hash = self._next_seq()
        body = {
            "schema": RECEIPT_SCHEMA,
            "seq": seq,
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "agent_id": agent_id,
            "task_id": task_id,
            "action": action,
            "tool": tool,
            "detail": detail or {},
            "cost_nano": cost_nano,
            "policy_version": policy_version,
            "outcome": outcome,
            "prev_hash": prev_hash,
        }
        if settles is not None:
            if not isinstance(settles, int) or settles < 1:
                raise ReceiptError("BAD_FIELD", "settles must be a positive int")
            body["settles"] = settles
        try:
            payload = _canonical(body)
        except (TypeError, ValueError) as exc:
            raise ReceiptError("NOT_JSON", f"detail is not JSON-serializable: {exc}") from exc
        if len(payload) > MAX_RECEIPT_BYTES:
            raise ReceiptError("TOO_LARGE",
                               f"receipt is {len(payload)} bytes, cap is {MAX_RECEIPT_BYTES}")
        receipt = dict(body)
        receipt["hash"] = _digest(payload)
        anchor = self._read_anchor()
        if anchor is not None and receipt["seq"] <= anchor["max_seq"]:
            raise ReceiptError("CHAIN_REWOUND",
                               f"seq {receipt['seq']} repeats history"
                               f" (anchor at {anchor['max_seq']}): the store"
                               " was truncated or replaced")
        line = _canonical(receipt) + b"\n"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with open(self.path, "ab") as fh:
                fh.write(line)
        except OSError as exc:
            raise ReceiptError("IO_FAILED", str(exc)) from exc
        try:
            if os.name == "posix":
                os.chmod(self.path, 0o600)
        except OSError:
            pass
        self._write_anchor(receipt["seq"], receipt["hash"])
        self._index_upsert(receipt)
        return receipt

    def _read_anchor(self) -> dict | None:
        if not self.anchor_path.exists():
            return None
        try:
            anchor = json.loads(self.anchor_path.read_text(encoding="utf-8"))
        except (ValueError, OSError) as exc:
            raise ReceiptError("ANCHOR_BROKEN",
                               f"cannot read anchor: {exc}") from exc
        if not isinstance(anchor.get("max_seq"), int) or \
                not isinstance(anchor.get("hash"), str):
            raise ReceiptError("ANCHOR_BROKEN", "anchor is malformed")
        return anchor

    def _write_anchor(self, seq: int, digest: str) -> None:
        tmp = self.anchor_path.with_suffix(self.anchor_path.suffix + ".tmp")
        try:
            payload = _canonical({"max_seq": seq, "hash": digest})
            tmp.write_text(payload.decode("utf-8"), encoding="utf-8")
            os.replace(tmp, self.anchor_path)
            if os.name == "posix":
                os.chmod(self.anchor_path, 0o600)
        except OSError as exc:
            raise ReceiptError("IO_FAILED",
                               f"cannot write anchor: {exc}") from exc


    def _index_db(self):
        self.index_path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(str(self.index_path), timeout=10.0)
        db.execute(
            "CREATE TABLE IF NOT EXISTS entries (seq INTEGER PRIMARY KEY,"
            " action TEXT NOT NULL, agent_id TEXT NOT NULL,"
            " tool TEXT NOT NULL, outcome TEXT NOT NULL,"
            " ts TEXT NOT NULL)")
        return db

    def _index_upsert(self, receipt):
        # Cache one row for the monitor. The JSONL file is the truth;
        # the index is acceleration the monitor rebuilds on any
        # mismatch, so a crash between line, index, and anchor
        # writes self-heals.
        db = self._index_db()
        try:
            db.execute(
                "INSERT OR REPLACE INTO entries"
                " (seq, action, agent_id, tool, outcome, ts)"
                " VALUES (?,?,?,?,?,?)",
                (receipt["seq"], receipt["action"],
                 receipt.get("agent_id", ""), receipt.get("tool", ""),
                 receipt.get("outcome", ""), receipt.get("ts", "")))
            db.commit()
        finally:
            db.close()

    def index_count(self):
        # Indexed rows, for staleness checks. Missing index reads zero.
        if not self.index_path.exists():
            return 0
        db = sqlite3.connect(str(self.index_path), timeout=10.0)
        try:
            return int(db.execute(
                "SELECT COUNT(*) FROM entries").fetchone()[0])
        except sqlite3.Error:
            return 0
        finally:
            db.close()

    def index_rebuild(self):
        # Rebuild the index from the JSONL truth. Returns row count.
        if self.index_path.exists():
            self.index_path.unlink()
        count = 0
        for receipt in self.read_all():
            self._index_upsert(receipt)
            count += 1
        return count

    def index_tail(self, limit=50):
        # Newest index rows first. The monitor's cheap poll.
        if not self.index_path.exists():
            return []
        db = sqlite3.connect(str(self.index_path), timeout=10.0)
        try:
            rows = db.execute(
                "SELECT seq, action, agent_id, tool, outcome, ts"
                " FROM entries ORDER BY seq DESC LIMIT ?", (limit,),
            ).fetchall()
        except sqlite3.Error:
            return []
        finally:
            db.close()
        return [{"seq": r[0], "action": r[1], "agent_id": r[2],
                 "tool": r[3], "outcome": r[4], "ts": r[5]} for r in rows]

    def begin(self, *, agent_id: str, task_id: str, action: str,
              tool: str = "", detail: dict | None = None) -> dict:
        """Record an action as pending. Settle it later from its return code."""
        return self.append(agent_id=agent_id, task_id=task_id, action=action,
                           tool=tool, detail=detail, outcome="pending")

    def settle(self, seq: int, return_code: int, *, cost_nano: int = 0,
               detail: dict | None = None,
               settler_id: str = "orchestrator") -> dict:
        """Settle a pending receipt. Outcome comes ONLY from return_code.

        0 settles ok; anything else settles failed. There is no parameter
        for an agent to report its own outcome.
        """
        if not isinstance(seq, int) or seq < 1:
            raise ReceiptError("BAD_FIELD", "seq must be a positive int")
        if not isinstance(return_code, int):
            raise ReceiptError("BAD_FIELD", "return_code must be an int")
        pending = None
        for receipt in self.read_all():
            if receipt.get("seq") == seq:
                pending = receipt
            if receipt.get("settles") == seq:
                raise ReceiptError("ALREADY_SETTLED",
                                   f"seq {seq} is already settled")
        if pending is None:
            raise ReceiptError("UNKNOWN_SEQ", f"no receipt with seq {seq}")
        if pending.get("outcome") != "pending":
            raise ReceiptError("NOT_PENDING",
                               f"seq {seq} has outcome {pending.get('outcome')!r}")
        outcome = "ok" if return_code == 0 else "failed"
        return self.append(agent_id=settler_id, task_id=pending["task_id"],
                           action="settle", outcome=outcome,
                           cost_nano=cost_nano, detail=detail, settles=seq)

    def read_all(self) -> list[dict]:
        if not self.path.exists():
            return []
        receipts = []
        with open(self.path, "r", encoding="utf-8") as fh:
            for lineno, line in enumerate(fh, 1):
                if not line.strip():
                    continue
                try:
                    receipts.append(json.loads(line))
                except ValueError as exc:
                    raise ReceiptError("CHAIN_BROKEN",
                                       f"line {lineno} is not JSON: {exc}") from exc
        return receipts

    def verify(self) -> int:
        """Replay the chain. Returns the receipt count, raises on any break."""
        expected_prev = "genesis"
        expected_seq = 1
        seen_seqs: set[int] = set()
        settle_targets: list[tuple[int, int]] = []
        for receipt in self.read_all():
            for key in ("seq", "prev_hash", "hash"):
                if key not in receipt:
                    raise ReceiptError("CHAIN_BROKEN",
                                       f"seq {expected_seq}: missing {key!r}")
            if receipt["seq"] != expected_seq:
                raise ReceiptError("CHAIN_BROKEN",
                                   f"seq {receipt['seq']}, expected {expected_seq}")
            if receipt["prev_hash"] != expected_prev:
                raise ReceiptError("CHAIN_BROKEN",
                                   f"seq {expected_seq}: prev_hash mismatch")
            check = {k: v for k, v in receipt.items() if k != "hash"}
            if _digest(_canonical(check)) != receipt["hash"]:
                raise ReceiptError("BAD_HASH", f"seq {expected_seq}: hash mismatch")
            if receipt.get("settles") is not None:
                target = receipt["settles"]
                if not isinstance(target, int):
                    raise ReceiptError("CHAIN_BROKEN",
                                       f"seq {expected_seq}: bad settles link")
                settle_targets.append((expected_seq, target))
            seen_seqs.add(expected_seq)
            expected_prev = receipt["hash"]
            expected_seq += 1
        for settler, target in settle_targets:
            if target not in seen_seqs:
                raise ReceiptError("CHAIN_BROKEN",
                                   f"seq {settler} settles unknown seq {target}")
            if target >= settler:
                raise ReceiptError("CHAIN_BROKEN",
                                   f"seq {settler} settles seq {target}, not earlier")
        targets = [target for _, target in settle_targets]
        if len(set(targets)) != len(targets):
            raise ReceiptError("CHAIN_BROKEN",
                               "a pending action settles exactly once")
        anchor = self._read_anchor()
        count = expected_seq - 1
        if anchor is None:
            if count > 0:
                raise ReceiptError("ANCHOR_MISSING",
                                   "receipts present but the head anchor is"
                                   " gone: absence proves nothing")
        elif count != anchor["max_seq"] or expected_prev != anchor["hash"]:
            raise ReceiptError("CHAIN_BROKEN",
                               "tail does not match the head anchor:"
                               " the store was truncated or replaced")
        return expected_seq - 1


def verify_on_start(path: str | os.PathLike[str]) -> int:
    """Self-check entry point. Returns the receipt count; raises on any break.

    Every process start calls this before doing anything else. A broken
    chain refuses to run.
    """
    return ReceiptStore(path).verify()
