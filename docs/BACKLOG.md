# Backlog

Non-blocking findings and known gaps. Nothing here gets its own PR unless a
milestone needs it (VISION.md rule 4).

| Item | Source | Scheduled in |
| --- | --- | --- |
| Receipt store rereads the whole file on append, settle and every critic check (quadratic per run) | EXPERIMENTS F6 | Monitor |
| Approvals for `gated` actions are not plumbed to any operator surface | OPERATIONS section 2 | Monitor |
| Process-level kill switch and dead-man's switch do not exist | OPERATIONS section 4 `[kernel]` | Monitor / M7 |
| Receipt store is writable by the agent process | OPERATIONS section 5 `[kernel]` | M7 |
| Memory module is never written by any agent in the loop | review 2026-11-12 | B3 |
| Planner decomposition off; retest on multi-part tasks only | EXPERIMENTS F2 | B2/B3 |
| Verifier identity should come from the orchestrator, not a string | handoff brief (parked) | B1 |
| Per-call spend reservation sized from actual messages, not the brief | PR #18 note | B1 |
| Model prices unverified against a real invoice | muse.py | B1 |
| Stronger G2: reader shows only files tracked at the frozen commit | PR #20 | when eval trees are reused |
| README still says Milestone 3 | review 2026-11-12 | this PR |
