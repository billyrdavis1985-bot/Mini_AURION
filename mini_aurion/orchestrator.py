"""Orchestrator: hub-spoke task runner with critic verification.

Flow: decompose the question into subtasks → delegate each to a specialist
→ critic-check every answer → one retry on failure → synthesize accepted
answers. Escalations and exhausted budgets produce partial results, never
guesses. Task start/end and every delegation are receipted; inter-agent
chatter is capped by a message budget.
"""
from __future__ import annotations

from .ledger import LedgerError
from .messages import AgentMessage


class _ModelRefused(Exception):
    pass


class Orchestrator:
    def __init__(self, agent_id: str, model, specialists: dict,
                 critic, *, receipts, ledger, message_budget: int = 10,
                 model_estimate_nano: int):
        """model_estimate_nano is required: 0 asserts the builder knows the
        model is free. Metering is never silently off."""
        if not isinstance(model_estimate_nano, int) or model_estimate_nano < 0:
            raise ValueError("model_estimate_nano must be a non-negative int")
        self.agent_id = agent_id
        self.model = model
        self.specialists = dict(specialists)
        self.critic = critic
        self.receipts = receipts
        self.ledger = ledger
        self.message_budget = message_budget
        self.model_estimate_nano = model_estimate_nano
        self.messages_used = 0
        self._model_spend = 0

    def _complete(self, task_id: str, messages: list) -> dict:
        reservation = None
        if self.model_estimate_nano > 0:
            try:
                reservation = self.ledger.reserve(task_id,
                                                  self.model_estimate_nano)
            except LedgerError as exc:
                raise _ModelRefused(str(exc)) from exc
        reply = self.model.complete(messages, tools=[])
        if reservation is not None:
            actual = getattr(self.model, "last_cost_nano", 0) or 0
            self.ledger.settle(reservation, actual)
            self._model_spend += actual
        return reply

    def run(self, task_id: str, question: str) -> dict:
        self.messages_used = 0
        self._model_spend = 0
        self.receipts.append(agent_id=self.agent_id, task_id=task_id,
                             action="task_start",
                             detail={"question": question[:500]}, outcome="ok")
        try:
            return self._run_inner(task_id, question)
        except Exception as exc:  # noqa: BLE001 - every task ends with task_end
            self.receipts.append(
                agent_id=self.agent_id, task_id=task_id, action="task_end",
                detail={"status": "escalated",
                        "error": f"{type(exc).__name__}: {exc}"[:300]},
                outcome="escalated")
            return {"task_id": task_id, "status": "escalated",
                    "text": f"CANNOT_VERIFY: orchestrator error: {exc}"[:300],
                    "evidence": [], "cost_nano": 0, "findings": [],
                    "unattempted": []}

    def _run_inner(self, task_id: str, question: str) -> dict:
        try:
            plan = self._complete(task_id, [{"role": "question",
                                             "text": question}])
        except _ModelRefused as exc:
            return self._finish(task_id, [], [], "escalated",
                                f"CANNOT_VERIFY: plan refused: {exc}"[:300],
                                [], f"plan refused: {exc}")
        subtasks = plan.get("subtasks", []) or [
            {"id": "s1", "brief": question,
             "specialist": next(iter(self.specialists), "none")}]
        findings: list[dict] = []
        unattempted: list[str] = []
        for sub in subtasks:
            if self.messages_used >= self.message_budget:
                unattempted.append(sub["id"])
                continue
            findings.append(self._delegate(task_id, sub))
        accepted = [f for f in findings if f["status"] == "accepted"]
        synth_note = ""
        if not accepted:
            return self._finish(task_id, findings, unattempted, "escalated",
                                "CANNOT_VERIFY: no accepted findings", [], "")
        try:
            synthesis = self._complete(
                task_id, [{"role": f["subtask"],
                           "text": f["message"].claim} for f in accepted])
        except _ModelRefused as exc:
            synthesis = None
            synth_note = f"synthesis refused: {exc}"
        if synthesis is not None:
            synth_msg = AgentMessage(
                sender=self.agent_id, task_id=task_id,
                claim=synthesis.get("text") or "(empty synthesis)",
                evidence=synthesis.get("evidence", []))
            check = self.critic.check(synth_msg)
            if check["verdict"] == "pass":
                cited = {(e.get("kind"), e.get("ref"))
                         for f in accepted for e in f["message"].evidence}
                fresh = [(e.get("kind"), e.get("ref"))
                         for e in synth_msg.evidence]
                if all(pointer in cited for pointer in fresh):
                    return self._finish(
                        task_id, findings, unattempted,
                        "complete" if not unattempted
                        and len(accepted) == len(findings) else "partial",
                        synth_msg.claim, synth_msg.evidence, "")
                synth_note = ("synthesis cited evidence no finding cited: "
                              f"{[p for p in fresh if p not in cited]}")[:300]
            else:
                synth_note = (f"synthesis failed critic:"
                              f" {check['catches']}")[:300]
        fallback = "PARTIAL: " + "; ".join(
            f["message"].claim for f in accepted)[:2000]
        evidence = []
        for finding in accepted:
            evidence.extend(finding["message"].evidence)
        return self._finish(task_id, findings, unattempted, "partial",
                            fallback, evidence, synth_note)

    def _finish(self, task_id: str, findings: list, unattempted: list,
                status: str, text: str, evidence: list,
                note: str) -> dict:
        cost_nano = (sum(f["message"].cost_nano for f in findings)
                     + self._model_spend)
        detail: dict = {"status": status,
                        "accepted": [f["subtask"] for f in findings
                                     if f["status"] == "accepted"],
                        "unattempted": unattempted}
        if note:
            detail["note"] = note
        self.receipts.append(agent_id=self.agent_id, task_id=task_id,
                             action="task_end", detail=detail,
                             outcome="ok" if status == "complete" else status)
        return {"task_id": task_id, "status": status, "text": text,
                "evidence": evidence, "cost_nano": cost_nano,
                "findings": findings, "unattempted": unattempted}

    def _delegate(self, task_id: str, sub: dict) -> dict:
        name = sub.get("specialist", next(iter(self.specialists), "none"))
        specialist = self.specialists.get(name)
        if specialist is None:
            self.receipts.append(
                agent_id=self.agent_id, task_id=task_id, action="delegate",
                detail={"subtask": sub.get("id", "?"), "to": name,
                        "error": "unknown specialist"},
                outcome="escalated")
            return {"subtask": sub.get("id", "?"), "status": "escalated",
                    "message": AgentMessage(
                        sender=self.agent_id, task_id=task_id,
                        claim=f"CANNOT_VERIFY: unknown specialist {name!r}",
                        escalated=True)}
        self.receipts.append(agent_id=self.agent_id, task_id=task_id,
                             action="delegate",
                             detail={"subtask": sub["id"], "to": name},
                             outcome="ok")
        message = self._investigate(task_id, specialist, sub["brief"])
        check = self.critic.check(message)
        self._record_check(task_id, sub["id"], message, check, attempt=1)
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
        self._record_check(task_id, sub["id"], retry, recheck, attempt=2)
        if recheck["verdict"] == "pass":
            return {"subtask": sub["id"], "status": "accepted",
                    "message": retry}
        return {"subtask": sub["id"], "status": "escalated", "message": retry}

    def _record_check(self, task_id: str, subtask: str, message,
                      check: dict, attempt: int) -> None:
        """Receipt the verdict and the claim behind it. Debugging a run
        without the claims is guessing."""
        self.receipts.append(
            agent_id=self.agent_id, task_id=task_id, action="critic_check",
            detail={"subtask": subtask, "attempt": attempt,
                    "verdict": check["verdict"],
                    "catches": check["catches"][:5],
                    "claim": message.claim[:500],
                    "evidence": message.evidence[:10]}, outcome="ok")

    def _investigate(self, task_id: str, specialist, brief: str) -> AgentMessage:
        self.messages_used += 1
        return specialist.investigate(task_id, brief)
