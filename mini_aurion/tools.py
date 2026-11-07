"""Trust wrapper around any tool backend.

Every call passes through, in order: policy gate → spend reservation →
pending receipt → execution → settlement from the real outcome. Denials and
approval requests are themselves receipted. There is no path that runs a
tool without a receipt, and no path that spends without a reservation.

Backend protocol: `call(name, args)` returns a result (or raises), and the
optional attribute `last_cost_nano` reports what the last call cost.

Tool identity is server-side: a ToolRegistry declares each tool's action
class and worst-case cost. Model-supplied labels are ignored entirely; an
unlabeled or unknown tool is refused, never defaulted to READ.
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


class ToolRegistry:
    """Server-side tool identities: class + worst-case cost per tool.

    The registry also owns the tool definitions sent to the model. The
    model never invents tools; it only sees what code registered.
    """

    def __init__(self):
        self._tools: dict[str, dict] = {}

    def register(self, name: str, action_class: str, worst_case_nano: int,
                 backend, *, description: str = "",
                 parameters: dict | None = None) -> None:
        if not name or not isinstance(name, str):
            raise ToolDenied("BAD_TOOL_DEF", "tool name must be non-empty")
        if action_class not in policy.CLASSES:
            raise ToolDenied("BAD_TOOL_DEF",
                             f"class must be one of {policy.CLASSES}")
        if not isinstance(worst_case_nano, int) or worst_case_nano < 0:
            raise ToolDenied("BAD_TOOL_DEF",
                             "worst_case_nano must be a non-negative int")
        if not hasattr(backend, "call"):
            raise ToolDenied("BAD_TOOL_DEF", "backend needs a call method")
        if parameters is not None and not isinstance(parameters, dict):
            raise ToolDenied("BAD_TOOL_DEF", "parameters must be an object")
        if name in self._tools:
            raise ToolDenied("BAD_TOOL_DEF", f"{name!r} is already registered")
        self._tools[name] = {"class": action_class,
                             "estimate": worst_case_nano, "backend": backend,
                             "description": description,
                             "parameters": parameters or {"type": "object"}}

    def get(self, name: str) -> dict:
        try:
            return self._tools[name]
        except KeyError:
            raise ToolDenied("UNKNOWN_TOOL",
                             f"{name!r} is not registered") from None

    def definitions(self) -> list[dict]:
        """Tool specs for the model. Empty registry means no tools."""
        return [{"name": name, "description": spec["description"],
                 "parameters": spec["parameters"]}
                for name, spec in self._tools.items()]


class ToolWrapper:
    def __init__(self, registry: ToolRegistry, *, receipts: ReceiptStore,
                 ledger, stage: str, task_id: str, agent_id: str):
        self.registry = registry
        self.receipts = receipts
        self.ledger = ledger
        self.stage = stage
        self.task_id = task_id
        self.agent_id = agent_id

    def call(self, name: str, args: dict, *, approved: bool = False,
             allowlisted: bool = False) -> dict:
        try:
            spec = self.registry.get(name)
        except ToolDenied as exc:
            self.receipts.append(agent_id=self.agent_id, task_id=self.task_id,
                                 action="denied", tool=name,
                                 detail={"class": "UNKNOWN",
                                         "reason": str(exc)},
                                 outcome="denied")
            raise
        action_class = spec["class"]
        estimate_nano = spec["estimate"]
        backend = spec["backend"]
        if not isinstance(args, dict):
            self.receipts.append(agent_id=self.agent_id, task_id=self.task_id,
                                 action="denied", tool=name,
                                 detail={"class": action_class,
                                         "reason": "BAD_ARGS: arguments must"
                                                   " be an object"},
                                 outcome="denied")
            raise ToolDenied("BAD_ARGS", f"{name}: arguments must be"
                                        " an object")
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
            result = backend.call(name, args)
            return_code = 0
            error = ""
        except Exception as exc:  # noqa: BLE001 - any backend failure settles failed
            result = None
            return_code = 1
            error = str(exc)
        reported = getattr(backend, "last_cost_nano", None)
        if reported is None:
            actual = estimate_nano
        else:
            actual = reported or 0
        if reservation is not None:
            self.ledger.settle(reservation, actual)
        self.receipts.settle(begun["seq"], return_code, cost_nano=actual)
        if return_code != 0:
            raise ToolFailed(begun["seq"], error)
        return {"result": result, "seq": begun["seq"], "cost_nano": actual}
