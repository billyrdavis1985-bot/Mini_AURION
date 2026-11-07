"""Trust wrapper around any tool backend.

Every call passes through, in order: policy gate → spend reservation →
pending receipt → execution → settlement from the real outcome. Denials and
approval requests are themselves receipted. There is no path that runs a
tool without a receipt, and no path that spends without a reservation.

Backend protocol: `call(name, args)` returns a result (or raises), and the
optional attribute `last_cost_nano` reports what the last call cost.
"""
from __future__ import annotations

from . import policy
from .ledger import LedgerError
from .receipts import ReceiptStore


class ToolDenied(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.code = code


class NeedsApproval(Exception):
    def __init__(self, request: dict):
        super().__init__(f"NEEDS_APPROVAL: {request['action_class']} "
                         f"{request['tool']}")
        self.request = request


class ToolFailed(Exception):
    def __init__(self, seq: int, message: str):
        super().__init__(f"TOOL_FAILED seq={seq}: {message}")
        self.seq = seq


class ToolWrapper:
    def __init__(self, backend, *, receipts: ReceiptStore,
                 ledger, stage: str, task_id: str, agent_id: str):
        self.backend = backend
        self.receipts = receipts
        self.ledger = ledger
        self.stage = stage
        self.task_id = task_id
        self.agent_id = agent_id

    def call(self, name: str, args: dict, action_class: str, *,
             estimate_nano: int = 0, approved: bool = False,
             allowlisted: bool = False) -> dict:
        verdict, reason = policy.decide(self.stage, action_class,
                                        approved=approved,
                                        allowlisted=allowlisted)
        if verdict == "deny":
            self.receipts.append(agent_id=self.agent_id, task_id=self.task_id,
                                 action="denied", tool=name,
                                 detail={"class": action_class, "reason": reason},
                                 outcome="denied")
            raise ToolDenied("POLICY_DENIED",
                             f"{action_class} {name}: {reason}")
        if verdict == "approve":
            request = {"agent_id": self.agent_id, "task_id": self.task_id,
                       "tool": name, "args": dict(args),
                       "action_class": action_class, "reason": reason}
            self.receipts.append(agent_id=self.agent_id, task_id=self.task_id,
                                 action="approval_requested", tool=name,
                                 detail=request, outcome="ok")
            raise NeedsApproval(request)
        reservation = None
        if estimate_nano > 0:
            try:
                reservation = self.ledger.reserve(self.task_id, estimate_nano)
            except LedgerError as exc:
                self.receipts.append(agent_id=self.agent_id,
                                     task_id=self.task_id,
                                     action="denied", tool=name,
                                     detail={"class": action_class,
                                             "reason": f"CAP_EXCEEDED: {exc}"},
                                     outcome="denied")
                raise
        begun = self.receipts.begin(agent_id=self.agent_id,
                                    task_id=self.task_id, action="tool_call",
                                    tool=name, detail={"args": dict(args)})
        try:
            result = self.backend.call(name, args)
            return_code = 0
            error = ""
        except Exception as exc:  # noqa: BLE001 - any backend failure settles failed
            result = None
            return_code = 1
            error = str(exc)
        actual = getattr(self.backend, "last_cost_nano", 0) or 0
        if reservation is not None:
            self.ledger.settle(reservation, actual)
        self.receipts.settle(begun["seq"], return_code, cost_nano=actual)
        if return_code != 0:
            raise ToolFailed(begun["seq"], error)
        return {"result": result, "seq": begun["seq"], "cost_nano": actual}
