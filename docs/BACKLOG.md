# Backlog

Non-blocking findings and known gaps. Nothing here gets its own PR unless a
milestone needs it (VISION.md rule 4).

## Open

| Item | Source | Scheduled in |
| --- | --- | --- |
| Receipt store rereads the whole file on append, settle and every critic check (quadratic per run) | EXPERIMENTS F6 | Monitor |
| Approvals for `gated` actions are not plumbed to any operator surface | OPERATIONS section 2 | Monitor |
| Process-level kill switch and dead-man's switch do not exist | OPERATIONS section 4 `[kernel]` | Monitor / M7 |
| Receipt store is writable by the agent process | OPERATIONS section 5 `[kernel]` | M7 |
| Memory module is never written by any agent in the loop | review 2026-11-12 | B3 |
| Planner decomposition off; retest on multi-part tasks only | EXPERIMENTS F2 | B2/B3 |
| Per-call spend reservation sized from the brief, not the actual messages. Matters more since F8: the verifier seat is most of the cost, and B2's fetched pages make contexts long. | PR #18 note; B1 review | B2 |
| A separate `verifier` seat reserves at the orchestrator's estimate; a pricier verifier would be under-reserved. Latent: no table uses a verifier seat yet. | B1 review (#54) | the first table with a verifier seat |
| Routing hash is computed before `"model": ""` resolves, so changing a model via env leaves the hash unchanged. Receipts still record the real model per call. Hash the resolved names. | B1 review (#54) | Monitor (it displays the hash) |
| Model prices unverified against a real invoice, for every paid provider | muse.py; B1 | operator: compare one invoice per provider against the ledger |
| A local tool-capable model strong enough to investigate (llama3.1:8b connects but cannot answer exactly; gemma3:12b and mistral-nemo reject tools) | EXPERIMENTS F11 | operator decision, hardware-dependent |
| Debug request dump (`MINI_AURION_DEBUG_BODY`) writes prompt contents with default file permissions | B1 review (#60) | when next touched |
| EXPERIMENTS.md entries out of order (F7, F11, F10, F9, F8) | B1 review | when next touched |
| Stronger G2: reader shows only files tracked at the frozen commit | PR #20 | when eval trees are reused |

## Closed

| Item | Closed by |
| --- | --- |
| Verifier identity should come from the orchestrator, not a string | #54 (router builds every seat's model) |
| README still says Milestone 3 | #51 |
| Local tier not recorded as verified | #68 (F11) |
| Planner, judge and synthesis calls wrote no `model_call` receipt or timing | #54 |
