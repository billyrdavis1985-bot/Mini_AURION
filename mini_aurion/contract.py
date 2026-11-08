"""Task contracts: the frozen definition of done (ADR-001).

A contract names the goal, the deliverable, what is in and out of scope,
the acceptance criteria, and the budget. It is frozen per task class
beside its eval set, hashed into the task's first receipt, and enforced
in code: untagged subtasks are refused, out-of-scope calls are denied,
and status is complete only when every criterion passes.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Criterion:
    id: str
    kind: str
    description: str


@dataclass(frozen=True)
class TaskContract:
    contract_id: str
    version: int
    goal: str
    deliverable: str
    in_scope_paths: tuple = ()
    out_of_scope_paths: tuple = ()
    acceptance: tuple = ()
    spend_usd: str = "3.00"
    messages: int = 10

    def criterion_ids(self) -> frozenset:
        return frozenset(c.id for c in self.acceptance)


def load_contract(path: str | Path, goal: str) -> TaskContract:
    """Load a frozen contract template, instantiated with this task's goal."""
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    return TaskContract(
        contract_id=str(raw["contract_id"]),
        version=int(raw["version"]),
        goal=goal,
        deliverable=str(raw.get("deliverable", "")),
        in_scope_paths=tuple(raw.get("in_scope_paths", [])),
        out_of_scope_paths=tuple(raw.get("out_of_scope_paths", [])),
        acceptance=tuple(Criterion(str(c["id"]), str(c["kind"]),
                                   str(c.get("description", "")))
                         for c in raw.get("acceptance", [])),
        spend_usd=str(raw.get("budget", {}).get("spend_usd", "3.00")),
        messages=int(raw.get("budget", {}).get("messages", 10)))


def contract_hash(contract: TaskContract) -> str:
    """Stable hash of the contract instance, receipted at task start."""
    body = json.dumps({
        "contract_id": contract.contract_id, "version": contract.version,
        "goal": contract.goal, "deliverable": contract.deliverable,
        "in_scope_paths": list(contract.in_scope_paths),
        "out_of_scope_paths": list(contract.out_of_scope_paths),
        "acceptance": [{"id": c.id, "kind": c.kind,
                        "description": c.description}
                       for c in contract.acceptance],
        "spend_usd": contract.spend_usd, "messages": contract.messages,
    }, sort_keys=True)
    return "sha256:" + hashlib.sha256(body.encode("utf-8")).hexdigest()


def _norm(path: str) -> str | None:
    """Normalize a resource path. None means it escapes the root."""
    parts = []
    for part in path.replace("\\", "/").split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            if not parts:
                return None
            parts.pop()
        else:
            parts.append(part)
    return "/".join(parts)


def _matches(resource: str, entry: str) -> bool:
    entry = _norm(entry)
    if entry is None:
        return False
    if entry == "":
        return True
    return resource == entry or resource.startswith(entry + "/")


def resource_in_scope(contract: TaskContract, resource: str) -> bool:
    """Whether a tool-call resource is inside the task's scope. Empty
    in-scope means the whole tree; out-of-scope always wins; escapes
    are out."""
    normed = _norm(resource)
    if normed is None:
        return False
    for entry in contract.out_of_scope_paths:
        if _matches(normed, str(entry)):
            return False
    if not contract.in_scope_paths:
        return True
    return any(_matches(normed, str(e)) for e in contract.in_scope_paths)


def render_for_planner(contract: TaskContract, specialists: list) -> str:
    """Contract context for the planner prompt: ids, scope, budget."""
    lines = [f"Goal: {contract.goal}",
             f"Deliverable: {contract.deliverable}",
             "Acceptance criteria:"]
    for c in contract.acceptance:
        lines.append(f"- {c.id} ({c.kind}): {c.description}")
    scope = ", ".join(contract.in_scope_paths) or "the whole tree"
    lines.append(f"In scope: {scope}")
    if contract.out_of_scope_paths:
        lines.append("Out of scope: "
                     + ", ".join(contract.out_of_scope_paths))
    lines.append(f"Budget: ${contract.spend_usd}, "
                 f"{contract.messages} messages")
    lines.append("Available specialists: " + ", ".join(specialists))
    return "\n".join(lines)


def scope_denials(receipts) -> int:
    """Count scope-gate denials in a receipt store's full history."""
    return sum(1 for r in receipts.read_all()
               if r.get("action") == "denied"
               and "SCOPE_EXCLUDED" in str(r.get("detail", {}).get(
                   "reason", "")))
