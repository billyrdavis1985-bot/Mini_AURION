"""Deterministic action gate. The only code that can permit an action.

Stages (OPERATIONS.md section 2): experimental, gated, trusted.
Action classes: READ (no side effects), WRITE (local state changes),
EXTERNAL (the world beyond this machine: send, publish, delete).

Rules:
- READ is allowed at every stage. Confinement to a sandbox is enforced by
  the tool layer, not here.
- WRITE is denied in experimental, needs human approval in gated, and is
  allowed in trusted.
- EXTERNAL is denied in experimental, needs human approval in gated, and in
  trusted needs approval unless the exact action is allowlisted.

Pure function: no I/O, no model, no time. Same input, same verdict.
"""
from __future__ import annotations

POLICY_VERSION = "m0.policy.v1"

STAGES = ("experimental", "gated", "trusted")
CLASSES = ("READ", "WRITE", "EXTERNAL")


def decide(stage: str, action_class: str, *, approved: bool = False,
           allowlisted: bool = False) -> tuple[str, str]:
    """Return (verdict, reason_code). Verdicts: allow, approve, deny."""
    if stage not in STAGES:
        return ("deny", "UNKNOWN_STAGE")
    if action_class not in CLASSES:
        return ("deny", "UNKNOWN_CLASS")
    if action_class == "READ":
        return ("allow", "READ_IS_SIDE_EFFECT_FREE")
    if stage == "experimental":
        return ("deny", "STAGE_FORBIDS")
    if approved:
        return ("allow", "HUMAN_APPROVED")
    if action_class == "WRITE" and stage == "trusted":
        return ("allow", "TRUSTED_WRITE")
    if action_class == "EXTERNAL" and stage == "trusted" and allowlisted:
        return ("allow", "TRUSTED_ALLOWLISTED")
    return ("approve", "NEEDS_APPROVAL")
