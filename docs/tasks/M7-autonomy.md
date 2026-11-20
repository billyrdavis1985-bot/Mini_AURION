# M7: Autonomy prerequisites (builder task)

**Moves VISION.md row:** none to Built. "Autonomous workflow" stays
Partial; its evidence gains this milestone's transcript. Built needs
the first WRITE capability, which M7 unlocks but does not build.

**Capability line when done:** a killed task resumes to completion
instead of restarting; a silenced monitor pages instead of going
quiet; and no agent tool can write the receipt, ledger, or memory
stores it is judged by.

**Decisions this builds on:** OPERATIONS sections 4 (process-level
kill, dead-man's switch) and 5 (stores agents cannot write). The
monitor already delivers SIGTERM/SIGKILL kill with receipt and
refund (`docs/demos/monitor.md`); M7 hardens and completes it.

## Starting state (honest)

- **Kill** exists and is demoed. Gap: pid reuse — `kill_task`
  signals the pid in `proc.json` without checking it is still the
  same process (backlog row from #72).
- **Dead-man's switch** does not exist. The monitor flags
  receipt-stale tasks STALE; that is the hook, not the switch
  (backlog row, OPERATIONS 4 `[kernel]`).
- **Store confinement** does not exist (backlog row, OPERATIONS 5
  `[kernel]`). Mitigating fact: tools are rooted at the eval tree,
  a separate repo, so no current tool can even address the state
  dir. The gate is forward-defense for the first WRITE tool.
- **Resume** does not exist. Kill ends the chain; rerunning
  re-spends everything.

## Order of PRs (each with its capability line)

1. **This task doc (proposal).** No code; reviewer and operator
   shape the designs below before anything is built.
2. **Store confinement.** Every WRITE-class tool call whose target
   resolves inside the task's store paths (receipts JSONL +
   `.anchor`, ledger and memory sqlite) is denied with a receipt,
   in the same shape as scope-gate denials. The runner registers
   the state dir; the wrapper derives the paths. Tests use a
   fixture WRITE tool (none ships yet — the gate guards a class
   with zero members, deliberately). Documents the remaining
   `[kernel]` limit: on the operator's single-user platform there
   is no second UID, so OS-level separation waits on a broker
   service; until then confinement is `[code]`, tested, not
   honor.
3. **Checkpoints + resume.** After the plan lands and after each
   accepted finding, the orchestrator appends a checkpoint receipt
   (plan, accepted subtask ids + note ids). Runners gain
   `--resume-from <state-dir>`: same task id, same chain, same
   memory and ledger, a `resume` receipt marking the continuation,
   and only the unfinished subtasks re-delegated — the recorded
   plan is reused, not re-planned. Refuses when the contract hash
   differs from the checkpoint's. Spend continues (same ledger);
   message count continues from the checkpoint. Tests on fixtures:
   kill-simulated partial chain resumes to the same brief a clean
   run produces, with no re-spend on completed subtasks.
4. **Dead-man's switch + pid hardening.** The monitor writes a
   heartbeat (timestamp + pid) on every served request; a check
   command exits nonzero when the heartbeat is older than the
   interval or missing — silence is the alert. The operator
   schedules it or runs it before live work; documented limit: a
   dead machine cannot page, full coverage wants the check running
   elsewhere. Pid hardening phase 1: `kill_task` refuses when the
   receipts already show `task_end` (nothing left to kill) —
   stdlib-only, closes the common reuse window; phase 2
   (start-time comparison via platform calls) stays a backlog row
   unless the reviewer wants it now.
5. **Demo + measurement.** Live transcript in `docs/demos/M7.md`:
   start a task, kill it mid-run from the monitor, watch STALE,
   resume it to completion on one chain; silence the monitor and
   show the check paging; show the confinement refusal from the
   fixture transcript (no live WRITE tool exists to abuse it —
   stated, not staged). Then an EXPERIMENTS.md entry.

PRs 2–4 need no live calls ($0, fixtures). PR 5 needs the operator
at the PC with keys; its spend is one killed task plus its resume.

## Demo (closes the milestone)

One chain showing `task_start` → `kill` → `resume` →
`task_end complete`, a STALE flag caught before the resume, a
paged dead-man's check, and a denied store write. Either the resume
reaches the same brief or the transcript says why it could not.

## Open questions (operator + reviewer)

- Resume under the same task id (proposed) or a linked new id?
- Message budget on resume: continued from the checkpoint
  (proposed) or fresh?
- Dead-man's interval default, and where the check runs?
- Pid phase 2 now, or does refuse-when-ended suffice?
- Does the row stay Partial (proposed), or does M7 earn more?

## Constraints

- No new eval sets or gates (rule 3); demo legs only.
- The monitor's served pages and kill behavior stay compatible;
  heartbeat and refusal are additive.
- Hardening budget 1:1 per rule 2; confinement counts as build
  (it is the milestone), not hardening.
