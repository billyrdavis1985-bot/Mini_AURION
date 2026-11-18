"""Critic: tool-less verification of cited evidence.

The critic judges; it never gathers. It has no tools (proven by canary
test, not trusted from a flag) and resolves every citation mechanically
through the memory's write-time checker. A claim without evidence fails.
An escalation is acknowledged, not graded.

Read-to-cite: a file citation counts only if the same task settled an
ok read of that exact path. Existence is not readership. A URL citation
counts only if the same task settled an ok fetch of that URL whose
snapshot hash matches the cited one.
"""
from __future__ import annotations

import posixpath
import threading

from .memory import MemoryError
from .messages import AgentMessage, MessageError
from .research import validate_finding


def _normalize(path: str) -> str:
    """Compare read paths and cites by spelling-normalized form, so
    './note.txt' and 'note.txt' match. Confinement is enforced at the
    tool, not here."""
    return posixpath.normpath(path.replace("\\", "/"))


class Critic:
    def __init__(self, agent_id: str, memory, *, receipts,
                 read_tools: frozenset = frozenset({"read_file"}),
                 fetch_tools: frozenset = frozenset({"fetch_url"})):
        self.agent_id = agent_id
        self.memory = memory
        self.receipts = receipts
        self.read_tools = read_tools
        self.fetch_tools = fetch_tools
        self._mu = threading.Lock()
        self.catches = 0

    def _caught(self, count: int) -> None:
        # Parallel delegates share one critic; the demo transcript
        # reads this counter, so increments stay exact under threads.
        with self._mu:
            self.catches += count

    def check(self, message: AgentMessage) -> dict:
        if message.escalated:
            return {"verdict": "escalated", "catches": [],
                    "by": self.agent_id}
        try:
            message.require_evidence()
        except MessageError as exc:
            self._caught(1)
            return {"verdict": "fail", "catches": [str(exc)],
                    "by": self.agent_id}
        catches = []
        for item in message.evidence:
            catches.extend(self._item_catches(message.task_id, item))
        if catches:
            self._caught(len(catches))
            return {"verdict": "fail", "catches": catches, "by": self.agent_id}
        return {"verdict": "pass", "catches": [], "by": self.agent_id}

    def file_note(self, task_id: str, author: str, text: str,
                  sources: list) -> dict | None:
        """File a critic-passed finding as a shared note, then verify
        it mechanically as this critic. Call only on findings this
        critic passed: read-before-cite was established by the pass,
        and verify() re-resolves existence. Assessment reasoning and
        support-failed claims never reach here (the caller filters);
        a verified note means the sources resolve, nothing more.
        Failures receipt and return None: the finding stands, the
        copy is dropped, and the missing note is visible."""
        try:
            entry = self.memory.write(author, text, sources,
                                      task_id=task_id)
        except MemoryError as exc:
            self.receipts.append(
                agent_id=self.agent_id, task_id=task_id,
                action="memory_write",
                detail={"error": str(exc)[:200]}, outcome="failed")
            return None
        self.receipts.append(
            agent_id=self.agent_id, task_id=task_id,
            action="memory_write",
            detail={"entry_id": entry["id"],
                    "status": entry["status"]}, outcome="ok")
        try:
            verified = self.memory.verify(
                entry["id"], self.agent_id, mechanical=True)
        except MemoryError as exc:
            self.receipts.append(
                agent_id=self.agent_id, task_id=task_id,
                action="memory_verify",
                detail={"entry_id": entry["id"],
                        "error": str(exc)[:200]}, outcome="failed")
            return None
        self.receipts.append(
            agent_id=self.agent_id, task_id=task_id, action="memory_verify",
            detail={"entry_id": entry["id"],
                    "verified_by": self.agent_id}, outcome="ok")
        return verified

    def _item_catches(self, task_id: str, item) -> list[str]:
        """One evidence item, fully checked: resolves, and was read or
        fetched in this task. Shared by check() and check_findings()."""
        if not isinstance(item, dict):
            return ["evidence item is not an object"]
        reason = self.memory.check_source(item, task_id)
        if reason is not None:
            return [f"{item.get('kind')}:{item.get('ref')}: {reason}"]
        catches = []
        if item.get("kind") == "file" and not self._was_read(
                task_id, str(item.get("ref", ""))):
            catches.append(f"file:{item.get('ref')}: cited but never read"
                           " in this task")
        if item.get("kind") == "url" and not self._was_fetched(
                task_id, str(item.get("ref", "")),
                str(item.get("snapshot", ""))):
            catches.append(f"url:{item.get('ref')}: cited but never"
                           " fetched in this task")
        return catches

    def check_findings(self, message: AgentMessage) -> dict:
        """ADR-003 D2: every finding typed, every fact cited. Facts
        pass or fail on their evidence; assessments whose facts hold
        come back flagged as judgment, never verified; forecasts come
        back valid for the caller to register. Pure, like check():
        registration is the caller's write, not the critic's."""
        if message.escalated:
            return {"verdict": "escalated", "catches": [],
                    "findings": [], "by": self.agent_id}
        if not message.findings:
            self._caught(1)
            return {"verdict": "fail",
                    "catches": ["no findings filed"],
                    "findings": [], "by": self.agent_id}
        checked = []
        failed = 0
        for finding in message.findings:
            reasons = validate_finding(finding)
            if not reasons:
                for item in finding.get("evidence", []):
                    reasons.extend(
                        self._item_catches(message.task_id, item))
            ftype = finding.get("type") if isinstance(finding, dict) \
                else None
            if reasons:
                verdict = "fail"
                failed += 1
            elif ftype == "assessment":
                verdict = "judgment"
            else:
                verdict = "pass"
            checked.append({"type": ftype, "verdict": verdict,
                            "catches": reasons})
        self._caught(failed)
        return {"verdict": "fail" if failed else "pass",
                "catches": [c for one in checked for c in one["catches"]],
                "findings": checked, "by": self.agent_id}

    def _was_read(self, task_id: str, ref: str) -> bool:
        path = _normalize(ref.partition(":")[0])
        begun: set[int] = set()
        records = self.receipts.read_all()
        for record in records:
            if record.get("task_id") != task_id:
                continue
            if record.get("action") == "tool_call" \
                    and record.get("tool") in self.read_tools:
                args = (record.get("detail") or {}).get("args", {})
                if _normalize(str(args.get("path", ""))) == path:
                    begun.add(record.get("seq", -1))
        for record in records:
            if record.get("action") == "settle" \
                    and record.get("outcome") == "ok" \
                    and record.get("settles") in begun:
                return True
        return False

    def _was_fetched(self, task_id: str, ref: str, snapshot: str) -> bool:
        # The cited bytes must be bytes this task fetched: the settle
        # receipt of the matching fetch_url call carries the snapshot
        # hash it stored (wired in B2 PR1). The cited ref may be the
        # requested URL or the final URL after redirects: the tool
        # reports final_url, so citing it is correct, not a stretch.
        begun: dict[int, str] = {}
        records = self.receipts.read_all()
        for record in records:
            if record.get("task_id") != task_id:
                continue
            if record.get("action") == "tool_call" \
                    and record.get("tool") in self.fetch_tools:
                args = (record.get("detail") or {}).get("args", {})
                if str(args.get("url", "")) == ref:
                    begun[record.get("seq", -1)] = "requested"
                else:
                    begun.setdefault(record.get("seq", -1), "other")
        for record in records:
            if record.get("action") != "settle" \
                    or record.get("outcome") != "ok" \
                    or record.get("settles") not in begun:
                continue
            detail = record.get("detail") or {}
            if detail.get("snapshot") != snapshot:
                continue
            if begun[record["settles"]] == "requested":
                return True
            if detail.get("final_url") == ref:
                return True
        return False
