"""Specialist agent: bounded tool loop behind the trust wrapper.

The model investigates: it may call tools (each gated, receipted, and
costed by ToolWrapper) up to fixed limits. When it stops calling tools, its
final reply becomes a typed message. If it says CANNOT_VERIFY, or the
budget runs out, or a tool needs human approval, the result is an
escalation — never a guess.
"""
from __future__ import annotations

from .fixture import ESCALATE_TEXT
from .messages import AgentMessage
from .tools import NeedsApproval, ToolFailed


class Specialist:
    def __init__(self, agent_id: str, model, tools, *,
                 max_model_calls: int = 4, max_tool_calls: int = 4):
        self.agent_id = agent_id
        self.model = model
        self.tools = tools
        self.max_model_calls = max_model_calls
        self.max_tool_calls = max_tool_calls

    def investigate(self, task_id: str, brief: str) -> AgentMessage:
        messages = [{"role": "brief", "text": brief}]
        tool_calls_made = 0
        cost_nano = 0
        for _ in range(self.max_model_calls):
            reply = self.model.complete(messages, tools=["*"])
            self.tools.receipts.append(
                agent_id=self.agent_id, task_id=task_id, action="model_call",
                detail={"finish": "tools" if reply["tool_calls"] else "answer"},
                outcome="ok")
            calls = reply.get("tool_calls", [])
            if not calls:
                text = reply.get("text", "")
                if text == ESCALATE_TEXT or not text:
                    return AgentMessage(
                        sender=self.agent_id, task_id=task_id,
                        claim=f"{ESCALATE_TEXT}: {brief[:80]}",
                        cost_nano=cost_nano, escalated=True)
                return AgentMessage(
                    sender=self.agent_id, task_id=task_id, claim=text,
                    evidence=reply.get("evidence", []),
                    confidence=reply.get("confidence", 0.0),
                    cost_nano=cost_nano)
            for call in calls:
                if tool_calls_made >= self.max_tool_calls:
                    return self._escalated(task_id, brief, cost_nano,
                                           "tool budget exhausted")
                tool_calls_made += 1
                name = call.get("tool", "")
                args = call.get("args", {})
                action_class = call.get("class", "READ")
                try:
                    out = self.tools.call(name, args, action_class)
                except NeedsApproval as exc:
                    return self._escalated(
                        task_id, brief, cost_nano,
                        f"needs approval: {exc.request['action_class']}"
                        f" {exc.request['tool']}")
                except ToolFailed as exc:
                    messages.append({"role": "tool_error",
                                     "text": f"{name} failed (seq {exc.seq})"})
                    continue
                cost_nano += out["cost_nano"]
                messages.append({"role": "tool_result", "tool": name,
                                 "text": str(out["result"])[:2000]})
        return self._escalated(task_id, brief, cost_nano,
                               "model budget exhausted")

    def _escalated(self, task_id: str, brief: str, cost_nano: int,
                   why: str) -> AgentMessage:
        return AgentMessage(sender=self.agent_id, task_id=task_id,
                            claim=f"{ESCALATE_TEXT}: {why} [{brief[:60]}]",
                            cost_nano=cost_nano, escalated=True)
