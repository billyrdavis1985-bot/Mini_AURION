"""Typed inter-agent messages. Agents don't chat; they file claims.

Every message carries: sender, task, claim, evidence pointers, confidence,
and cost so far. A message without evidence is refused by recipients, not
interpreted generously. Evidence kinds match memory.py: file, receipt, url.
"""
from __future__ import annotations


class MessageError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.code = code


KINDS = ("file", "receipt", "url")


class AgentMessage:
    def __init__(self, *, sender: str, task_id: str, claim: str,
                 evidence: list[dict] | None = None,
                 confidence: float = 0.0, cost_nano: int = 0,
                 escalated: bool = False):
        if not sender or not isinstance(sender, str):
            raise MessageError("BAD_FIELD", "sender must be a non-empty string")
        if not task_id or not isinstance(task_id, str):
            raise MessageError("BAD_FIELD", "task_id must be a non-empty string")
        if not claim or not isinstance(claim, str):
            raise MessageError("BAD_FIELD", "claim must be a non-empty string")
        if not isinstance(confidence, (int, float)) or not 0.0 <= confidence <= 1.0:
            raise MessageError("BAD_FIELD", "confidence must be within 0..1")
        if not isinstance(cost_nano, int) or cost_nano < 0:
            raise MessageError("BAD_FIELD", "cost_nano must be a non-negative int")
        evidence = list(evidence or [])
        for item in evidence:
            if not isinstance(item, dict):
                raise MessageError("BAD_EVIDENCE", "evidence items must be objects")
            if item.get("kind") not in KINDS:
                raise MessageError("BAD_EVIDENCE",
                                   f"kind must be one of {KINDS}")
            if "ref" not in item:
                raise MessageError("BAD_EVIDENCE", "evidence items need a ref")
        self.sender = sender
        self.task_id = task_id
        self.claim = claim
        self.evidence = evidence
        self.confidence = float(confidence)
        self.cost_nano = cost_nano
        self.escalated = escalated

    def require_evidence(self) -> "AgentMessage":
        """Refuse evidence-free claims. Escalations pass without evidence:
        'I can't verify this' is honest, not a claim."""
        if not self.escalated and not self.evidence:
            raise MessageError("NO_EVIDENCE",
                               f"{self.sender} filed a claim without evidence")
        return self

    def to_dict(self) -> dict:
        return {"sender": self.sender, "task_id": self.task_id,
                "claim": self.claim, "evidence": self.evidence,
                "confidence": self.confidence, "cost_nano": self.cost_nano,
                "escalated": self.escalated}
