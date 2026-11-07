"""Hash-chained append-only action receipts.

Every agent action records who did it, under which task and policy, what it
cost, and what happened. Each receipt links to the previous receipt's hash,
so tampering or truncation is detectable by replay.

Single-writer: one process appends to a receipt file. Concurrent writers are
not supported in Milestone 0 and must coordinate outside this module.
"""
from __future__ import annotations

import hashlib
import json
import os
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
        last = None
        if self.path.exists():
            with open(self.path, "r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if line:
                        last = line
        if last is None:
            return "genesis"
        try:
            return json.loads(last)["hash"]
        except (ValueError, KeyError) as exc:
            raise ReceiptError("CHAIN_BROKEN",
                               f"tail line is not a receipt: {exc}") from exc

    def append(self, *, agent_id: str, task_id: str, action: str,
               tool: str = "", detail: dict | None = None,
               cost_nano: int = 0, policy_version: str = "m0",
               outcome: str = "ok") -> dict:
        for name, value in (("agent_id", agent_id), ("task_id", task_id),
                            ("action", action)):
            if not value or not isinstance(value, str):
                raise ReceiptError("BAD_FIELD", f"{name} must be a non-empty string")
        if not isinstance(cost_nano, int) or cost_nano < 0:
            raise ReceiptError("BAD_FIELD", "cost_nano must be a non-negative int")
        body = {
            "schema": RECEIPT_SCHEMA,
            "seq": len(self) + 1,
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "agent_id": agent_id,
            "task_id": task_id,
            "action": action,
            "tool": tool,
            "detail": detail or {},
            "cost_nano": cost_nano,
            "policy_version": policy_version,
            "outcome": outcome,
            "prev_hash": self._tail_hash(),
        }
        try:
            payload = _canonical(body)
        except (TypeError, ValueError) as exc:
            raise ReceiptError("NOT_JSON", f"detail is not JSON-serializable: {exc}") from exc
        if len(payload) > MAX_RECEIPT_BYTES:
            raise ReceiptError("TOO_LARGE",
                               f"receipt is {len(payload)} bytes, cap is {MAX_RECEIPT_BYTES}")
        receipt = dict(body)
        receipt["hash"] = _digest(payload)
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
        return receipt

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
            expected_prev = receipt["hash"]
            expected_seq += 1
        return expected_seq - 1
