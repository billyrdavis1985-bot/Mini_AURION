# Backlog

Non-blocking findings and known gaps. Nothing here gets its own PR unless a
milestone needs it (VISION.md rule 4).

## Open

| Item | Source | Scheduled in |
| --- | --- | --- |
| Dead-man's switch does not exist (Monitor flags receipt-stale tasks as STALE; that is the hook, not the switch) | OPERATIONS section 4 `[kernel]` | M7 |
| Receipt store is writable by the agent process | OPERATIONS section 5 `[kernel]` | M7 |
| Memory module is never written by any agent in the loop | review 2026-11-12 | B3 |
| Planner decomposition off; retest on multi-part tasks only | EXPERIMENTS F2 | B2/B3 |
| Per-call spend reservation sized from the brief, not the actual messages. Matters more since F8: the verifier seat is most of the cost, and B2's fetched pages make contexts long. | PR #18 note; B1 review | B2 |
| A separate `verifier` seat reserves at the orchestrator's estimate; a pricier verifier would be under-reserved. Latent: no table uses a verifier seat yet. | B1 review (#54) | the first table with a verifier seat |
| Model prices unverified against a real invoice, for every paid provider | muse.py; B1 | operator: compare one invoice per provider against the ledger |
| A local tool-capable model strong enough to investigate (llama3.1:8b connects but cannot answer exactly; gemma3:12b and mistral-nemo reject tools) | EXPERIMENTS F11 | operator decision, hardware-dependent |
| Debug request dump (`MINI_AURION_DEBUG_BODY`) writes prompt contents with default file permissions | B1 review (#60) | when next touched |
| EXPERIMENTS.md entries out of order (F7, F11, F10, F9, F8) | B1 review | when next touched |
| Stronger G2: reader shows only files tracked at the frozen commit | PR #20 | when eval trees are reused |
| `kill_task` signals the pid in `proc.json`; a dead pid reused by the OS would hit an unrelated process. Close by recording process start time and checking before signalling. | #72 backlog note | when next touched |
| `fetch_url` has a per-operation socket timeout but no total deadline: a slow-drip server can hold one fetch for minutes within the size cap. Add a wall-clock deadline per fetch. | B2 review (#79) | before B2 runs unattended |
| `fetch_url` caps the **raw** body at 256 KB; github.com alone exceeded it in #79's live check. Most modern pages will. Cap the raw read higher (e.g. 2 MB) and cap the **extracted text** at what the context can carry. | #79 live evidence | before the B2 demo (or the demo measures how many fetches refuse) |
| Brief state dirs (`eval/<brief>/` with snapshots of fetched pages, ledgers, receipts) are not gitignored; `repo_state` ignores untracked files, so a `git add -A` would commit fetched web content. Ignore `eval/brief-*/`, `eval/verify-*`, or move state out of the tree. | B2 review (#84) | before the B2 demo |
| `FetchBackend(allow_private=True)` disables D5 entirely; it exists for loopback tests. The research runner should assert it is off, so no future flag or config path can enable it live. | B2 review (#79) | when next touched |
| D3 as built differs from ADR-003: two verifier calls per finding (two phrasings, both must agree), no OVERREACH verdict, fail-closed on error. Doubles the dominant cost (F8). Record as a decision (amend ADR-003 D3) once the demo shows whether the second call changes verdicts. | B2 review (#82) | B2 demo |
| The support prompt asks whether "even a weak reading" lets the claim be false; this biases toward UNSUPPORTED. The demo must report the false-flag rate on genuine facts next to the planted-claim catch, or the catch is uninterpretable. | B2 review (#82) | B2 demo write-up |

## Closed

| Item | Closed by |
| --- | --- |
| Verifier identity should come from the orchestrator, not a string | #54 (router builds every seat's model) |
| README still says Milestone 3 | #51 |
| Local tier not recorded as verified | #68 (F11) |
| Planner, judge and synthesis calls wrote no `model_call` receipt or timing | #54 |
| Receipt store rereads the whole file on append (quadratic per run) | #70 (anchor + tail fast path, per-task index) |
| Approvals for `gated` actions are not plumbed to any operator surface | #71 (approvals queue + specialist pause) |
| Process-level kill switch does not exist | #71 (SIGTERM/SIGKILL + kill receipt + refund; demo `docs/demos/monitor.md`) |
| Routing hash computed before `"model": ""` resolves | #70 (hash commits the resolved provider/model) |
