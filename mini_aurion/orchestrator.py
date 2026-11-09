"""Orchestrator: hub-spoke task runner with critic verification.

Flow: plan subtasks (or run the question as one subtask when the
contract leaves decomposition off) → delegate each to a specialist
→ critic-check every answer → one retry on failure → synthesize accepted
answers. Escalations and exhausted budgets produce partial results, never
guesses. Task start/end and every delegation are receipted; inter-agent
chatter is capped by a message budget.
"""
from __future__ import annotations

from .contract import TaskContract, contract_hash, render_for_planner
from .ledger import LedgerError
from .messages import AgentMessage


class _ModelRefused(Exception):
    pass


class Orchestrator:
    def __init__(self, agent_id: str, model, specialists: dict,
                 critic, *, receipts, ledger, message_budget: int = 10,
                 model_estimate_nano: int,
                 contract: TaskContract | None = None,
                 acceptance: tuple = (),
                 planner_prompt: str | None = None,
                 judge_prompt: str | None = None):
        """model_estimate_nano is required: 0 asserts the builder knows the
        model is free. Metering is never silently off. With a contract, the
        plan is validated against its criteria and completion requires every
        acceptance check; acceptance holds (criterion id, checker) pairs."""
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
        self.contract = contract
        self.acceptance = tuple(acceptance)
        self.planner_prompt = planner_prompt
        self.judge_prompt = judge_prompt
        self.messages_used = 0

    def _complete(self, task_id: str, messages: list,
                  system: str | None = None) -> dict:
        reservation = None
        if self.model_estimate_nano > 0:
            try:
                reservation = self.ledger.reserve(task_id,
                                                  self.model_estimate_nano)
            except LedgerError as exc:
                raise _ModelRefused(str(exc)) from exc
        if system is None:
            reply = self.model.complete(messages, tools=[])
        else:
            reply = self.model.complete(messages, tools=[],
                                        system_prompt=system)
        if reservation is not None:
            actual = getattr(self.model, "last_cost_nano", 0) or 0
            self.ledger.settle(reservation, actual)
        return reply

    def run(self, task_id: str, question: str) -> dict:
        self.messages_used = 0
        start_detail: dict = {"question": question[:500]}
        if self.contract is not None:
            start_detail["contract"] = contract_hash(self.contract)
        self.receipts.append(agent_id=self.agent_id, task_id=task_id,
                             action="task_start", detail=start_detail,
                             outcome="ok")
        try:
            return self._run_inner(task_id, question)
        except Exception as exc:  # noqa: BLE001 - every task ends with task_end
            self.receipts.append(
                agent_id=self.agent_id, task_id=task_id, action="task_end",
                detail={"status": "escalated",
                        "error": f"{type(exc).__name__}: {exc}"[:300]},
                outcome="escalated")
            try:
                cost_nano = self.ledger.spend(task_id)["settled_nano"]
            except Exception:  # noqa: BLE001 - reporting never breaks the guard
                cost_nano = 0
            return {"task_id": task_id, "status": "escalated",
                    "text": f"CANNOT_VERIFY: orchestrator error: {exc}"[:300],
                    "evidence": [], "cost_nano": cost_nano, "findings": [],
                    "unattempted": [], "cause": "error"}

    def _run_inner(self, task_id: str, question: str) -> dict:
        plan_messages = [{"role": "question", "text": question}]
        system = None
        if self.contract is not None:
            plan_messages.append(
                {"role": "contract",
                 "text": render_for_planner(self.contract,
                                            sorted(self.specialists))})
            system = self.planner_prompt
        if self.contract is not None and not self.contract.decompose:
            # Decomposition is opt-in: without it the question runs as
            # one subtask and the planner model is never called. The
            # empty plan reuses the validated default-subtask fallback,
            # so criteria tags, scope, acceptance, and judge still apply.
            plan = {"subtasks": []}
        else:
            try:
                plan = self._complete(task_id, plan_messages, system=system)
            except _ModelRefused as exc:
                return self._finish(
                    task_id, [], [], "escalated",
                    f"CANNOT_VERIFY: plan refused: {exc}"[:300],
                    [], f"plan refused: {exc}", cause="cap")
        subtasks, refused = self._validate_plan(
            task_id, plan.get("subtasks", []), question)
        findings: list[dict] = refused
        unattempted: list[str] = []
        for sub in subtasks:
            if self.messages_used >= self.message_budget:
                unattempted.append(sub["id"])
                continue
            findings.append(self._delegate(task_id, sub, question))
        accepted = [f for f in findings if f["status"] == "accepted"]
        synth_note = ""
        if not accepted:
            return self._finish(task_id, findings, unattempted, "escalated",
                                "CANNOT_VERIFY: no accepted findings", [], "")
        try:
            # Synthesis sees the question, not just the claims: without it
            # the summary drifts and the critic usually sends it to fallback.
            synthesis = self._complete(
                task_id, [{"role": "question", "text": question}] +
                [{"role": f["subtask"], "text": f["message"].claim}
                 for f in accepted])
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
        # The marker rides on its own line: glued onto the first claim line
        # it eats a leading bullet ("PARTIAL: - ..." is not a bullet).
        fallback = "PARTIAL:\n" + "; ".join(
            f["message"].claim for f in accepted)[:2000]
        evidence = []
        for finding in accepted:
            evidence.extend(finding["message"].evidence)
        return self._finish(task_id, findings, unattempted, "partial",
                            fallback, evidence, synth_note)

    def _finish(self, task_id: str, findings: list, unattempted: list,
                status: str, text: str, evidence: list, note: str,
                cause: str = "model") -> dict:
        # The ledger is the cost signal: summing messages drops every failed
        # attempt the retry replaced.
        cost_nano = self.ledger.spend(task_id)["settled_nano"]
        candidate = {"status": status, "text": text, "evidence": evidence,
                     "findings": findings, "unattempted": unattempted}
        results = {}
        for cid, checker in self.acceptance:
            try:
                results[cid] = bool(checker(candidate))
            except Exception:
                results[cid] = False
        failed = [cid for cid, ok in results.items() if not ok]
        if status == "complete" and failed:
            status = "partial"
            note = ((note + "; ") if note else ""
                    + "acceptance failed: " + ",".join(failed))
        detail: dict = {"status": status,
                        "accepted": [f["subtask"] for f in findings
                                     if f["status"] == "accepted"],
                        "unattempted": unattempted,
                        "acceptance": results}
        if note:
            detail["note"] = note
        self.receipts.append(agent_id=self.agent_id, task_id=task_id,
                             action="task_end", detail=detail,
                             outcome="ok" if status == "complete" else status)
        return {"task_id": task_id, "status": status, "text": text,
                "evidence": evidence, "cost_nano": cost_nano,
                "findings": findings, "unattempted": unattempted,
                "cause": cause}

    def _validate_plan(self, task_id: str, raw, question: str):
        """Split the planner's output into runnable subtasks and refusals.
        Under a contract every subtask names known criterion ids, or it is
        refused with a receipt. A structurally unusable plan falls back to
        the single default subtask; all-refused does not (that would dodge
        the refusal)."""
        default = {"id": "s1", "brief": question,
                   "specialist": next(iter(self.specialists), "none")}
        if self.contract is None:
            return (raw or [default]), []
        if not isinstance(raw, list) or not raw:
            default["criteria"] = sorted(self.contract.criterion_ids())
            return [default], []
        known = self.contract.criterion_ids()
        runnable, refused = [], []
        for sub in raw:
            if not isinstance(sub, dict):
                continue
            sid = sub.get("id", "?")
            if not isinstance(sub.get("brief"), str) or not sub["brief"]:
                continue
            claimed = sub.get("criteria", [])
            unknown = [c for c in claimed if c not in known]
            if not claimed or unknown:
                reason = ("names no criterion" if not claimed
                          else f"names unknown criteria {unknown}")
                self.receipts.append(
                    agent_id=self.agent_id, task_id=task_id,
                    action="delegate",
                    detail={"subtask": sid,
                            "to": sub.get("specialist", "?"),
                            "brief": sub["brief"][:300],
                            "reason": reason},
                    outcome="denied")
                refused.append(
                    {"subtask": sid, "status": "refused", "reason": reason,
                     "message": AgentMessage(
                         sender=self.agent_id, task_id=task_id,
                         claim=f"refused: {reason}", cost_nano=0,
                         escalated=True)})
            else:
                runnable.append(sub)
        if not runnable and not refused:
            default["criteria"] = sorted(known)
            return [default], []
        return runnable, refused

    def _delegate(self, task_id: str, sub: dict, question: str) -> dict:
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
                             detail={"subtask": sub["id"], "to": name,
                                     "brief": str(sub.get("brief", ""))[:300]},
                             outcome="ok")
        message = self._investigate(task_id, specialist, sub["brief"])
        check = self.critic.check(message)
        self._record_check(task_id, sub["id"], message, check, attempt=1)
        if check["verdict"] == "pass":
            if self._judge(task_id, question, sub, message):
                return {"subtask": sub["id"], "status": "flagged",
                        "message": message}
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
            if self._judge(task_id, question, sub, retry):
                return {"subtask": sub["id"], "status": "flagged",
                        "message": retry}
            return {"subtask": sub["id"], "status": "accepted",
                    "message": retry}
        return {"subtask": sub["id"], "status": "escalated", "message": retry}

    def _judge(self, task_id: str, question: str, sub: dict,
               message) -> bool:
        """Flag-only relevance check on a passed finding. True flags it.
        Runs only when a judge prompt is configured. Unparseable replies
        and judge errors keep the finding: the judge is a backstop, and
        its own failure is receipted, never silent."""
        if self.judge_prompt is None:
            return False
        try:
            reply = self._complete(
                task_id, [{"role": "question", "text": question},
                          {"role": "finding", "text": message.claim}],
                system=self.judge_prompt)
            verdict, _, reason = reply.get("text", "").partition(":")
            decision = verdict.strip().casefold()
            if decision not in ("relevant", "irrelevant"):
                raise ValueError(
                    f"unparseable judge reply: {reply.get('text', '')[:50]!r}")
        except Exception as exc:
            self.receipts.append(
                agent_id=self.agent_id, task_id=task_id,
                action="relevance_check",
                detail={"subtask": sub.get("id", "?"), "verdict": "error",
                        "reason": f"{type(exc).__name__}: {exc}"[:200]},
                outcome="ok")
            return False
        self.receipts.append(
            agent_id=self.agent_id, task_id=task_id,
            action="relevance_check",
            detail={"subtask": sub.get("id", "?"), "verdict": decision,
                    "reason": reason.strip()[:200]},
            outcome="ok")
        return decision == "irrelevant"

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
