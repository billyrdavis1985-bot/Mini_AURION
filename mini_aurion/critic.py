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

from .messages import AgentMessage, MessageError


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
            reason = self.memory.check_source(item, message.task_id)
            if reason is not None:
                catches.append(f"{item.get('kind')}:{item.get('ref')}: {reason}")
                continue
            if item.get("kind") == "file" and not self._was_read(
                    message.task_id, str(item.get("ref", ""))):
                catches.append(f"file:{item.get('ref')}: cited but never read"
                               " in this task")
            if item.get("kind") == "url" and not self._was_fetched(
                    message.task_id, str(item.get("ref", "")),
                    str(item.get("snapshot", ""))):
                catches.append(f"url:{item.get('ref')}: cited but never"
                               " fetched in this task")
        if catches:
            self.catches += len(catches)
            return {"verdict": "fail", "catches": catches, "by": self.agent_id}
        return {"verdict": "pass", "catches": [], "by": self.agent_id}

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
        # hash it stored (wired in B2 PR1).
        begun: set[int] = set()
        records = self.receipts.read_all()
        for record in records:
            if record.get("task_id") != task_id:
                continue
            if record.get("action") == "tool_call" \
                    and record.get("tool") in self.fetch_tools:
                args = (record.get("detail") or {}).get("args", {})
                if str(args.get("url", "")) == ref:
                    begun.add(record.get("seq", -1))
        for record in records:
            if record.get("action") == "settle" \
                    and record.get("outcome") == "ok" \
                    and record.get("settles") in begun \
                    and (record.get("detail") or {}).get("snapshot") \
                    == snapshot:
                return True
        return False
