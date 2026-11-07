"""Orchestrator: hub-spoke task runner with critic verification.

Flow: decompose the question into subtasks → delegate each to a specialist
→ critic-check every answer → one retry on failure → synthesize accepted
answers. Escalations and exhausted budgets produce partial results, never
guesses. Task start/end and every delegation are receipted; inter-agent
chatter is capped by a message budget.
"""
from __future__ import annotations

from .messages import AgentMessage


class Orchestrator:
    def __init__(self, agent_id: str, model, specialists: dict,
                 critic, *, receipts, message_budget: int = 10):
        self.agent_id = agent_id
        self.model = model
        self.specialists = dict(specialists)
        self.critic = critic
        self.receipts = receipts
        self.message_budget = message_budget
        self.messages_used = 0

    def run(self, task_id: str, question: str) -> dict:
        self.receipts.append(agent_id=self.agent_id, task_id=task_id,
                             action="task_start",
                             detail={"question": question[:500]}, outcome="ok")
        plan = self.model.complete([{"role": "question", "text": question}],
                                   tools=[])
        subtasks = plan.get("subtasks", []) or [
            {"id": "s1", "brief": question,
             "specialist": next(iter(self.specialists))}]
        findings: list[dict] = []
        unattempted: list[str] = []
        for sub in subtasks:
            if self.messages_used >= self.message_budget:
                unattempted.append(sub["id"])
                continue
            findings.append(self._delegate(task_id, sub))
        accepted = [f for f in findings if f["status"] == "accepted"]
        if accepted:
            synthesis = self.model.complete(
                [{"role": f["subtask"],
                  "text": f["message"].claim} for f in accepted], tools=[])
            final_text = synthesis.get("text", "")
            evidence = []
            for finding in accepted:
                evidence.extend(finding["message"].evidence)
        else:
            final_text, evidence = "CANNOT_VERIFY: no accepted findings", []
        cost_nano = sum(f["message"].cost_nano for f in findings)
        if not accepted:
            status = "escalated"
        elif unattempted or len(accepted) < len(findings):
            status = "partial"
        else:
            status = "complete"
        self.receipts.append(
            agent_id=self.agent_id, task_id=task_id, action="task_end",
            detail={"status": status,
                    "accepted": [f["subtask"] for f in accepted],
                    "unattempted": unattempted},
            outcome="ok" if status == "complete" else status)
        return {"task_id": task_id, "status": status, "text": final_text,
                "evidence": evidence, "cost_nano": cost_nano,
                "findings": findings, "unattempted": unattempted}

    def _delegate(self, task_id: str, sub: dict) -> dict:
        name = sub.get("specialist", next(iter(self.specialists)))
        specialist = self.specialists[name]
        self.receipts.append(agent_id=self.agent_id, task_id=task_id,
                             action="delegate",
                             detail={"subtask": sub["id"], "to": name},
                             outcome="ok")
        message = self._investigate(task_id, specialist, sub["brief"])
        check = self.critic.check(message)
        if check["verdict"] == "pass":
            return {"subtask": sub["id"], "status": "accepted",
                    "message": message}
        if check["verdict"] == "escalated":
            return {"subtask": sub["id"], "status": "escalated",
                    "message": message}
        if self.messages_used >= self.message_budget:
            return {"subtask": sub["id"], "status": "escalated",
                    "message": message}
        retry = self._investigate(
            task_id, specialist,
            f"{sub['brief']} [previous answer failed: {'; '.join(check['catches'])[:300]}]")
        recheck = self.critic.check(retry)
        if recheck["verdict"] == "pass":
            return {"subtask": sub["id"], "status": "accepted",
                    "message": retry}
        return {"subtask": sub["id"], "status": "escalated", "message": retry}

    def _investigate(self, task_id: str, specialist, brief: str) -> AgentMessage:
        self.messages_used += 1
        return specialist.investigate(task_id, brief)
