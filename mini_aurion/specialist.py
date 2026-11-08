"""Specialist agent: bounded tool loop behind the trust wrapper.

The model investigates: it may call tools (each gated, receipted, and
costed by ToolWrapper) up to fixed limits. When it stops calling tools, its
final reply becomes a typed message. If it says CANNOT_VERIFY, or the
budget runs out, or a tool needs human approval, the result is an
escalation — never a guess. The final call is offered no tools, so the
loop converges to an answer or an explicit escalation.
"""
from __future__ import annotations

from .fixture import ESCALATE_TEXT
from .ledger import LedgerError
from .messages import AgentMessage
from .tools import NeedsApproval, ToolDenied, ToolFailed

# Per-tool-result cap. 2000 hid a graded answer sitting at byte ~2500 of a
# 6173-byte file (measured live); 12000 covers whole source files while
# still bounding context. Truncation is always marked, never silent.
MAX_TOOL_CHARS = 12000


class Specialist:
    def __init__(self, agent_id: str, model, tools, *,
                 max_model_calls: int = 6, max_tool_calls: int = 8,
                 model_estimate_nano: int):
        """model_estimate_nano is required: 0 asserts the builder knows the
        model is free. Metering is never silently off. Budgets fit
        list-then-read exploration (measured live: 4 tools starve it)."""
        if not isinstance(model_estimate_nano, int) or model_estimate_nano < 0:
            raise ValueError("model_estimate_nano must be a non-negative int")
        self.agent_id = agent_id
        self.model = model
        self.tools = tools
        self.max_model_calls = max_model_calls
        self.max_tool_calls = max_tool_calls
        self.model_estimate_nano = model_estimate_nano

    def investigate(self, task_id: str, brief: str) -> AgentMessage:
        self.tools.clear_cache()
        messages = [{"role": "brief", "text": brief}]
        tool_calls_made = 0
        cost_nano = 0
        for call_no in range(self.max_model_calls):
            reservation = None
            if self.model_estimate_nano > 0:
                try:
                    reservation = self.tools.ledger.reserve(
                        task_id, self.model_estimate_nano)
                except LedgerError as exc:
                    return self._escalated(
                        task_id, brief, cost_nano,
                        f"spend cap refused model call: {exc}")
            # Convergence: the final model call, and any call after the
            # tools run out, is offered no tools, so exploration cannot
            # spend the budget answering was meant for. Tool calls in a
            # stripped reply are void.
            tools_out = tool_calls_made >= self.max_tool_calls
            last_call = call_no == self.max_model_calls - 1
            converging = tools_out or last_call
            offered = [] if converging \
                else self.tools.registry.definitions()
            reply = self.model.complete(messages, tools=offered)
            if reservation is not None:
                actual = getattr(self.model, "last_cost_nano", 0) or 0
                self.tools.ledger.settle(reservation, actual)
                cost_nano += actual
            calls = [] if converging else reply.get("tool_calls", [])
            self.tools.receipts.append(
                agent_id=self.agent_id, task_id=task_id, action="model_call",
                detail={"finish": "tools" if calls else "answer"},
                outcome="ok")
            if not calls:
                text = reply.get("text", "")
                if text == ESCALATE_TEXT or not text:
                    if tools_out:
                        return self._escalated(task_id, brief, cost_nano,
                                               "tool budget exhausted")
                    if last_call:
                        return self._escalated(task_id, brief, cost_nano,
                                               "model budget exhausted")
                    return AgentMessage(
                        sender=self.agent_id, task_id=task_id,
                        claim=f"{ESCALATE_TEXT}: {brief[:80]}",
                        cost_nano=cost_nano, escalated=True)
                return AgentMessage(
                    sender=self.agent_id, task_id=task_id, claim=text,
                    evidence=reply.get("evidence", []),
                    confidence=reply.get("confidence", 0.0),
                    cost_nano=cost_nano)
            tools_left = self.max_tool_calls - tool_calls_made
            models_left = self.max_model_calls - (call_no + 1)
            if tools_left <= 2 or models_left <= 1:
                messages.append(
                    {"role": "budget",
                     "text": f"{tools_left} tool calls and {models_left}"
                             " model calls remain. Finish reading and answer"
                             " now; do not start new exploration."})
            for call in calls:
                if tool_calls_made >= self.max_tool_calls:
                    return self._escalated(task_id, brief, cost_nano,
                                           "tool budget exhausted")
                tool_calls_made += 1
                name = call.get("tool", "")
                args = call.get("args", {})
                # Model-supplied labels are ignored: the registry declares
                # the tool's class and cost.
                try:
                    out = self.tools.call(name, args)
                except NeedsApproval as exc:
                    return self._escalated(
                        task_id, brief, cost_nano,
                        f"needs approval: {exc.request['action_class']}"
                        f" {exc.request['tool']}")
                except ToolDenied as exc:
                    return self._escalated(
                        task_id, brief, cost_nano, f"tool denied: {exc}")
                except ToolFailed as exc:
                    messages.append({"role": "tool_error",
                                     "text": f"{name} failed (seq {exc.seq})"})
                    continue
                cost_nano += out["cost_nano"]
                full = str(out["result"])
                if len(full) > MAX_TOOL_CHARS:
                    text = (full[:MAX_TOOL_CHARS] +
                            f"\n[truncated: showing {MAX_TOOL_CHARS} of"
                            f" {len(full)} chars]")
                else:
                    text = full
                messages.append({"role": "tool_result", "tool": name,
                                 "args": args, "text": text})
        return self._escalated(task_id, brief, cost_nano,
                               "model budget exhausted")

    def _escalated(self, task_id: str, brief: str, cost_nano: int,
                   why: str) -> AgentMessage:
        return AgentMessage(sender=self.agent_id, task_id=task_id,
                            claim=f"{ESCALATE_TEXT}: {why} [{brief[:60]}]",
                            cost_nano=cost_nano, escalated=True)
