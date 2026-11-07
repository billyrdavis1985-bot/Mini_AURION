"""Critic: tool-less verification of cited evidence.

The critic judges; it never gathers. It has no tools (proven by canary
test, not trusted from a flag) and resolves every citation mechanically
through the memory's write-time checker. A claim without evidence fails.
An escalation is acknowledged, not graded.
"""
from __future__ import annotations

from .messages import AgentMessage, MessageError


class Critic:
    def __init__(self, agent_id: str, memory):
        self.agent_id = agent_id
        self.memory = memory
        self.catches = 0

    def check(self, message: AgentMessage) -> dict:
        if message.escalated:
            return {"verdict": "escalated", "catches": [],
                    "by": self.agent_id}
        try:
            message.require_evidence()
        except MessageError as exc:
            self.catches += 1
            return {"verdict": "fail", "catches": [str(exc)],
                    "by": self.agent_id}
        catches = []
        for item in message.evidence:
            reason = self.memory.check_source(item)
            if reason is not None:
                catches.append(f"{item.get('kind')}:{item.get('ref')}: {reason}")
        if catches:
            self.catches += len(catches)
            return {"verdict": "fail", "catches": catches, "by": self.agent_id}
        return {"verdict": "pass", "catches": [], "by": self.agent_id}
