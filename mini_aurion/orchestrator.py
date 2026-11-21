"""Orchestrator: hub-spoke task runner with critic verification.

Flow: plan subtasks (or run the question as one subtask when the
contract leaves decomposition off) → delegate each to a specialist
→ critic-check every answer → one retry on failure → synthesize accepted
answers. Escalations and exhausted budgets produce partial results, never
guesses. Task start/end and every delegation are receipted; inter-agent
chatter is capped by a message budget.
"""
from __future__ import annotations

import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext

from .contract import TaskContract, contract_hash, render_for_planner
from .ledger import LedgerError
from .memory import MemoryError
from .messages import AgentMessage
from .receipts import ReceiptError
from .research import (SUPPORT_PROMPT, SUPPORT_REASON_PROMPT,
                       parse_research_reply, parse_support_verdict,
                       register_forecasts)


class _ModelRefused(Exception):
    pass


SYNTH_FORMAT = ("Reply in the brief's requested format. Cite evidence "
                "as path:line plus the exact quoted text (12+ characters "
                "copied from that line). Cite supporting notes by their "
                "[mem_...] id; cite only notes given above.")
NOTE_ID_RE = re.compile(r"mem_[0-9a-f]{16}")


class Orchestrator:
    def __init__(self, agent_id: str, model, specialists: dict,
                 critic, *, receipts, ledger, message_budget: int = 10,
                 model_estimate_nano: int,
                 contract: TaskContract | None = None,
                 acceptance: tuple = (),
                 planner_prompt: str | None = None,
                 judge_prompt: str | None = None,
                 verifier_model=None,
                 routing_hash: str | None = None,
                 max_workers: int = 1):
        """model_estimate_nano is required: 0 asserts the builder knows the
        model is free. Metering is never silently off. With a contract, the
        plan is validated against its criteria and completion requires every
        acceptance check; acceptance holds (criterion id, checker) pairs.
        max_workers above 1 delegates subtasks on threads; findings still
        merge in subtask order, never finish order. Sharing rules: one
        wrapper per specialist (the duplicate cache points at content
        "above in this investigation"); models may be shared (the
        complete+cost read is locked) but per-specialist models keep
        calls from serializing. The stores, the message budget, and
        the critic's counter are locked for sharing."""
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
        # The judge runs on the verifier seat: independence from the
        # specialist is structural (different model object), not a hope.
        # Verifier identity comes from the router via this constructor,
        # never from a string an agent supplies.
        self.verifier_model = verifier_model or model
        self.routing_hash = routing_hash
        if not isinstance(max_workers, int) or max_workers < 1:
            raise ValueError("max_workers must be a positive int")
        self.max_workers = max_workers
        self._mu = threading.Lock()
        self.messages_used = 0
        self._ckpt_plan: list = []
        self._ckpt_settled: dict = {}
        self._ckpt_refused: list = []
        self._ckpt_goal = ""

    def _messages_spent(self) -> int:
        with self._mu:
            return self.messages_used

    def _spend_message(self) -> None:
        with self._mu:
            self.messages_used += 1

    def _complete(self, task_id: str, messages: list,
                  system: str | None = None,
                  role: str = "complete", model=None) -> dict:
        model = model or self.model
        reservation = None
        if self.model_estimate_nano > 0:
            try:
                reservation = self.ledger.reserve(
                    task_id, self.model_estimate_nano,
                    provider=getattr(model, "provider", ""),
                    model=getattr(model, "model", ""))
            except LedgerError as exc:
                raise _ModelRefused(str(exc)) from exc
        call_started = time.perf_counter()
        # complete() + the cost read-back is one atomic unit: parallel
        # delegates share this model for relevance judges, and a bare
        # read could settle with another call's cost. Models without a
        # lock predate threads; they keep the old serial behavior.
        model_mu = getattr(model, "mu", None) or nullcontext()
        with model_mu:
            if system is None:
                reply = model.complete(messages, tools=[])
            else:
                reply = model.complete(messages, tools=[],
                                       system_prompt=system)
            call_ms = int((time.perf_counter() - call_started) * 1000)
            if reservation is not None:
                actual = getattr(model, "last_cost_nano", 0) or 0
                self.ledger.settle(reservation, actual)
        # The orchestrator's own calls are receipted like the
        # specialist's: without this the judge and synthesis are
        # invisible, and no receipt says which model judged.
        self.receipts.append(
            agent_id=self.agent_id, task_id=task_id, action="model_call",
            detail={"finish": "answer", "role": role,
                    "provider": getattr(model, "provider", "fixture"),
                    "model": getattr(model, "model", "?"),
                    "duration_ms": call_ms},
            outcome="ok")
        return reply

    def run(self, task_id: str, question: str) -> dict:
        self.messages_used = 0
        self._ckpt_plan = []
        self._ckpt_settled = {}
        self._ckpt_refused = []
        self._ckpt_goal = question
        start_detail: dict = {"question": question[:500]}
        if self.contract is not None:
            start_detail["contract"] = contract_hash(self.contract)
        if self.routing_hash is not None:
            start_detail["routing"] = self.routing_hash
        self.receipts.append(agent_id=self.agent_id, task_id=task_id,
                             action="task_start", detail=start_detail,
                             outcome="ok")
        return self._guarded(
            task_id, lambda: self._run_inner(task_id, question))

    def _guarded(self, task_id: str, fn) -> dict:
        """Run to task_end even when the body raises. Fresh runs and
        resumes share the guard: a crash mid-resume ends the chain
        honestly instead of leaving a second open wound."""
        self._run_started = time.perf_counter()
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 - every task ends with task_end
            self.receipts.append(
                agent_id=self.agent_id, task_id=task_id, action="task_end",
                detail={"status": "escalated",
                        "error": f"{type(exc).__name__}: {exc}"[:300],
                        "duration_ms": self._run_ms()},
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
                plan = self._complete(task_id, plan_messages, system=system,
                                      role="plan")
            except _ModelRefused as exc:
                return self._finish(
                    task_id, [], [], "escalated",
                    f"CANNOT_VERIFY: plan refused: {exc}"[:300],
                    [], f"plan refused: {exc}", cause="cap")
        subtasks, refused = self._validate_plan(
            task_id, plan.get("subtasks", []), question)
        if refused and not subtasks:
            plan_verdict = "refused"
        elif refused:
            plan_verdict = "partial"
        else:
            plan_verdict = "accepted"
        self.receipts.append(
            agent_id=self.agent_id, task_id=task_id, action="plan",
            detail={"verdict": plan_verdict,
                    "subtasks": [sub.get("id", "?") for sub in subtasks
                                 if isinstance(sub, dict)],
                    "refused": [f["subtask"] for f in refused]},
            outcome="ok" if subtasks else "denied")
        self._ckpt_plan = [dict(s) for s in subtasks]
        self._ckpt_settled = {}
        self._ckpt_refused = [
            {"subtask": f["subtask"], "reason": f["reason"]}
            for f in refused]
        self._checkpoint(task_id, "plan")
        fresh, unattempted = self._run_subtasks(task_id, question, subtasks)
        return self._synthesize_and_finish(
            task_id, question, refused + fresh, unattempted)

    def _run_subtasks(self, task_id: str, question: str,
                      subtasks: list) -> tuple[list, list]:
        """Delegate every subtask, serially or on threads. Fresh runs
        pass the validated plan; resumes pass only what never settled."""
        findings: list[dict] = []
        unattempted: list[str] = []
        if self.max_workers == 1:
            for sub in subtasks:
                if self.messages_used >= self.message_budget:
                    unattempted.append(sub["id"])
                    continue
                findings.append(self._delegate(task_id, sub, question))
        else:
            # Parallel admission is one pre-check per subtask: delegates
            # race, so checking inside each thread would admit by luck
            # of the draw. pool.map preserves input order, so the brief
            # merges by subtask, not by finish time.
            runnable = []
            for sub in subtasks:
                if self._messages_spent() >= self.message_budget:
                    unattempted.append(sub["id"])
                else:
                    runnable.append(sub)
            with ThreadPoolExecutor(
                    max_workers=self.max_workers,
                    thread_name_prefix="ma-spec") as pool:
                findings.extend(pool.map(
                    lambda sub: self._delegate(task_id, sub, question),
                    runnable))
        return findings, unattempted

    def _synthesize_and_finish(self, task_id: str, question: str,
                               findings: list, unattempted: list) -> dict:
        accepted = [f for f in findings if f["status"] == "accepted"]
        synth_note = ""
        if not accepted:
            return self._finish(task_id, findings, unattempted, "escalated",
                                "CANNOT_VERIFY: no accepted findings", [], "")
        # Verified-only synthesis: the prompt holds verified notes, never
        # raw claims. file_note can fail (the finding stands, the copy is
        # dropped), so re-check each note's live status here: fail closed
        # on what the store says now, not on filing memory.
        notes = []
        for finding in accepted:
            note_id = finding.get("note_id")
            if not note_id:
                continue
            try:
                entry = self.critic.memory.get(note_id)
            except MemoryError:
                continue
            if entry["status"] == "verified" \
                    and entry.get("task_id") == task_id:
                notes.append(entry)
        if not notes:
            return self._finish(
                task_id, findings, unattempted, "partial",
                "PARTIAL:\nCANNOT_VERIFY: no verified notes to synthesize",
                [], "synthesis refused: no verified notes")
        try:
            # Synthesis sees the question, not just the notes: without it
            # the summary drifts and the critic usually sends it to fallback.
            synthesis = self._complete(
                task_id, [{"role": "format", "text": SYNTH_FORMAT},
                          {"role": "question", "text": question}] +
                [{"role": "note", "text": f"[{n['id']}] {n['text']}"}
                 for n in notes],
                role="synthesis")
        except _ModelRefused as exc:
            synthesis = None
            synth_note = f"synthesis refused: {exc}"
        if synthesis is not None:
            bad_cites = [c for c in NOTE_ID_RE.findall(
                synthesis.get("text") or "")
                if c not in {n["id"] for n in notes}]
            if bad_cites:
                synthesis = None
                synth_note = ("synthesis cited notes outside this task's "
                              f"verified set: {bad_cites}")[:300]
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
        # Fallback draws from the same verified notes, never raw claims;
        # each note id rides on its own line for the same reason.
        fallback = "PARTIAL:\n" + "\n".join(
            f"[{n['id']}]\n{n['text']}" for n in notes)[:2000]
        evidence = []
        for note in notes:
            evidence.extend(note["sources"])
        return self._finish(task_id, findings, unattempted, "partial",
                            fallback, evidence, synth_note)

    def _run_ms(self) -> int:
        started = getattr(self, "_run_started", None)
        if started is None:
            return 0
        return int((time.perf_counter() - started) * 1000)

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
                        "acceptance": results,
                        "duration_ms": self._run_ms()}
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
                return self._settle(task_id, sub, "flagged", message)
            filed = self.critic.file_note(
                task_id, message.sender, message.claim,
                message.evidence)
            return self._settle(task_id, sub, "accepted", message,
                                note_id=(filed or {}).get("id"))
        if check["verdict"] == "escalated":
            return {"subtask": sub["id"], "status": "escalated",
                    "message": message}
        if self._messages_spent() >= self.message_budget:
            return {"subtask": sub["id"], "status": "escalated",
                    "message": message}
        retry = self._investigate(
            task_id, specialist,
            f"{sub['brief']} [previous answer failed: {'; '.join(check['catches'])[:300]}]")
        recheck = self.critic.check(retry)
        self._record_check(task_id, sub["id"], retry, recheck, attempt=2)
        if recheck["verdict"] == "pass":
            if self._judge(task_id, question, sub, retry):
                return self._settle(task_id, sub, "flagged", retry)
            filed = self.critic.file_note(
                task_id, retry.sender, retry.claim, retry.evidence)
            return self._settle(task_id, sub, "accepted", retry,
                                note_id=(filed or {}).get("id"))
        return {"subtask": sub["id"], "status": "escalated", "message": retry}

    def _settle(self, task_id: str, sub: dict, status: str,
                message, note_id=None) -> dict:
        """Record a terminal verdict and checkpoint it. Accepted and
        flagged both settle: a verdict either way. Escalations settle
        nothing — on resume they are retried, not replayed."""
        with self._mu:
            self._ckpt_settled[sub["id"]] = {
                "subtask": sub["id"], "status": status,
                "sender": message.sender, "claim": message.claim,
                "evidence": list(message.evidence), "note_id": note_id}
        self._checkpoint(task_id, "progress")
        finding: dict = {"subtask": sub["id"], "status": status,
                         "message": message}
        if status == "accepted":
            finding["note_id"] = note_id
        return finding

    def _checkpoint(self, task_id: str, phase: str) -> None:
        """Receipt the resumable state: the validated plan, every
        settled subtask, and the counters a resume continues. Only
        under a contract: without one there is nothing to check the
        resume against. Written after the plan lands and after every
        settled finding, on the serial and threaded paths alike;
        resume unions every checkpoint, so thread write order cannot
        lose a verdict."""
        if self.contract is None:
            return
        with self._mu:
            plan = [dict(s) for s in self._ckpt_plan]
            settled = [dict(self._ckpt_settled[k])
                       for k in sorted(self._ckpt_settled)]
            refused = [dict(r) for r in self._ckpt_refused]
            used = self.messages_used
            goal = self._ckpt_goal
        self.receipts.append(
            agent_id=self.agent_id, task_id=task_id, action="checkpoint",
            detail={"phase": phase, "plan": plan, "settled": settled,
                    "refused": refused, "goal": goal,
                    "contract": contract_hash(self.contract),
                    "messages_used": used},
            outcome="ok")

    def resume_task(self, task_id: str) -> dict:
        """Continue an interrupted task on its own chain: same task
        id, same receipts, same ledger and memory. The recorded plan
        is reused, never re-planned; only subtasks with no settled
        verdict are re-delegated, so completed work is never
        re-spent. Spend continues (same ledger) and the message
        count continues from the checkpoint. Refusals return status
        refused and append nothing: a refusal is an operator error,
        not a task event."""
        try:
            self.receipts.verify()
        except ReceiptError as exc:
            return {"task_id": task_id, "status": "refused",
                    "reason": f"chain broken: {exc}"}
        if self.contract is None:
            return {"task_id": task_id, "status": "refused",
                    "reason": "resume needs a contract"}
        prior = [r for r in self.receipts.read_all()
                 if r["task_id"] == task_id]
        if any(r["action"] == "task_end" for r in prior):
            return {"task_id": task_id, "status": "refused",
                    "reason": "task already ended"}
        checkpoints = [r for r in prior if r["action"] == "checkpoint"]
        if not checkpoints:
            return {"task_id": task_id, "status": "refused",
                    "reason": "no checkpoint: nothing to resume"}
        latest = checkpoints[-1]["detail"]
        if latest.get("contract") != contract_hash(self.contract):
            return {"task_id": task_id, "status": "refused",
                    "reason": "contract changed since the checkpoint"}
        goal = latest.get("goal", "")
        if not goal or not isinstance(goal, str):
            return {"task_id": task_id, "status": "refused",
                    "reason": "checkpoint has no goal"}
        settled: dict = {}
        for point in checkpoints:
            for record in point["detail"].get("settled", []):
                settled[record["subtask"]] = record
        plan = latest.get("plan", [])
        self._ckpt_plan = [dict(s) for s in plan]
        self._ckpt_settled = {k: dict(v) for k, v in settled.items()}
        self._ckpt_refused = [dict(r) for r in latest.get("refused", [])]
        self._ckpt_goal = goal
        self.messages_used = latest.get("messages_used", 0)
        replayed = []
        for sub in plan:
            record = settled.get(sub.get("id", "?"))
            if record is not None:
                replayed.append(self._replay(task_id, record))
        unfinished = [s for s in plan if s.get("id", "?") not in settled]
        refused = []
        for entry in self._ckpt_refused:
            refused.append(
                {"subtask": entry["subtask"], "status": "refused",
                 "reason": entry.get("reason", ""),
                 "message": AgentMessage(
                     sender=self.agent_id, task_id=task_id,
                     claim=f"refused: {entry.get('reason', '')}",
                     cost_nano=0, escalated=True)})
        self.receipts.append(
            agent_id=self.agent_id, task_id=task_id, action="resume",
            detail={"from_checkpoint": checkpoints[-1]["seq"],
                    "replayed": [f["subtask"] for f in replayed],
                    "re_delegating": [s.get("id", "?") for s in unfinished]},
            outcome="ok")

        def _continue():
            fresh, unattempted = self._run_subtasks(
                task_id, goal, unfinished)
            return self._synthesize_and_finish(
                task_id, goal, refused + replayed + fresh, unattempted)

        result = self._guarded(task_id, _continue)
        result["resumed"] = True
        result["replayed"] = [f["subtask"] for f in replayed]
        result["re_delegated"] = [s.get("id", "?") for s in unfinished]
        return result

    def _replay(self, task_id: str, record: dict) -> dict:
        """Rebuild a settled finding from its checkpoint record. The
        note itself is re-read live at synthesis, fail-closed, exactly
        as on a fresh run."""
        message = AgentMessage(
            sender=record.get("sender", "?"), task_id=task_id,
            claim=record.get("claim", ""),
            evidence=[dict(e) for e in record.get("evidence", [])],
            cost_nano=0)
        finding: dict = {"subtask": record["subtask"],
                         "status": record["status"], "message": message}
        if record.get("note_id"):
            finding["note_id"] = record["note_id"]
        return finding

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
                system=self.judge_prompt, role="judge",
                model=self.verifier_model)
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

    def check_support(self, task_id: str, claim: str, quotes: list,
                      subtask: str = "?") -> dict:
        """ADR-003 D3: do the quotes support the claim? Two verifier
        calls with different phrasings; the verdict must agree and say
        supported, or the claim fails. No third call. Unlike the
        flag-only relevance judge, this gate fails closed: an
        unparseable reply or a model error marks the claim failed,
        receipted distinctly as unsupported vs error."""
        if not quotes:
            return self._support_receipt(
                task_id, subtask, "unsupported", None, None,
                "no quotes to check")
        try:
            messages = [{"role": "claim", "text": claim},
                        {"role": "quotes",
                         "text": "\n---\n".join(quotes)}]
            first = self._complete(
                task_id, messages, system=SUPPORT_PROMPT,
                role="support", model=self.verifier_model)
            second = self._complete(
                task_id, messages, system=SUPPORT_REASON_PROMPT,
                role="support", model=self.verifier_model)
        except Exception as exc:
            return self._support_receipt(
                task_id, subtask, "error", None, None,
                f"{type(exc).__name__}: {exc}"[:200])
        verdict_a, reason_a = parse_support_verdict(
            first.get("text", ""))
        verdict_b, _ = parse_support_verdict(second.get("text", ""))
        if verdict_a == "supported" and verdict_b == "supported":
            return self._support_receipt(
                task_id, subtask, "supported", verdict_a, verdict_b,
                reason_a)
        return self._support_receipt(
            task_id, subtask, "unsupported", verdict_a, verdict_b,
            reason_a or "verdicts disagreed or unparseable")

    def _support_receipt(self, task_id: str, subtask: str, verdict: str,
                         first, second, reason: str) -> dict:
        self.receipts.append(
            agent_id=self.agent_id, task_id=task_id,
            action="support_check",
            detail={"subtask": subtask, "verdict": verdict,
                    "first": first, "second": second,
                    "reason": (reason or "")[:200]},
            outcome="ok")
        return {"verdict": verdict, "reason": (reason or "")[:200],
                "first": first, "second": second}

    def run_research(self, task_id: str, question: str, *,
                     contract, predictions_path, models: list) -> dict:
        """One research question, one investigator pass: investigate
        with fetch_url, parse the findings envelope (one retry on a
        malformed reply, as in _delegate), critic-check every finding
        (D1/D2), support-check each one that passed (D3), register
        the forecasts (D4). No planner: decomposition for research
        is future work, and one pass is what the B2 demo measures."""
        self.messages_used = 0
        self._run_started = time.perf_counter()
        self.receipts.append(
            agent_id=self.agent_id, task_id=task_id,
            action="task_start",
            detail={"question": question[:500],
                    "contract": contract_hash(contract),
                    **({"routing": self.routing_hash}
                       if self.routing_hash is not None else {})},
            outcome="ok")
        try:
            return self._research_inner(
                task_id, question, contract, predictions_path, models)
        except Exception as exc:  # noqa: BLE001 - every task ends with task_end
            return self._research_end(
                task_id, "escalated", [], 0,
                f"{type(exc).__name__}: {exc}"[:300], cause="error")

    def _research_inner(self, task_id: str, question: str, contract,
                        predictions_path, models: list) -> dict:
        specialist = self.specialists.get(
            "spec", next(iter(self.specialists.values()), None))
        if specialist is None:
            return self._research_end(
                task_id, "escalated", [], 0, "unknown specialist")
        message = self._investigate(task_id, specialist, question)
        if message.escalated:
            return self._research_end(
                task_id, "escalated", [], 0,
                f"investigator escalated: {message.claim}"[:300])
        findings, error = parse_research_reply(message.claim)
        if error is not None:
            retry = self._investigate(
                task_id, specialist,
                f"{question} [previous answer failed: {error}]")
            if retry.escalated:
                return self._research_end(
                    task_id, "escalated", [], 0,
                    f"investigator escalated: {retry.claim}"[:300])
            findings, error = parse_research_reply(retry.claim)
            if error is not None:
                return self._research_end(
                    task_id, "escalated", [], 0,
                    f"findings unparseable: {error}")
            message = retry
        checked = self.critic.check_findings(AgentMessage(
            sender=specialist.agent_id, task_id=task_id,
            claim=message.claim, findings=findings))
        brief = self._verify_and_register(
            task_id, findings, checked, predictions_path, models,
            author=specialist.agent_id)
        registered = len([r for r in self.receipts.read_all()
                          if r["task_id"] == task_id
                          and r["action"] == "forecast_registered"])
        # M3: a verifier outage is an invalid run, not a result. An
        # 'error' verdict must never read as a flag: without this,
        # a downed verifier seat would "catch" every claim.
        errors = [f for f in brief if f["support"] == "error"]
        if errors:
            return self._research_end(
                task_id, "escalated", brief, registered,
                f"verifier outage: {len(errors)} support checks"
                " errored; brief is invalid",
                cause="error")
        standing = [f for f in brief
                    if f["critic"] in ("pass", "judgment")
                    and f["support"] == "supported"]
        if len(standing) == len(brief) and brief:
            status = "complete"
        elif standing:
            status = "partial"
        else:
            status = "escalated"
        return self._research_end(
            task_id, status, brief, registered, "")

    def _verify_and_register(self, task_id: str, findings: list,
                             checked: dict, predictions_path,
                             models: list, author: str) -> list:
        """Support-check every critic-passed finding and register the
        forecasts. Forecasts register once the critic passes them,
        even support-flagged: the register records what was claimed,
        and the brief carries the flag beside it. Shared notes are
        stricter than the register: only critic-passed findings
        whose support also held are filed, so a verified note never
        carries a claim the verifier flagged."""
        brief = []
        for finding, verdict in zip(findings, checked["findings"]):
            # Copy, then annotate: cherry-picking keys silently drops
            # fields a replay needs (measured: probability never made
            # the brief, so every forecast replay failed D2).
            entry = dict(finding) if isinstance(finding, dict) else {}
            entry.setdefault("type", None)
            entry.setdefault("claim", "")
            entry.setdefault("evidence", [])
            entry.update({"critic": verdict["verdict"],
                          "catches": verdict["catches"],
                          "support": None, "support_reason": "",
                          "registered": False})
            if verdict["verdict"] in ("pass", "judgment"):
                quotes = [item.get("quote", "")
                          for item in entry["evidence"]
                          if isinstance(item, dict)
                          and item.get("quote")]
                out = self.check_support(
                    task_id, entry["claim"], quotes,
                    subtask=f"finding-{len(brief) + 1}")
                entry["support"] = out["verdict"]
                entry["support_reason"] = out["reason"]
                if verdict["verdict"] == "pass" \
                        and out["verdict"] == "supported":
                    entry["note_id"] = (
                        self.critic.file_note(
                            task_id, author, entry["claim"],
                            entry["evidence"]) or {}).get("id")
            brief.append(entry)
        registerable = [
            finding for finding, verdict in zip(findings, checked["findings"])
            if isinstance(finding, dict)
            and finding.get("type") == "forecast"
            and verdict["verdict"] == "pass"]
        if registerable:
            register_forecasts(task_id, registerable,
                               predictions_path=predictions_path,
                               receipts=self.receipts, models=models)
            wanted = {id(f) for f in registerable}
            for entry, finding in zip(brief, findings):
                if id(finding) in wanted:
                    entry["registered"] = True
        return brief

    def verify_findings(self, task_id: str, findings: list,
                        *, brief_task_id: str = "") -> dict:
        """Re-run support verification over findings from an earlier
        task (the planted-claim experiment): the D1 gate re-checks
        each finding against the brief's task id, where its reads
        live, and check_support runs on this task's verifier seat.
        Findings that fail D1 are refused, never counted."""
        checked = self.critic.check_findings(AgentMessage(
            sender=self.agent_id, task_id=brief_task_id or task_id,
            claim="(replay)", findings=findings))
        out = []
        for finding, verdict in zip(findings, checked["findings"]):
            claim = finding.get("claim", "") if isinstance(
                finding, dict) else ""
            entry = {"claim": claim, "d1": verdict["verdict"],
                     "catches": verdict["catches"],
                     "support": None, "support_reason": ""}
            if verdict["verdict"] in ("pass", "judgment"):
                quotes = [item.get("quote", "")
                          for item in (finding.get("evidence", [])
                                       if isinstance(finding, dict) else [])
                          if isinstance(item, dict) and item.get("quote")]
                result = self.check_support(
                    task_id, claim, quotes,
                    subtask=f"replay-{len(out) + 1}")
                entry["support"] = result["verdict"]
                entry["support_reason"] = result["reason"]
            out.append(entry)
        return {"task_id": task_id, "findings": out}

    def _research_end(self, task_id: str, status: str, brief: list,
                      registered: int, error: str,
                      cause: str = "model") -> dict:
        try:
            cost_nano = self.ledger.spend(task_id)["settled_nano"]
        except Exception:  # noqa: BLE001 - reporting never breaks the guard
            cost_nano = 0
        # M4: done means accepted, as in _finish. The candidate carries
        # the brief entries, which is what the research checkers read.
        candidate = {"status": status, "text": "", "evidence": [],
                     "findings": brief, "unattempted": []}
        results = {}
        for cid, checker in self.acceptance:
            try:
                results[cid] = bool(checker(candidate))
            except Exception:
                results[cid] = False
        failed = [cid for cid, ok in results.items() if not ok]
        if status == "complete" and failed:
            status = "partial"
        detail = {"status": status, "findings": len(brief),
                  "registered": registered, "cause": cause,
                  "acceptance": results,
                  "duration_ms": self._run_ms()}
        if error:
            detail["error"] = error[:300]
        if failed:
            detail["note"] = ("acceptance failed: " + ",".join(failed))[:300]
        self.receipts.append(
            agent_id=self.agent_id, task_id=task_id, action="task_end",
            detail=detail,
            outcome="ok" if status == "complete" else status)
        return {"task_id": task_id, "status": status, "findings": brief,
                "registered": registered, "cost_nano": cost_nano,
                "error": error, "cause": cause}

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
        self._spend_message()
        return specialist.investigate(task_id, brief)
