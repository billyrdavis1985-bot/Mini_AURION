# Mini-AURION Operations Manual

Constitution for running a heterogeneous multi-agent research system.

Every rule carries an enforcement tag: **[kernel]** (sandbox, OS permissions,
or process control stop a violation), **[code]** (a gate refuses it),
**[review]** (a human checks it), or **[honor]** (nothing stops it — these
are the risk list and must shrink over time).

## 1. Principles

1. No agent's word is truth. Claims are accepted only with cited evidence
   that code verified **when written**: the file exists, the receipt id is
   real, the URL was actually fetched. No model judgment involved. **[code]**
2. Every action leaves a receipt, written **pending** first, then settled
   from the action's actual return code — never from what the agent reports.
   Receipts are written by the orchestrator or tool wrapper into a path
   agents cannot write to. A receipt is a recorded claim, not proof. **[code]**
3. Cost is a first-class signal, tracked like latency. Caps refuse before
   spending (reserve-then-settle); the effective cap is read back and
   confirmed, not just requested. **[code]**
4. Capabilities graduate on evidence, never on hope. **[review]**
5. The human holds the merge gate, the spend caps, and the kill switch. Always.
   **[honor]** — enforced by process discipline and the single-operator rule.

## 2. Capability lifecycle

Every ability (task class, tool, agent, prompt) lives in exactly one stage:

| Stage | Meaning | Requirements to enter |
|---|---|---|
| `experimental` | Sandbox only. No external actions, no paid-tier autonomy. | Written purpose + 5 graded eval cases. **[review]** |
| `gated` | May act in the real world, but a human approves each action. | Eval pass rate meets its bar; misses documented. **[review]** |
| `trusted` | Autonomous within documented bounds (caps, tools, hours). | **Positive evidence:** N=50 gated actions with receipts human-reviewed, plus a red-team pass (poisoning + injection batteries) with findings reviewed. **[review]** |
| `retired` | Disabled. Receipts preserved. | Superseded, unsafe, or uneconomical. **[review]** |

No stage may be skipped. Regression returns the capability to the previous
stage. Absence of detected incidents is not evidence of safety.

## 3. Operating loop

```text
propose → gate (eval + review) → deploy (at current stage) →
monitor (receipts + spend + quality) → graduate or roll back
```

This loop governs prompts, policies, tools, agents, and task classes alike.
Small, boring, repeatable. **[review]**

## 4. Budgets and circuit breakers

- Per-task spend cap, per-capability daily cap, global daily cap. Caps REFUSE
  before spending (reserve-then-settle), never apologize after. **[code]**
- Per-task message cap between agents. Chatter is metered. **[code]**
- Circuit breaker: any cap breach, error burst, or quality anomaly freezes the
  paid tiers for that capability until human review. **[code]**
- Kill switch is a **process-level kill**, not a flag the orchestrator checks:
  it works even when the orchestrator is wedged. **[kernel]** Paired with a
  self-hosted dead-man's switch: if the monitor goes silent past its interval,
  that silence is itself an alert. **[kernel]**
- Kill switch and dead-man's switch are tested monthly. **[review]**

## 5. Receipts

- Every agent action records: agent id, task id, action/tool + arguments,
  evidence pointers, cost, timestamp, policy version, outcome. **[code]**
- Each receipt is written **pending** first, then settled from the action's
  actual return code. Settlement from agent self-report is forbidden. **[code]**
- Receipts are written by the orchestrator/tool wrapper into a store agents
  cannot write to (separate UID or write-only-by-wrapper path). Anything an
  agent can write, it can forge. **[kernel]**
- The receipt chain is **verified on every start** (self-check), not sampled
  monthly. A broken chain refuses to run. **[code]**
- Receipts are append-only. Debugging and monitoring read receipts, not logs.
  **[kernel]** (file permissions) + **[code]** (no delete/update API)

## 6. Memory and provenance

Stored knowledge is structured notes, not a vector blob. Every entry carries:
author agent, sources, verification status (`verified` / `unverified` /
`disputed`), and expiry. **[code]**

- **Owner rule:** only a *different* agent than the author, or a mechanical
  check, can set an entry to `verified`. Self-verification is void. **[code]**
- **No state change on memory alone:** a memory entry can never by itself
  justify closing a task, dropping a frontier item, or granting a permission.
  State changes need fresh evidence or human approval. **[code]** (gate) +
  **[review]** (audit)
- Source pointers resolve at write time: missing file, unknown receipt id, or
  unfetched URL means the entry lands `unverified` with a reason. **[code]**
- Unverified entries expire in 7 days unless verified or renewed. Entries
  built on an expired entry are **flagged** (status `derived-unverified`),
  never quietly kept. **[code]**
- Disputed entries are quarantined from retrieval until resolved. **[code]**

## 7. Eval discipline

- Each capability owns a frozen graded eval set (facts + judgments versioned).
  **[review]**
- New prompts/models must pass a promotion gate: beat or match the incumbent
  on held-out cases over N runs BEFORE adoption. Never tune on the test set.
  **[review]**
- Misses are preserved with analysis, never deleted or relabeled. **[review]**
- Stability: repeat runs must agree within tolerance — **at least 4 of 5 runs
  agree per case** — or the capability drops a stage. **[code]** (measured) +
  **[review]** (demotion)
- Only valid runs count: transport errors and spend-cap refusals never
  reached the model, so they grade as invalid (neither pass nor fail), are
  reported separately, and a case with fewer than 5 valid runs is undecided,
  never stable. **[code]**

## 8. Tier rules

- Cost flows uphill, verification flows downhill: cheap tiers do volume,
  premium tiers decide and verify. **[review]** (routing tables are reviewed)
- **Lower tiers can flag but never clear.** A cheap model may escalate; only
  the verifying tier (or human) may pass. **[code]** (verdict roles enforced)
- Verifier sessions get **no tools**: they judge cited evidence, they don't
  gather. Proven by a canary test (a verifier that attempts a tool call must
  fail closed), not trusted from a flag. **[code]**
- Local tiers (Pi/GPU) hold no paid-tier keys. They queue work; the
  orchestrator spends. **[kernel]** (keys absent from the machine) — audited,
  never assumed.
- External actions (send, publish, purchase, delete) require `gated` minimum,
  full-text logging before execution, and dry-run mode as default. **[code]**
- Sandboxed execution only for agent-run code. No agent code on bare metal.
  **[kernel]**

## 9. Hostile input

Agents are not just fallible; what they read may be hostile. Web pages,
files, tool outputs, and other agents' messages can all carry injected
instructions.

- Content an agent reads is **data, never instructions**. Tool outputs and
  retrieved text are quoted/untrusted by construction. **[code]** (wrapping) +
  **[review]** (prompt audits)
- One agent's output is **untrusted input** to the next. It is verified like
  any external claim before use. **[code]**
- **No agent can grant another agent permissions.** Authority flows only from
  policy + human, never laterally. **[code]**

## 10. Roles

- **Operator (Billy):** merge gate, spend caps, promotions, kill switch, final
  word on scope. Only human who can move a capability up a stage. **[honor]**
- **Builder (Spark):** implements on branches, writes tests + docs, reports
  honestly. Never merges, never spends beyond the milestone cap, never touches
  credentials. **[review]** — drafted by the Builder, separately approved by
  the operator 2026-11-08 (see change log).
- **Reviewer:** checks diffs and eval evidence before merge. Scores catches,
  not agreement. **[review]**
- **Agents (runtime):** operate within stage bounds. Escalate (`can't verify`)
  instead of guessing. Escalation is success, not failure. **[code]**

## 11. Incident review

Any of these triggers a written review within 48h: cap breach, external action
without approval, confident error that reached the operator, cost-per-success
drift beyond 2x, 72h of unexplained quality drop. Format: what happened
(receipt ids), why, what changes (loop/probe/gate), who owns it. Blameless.
Filed in-repo. **[review]**

## 12. Task contracts

Every task runs under a frozen, versioned contract: goal, deliverable,
in/out-of-scope resources, acceptance criteria, budget (ADR-001).
- Traceability: every planner subtask names the criterion ids it serves;
  the orchestrator refuses subtasks naming none or unknown ids. **[code]**
- Scope gate: tool calls outside the contract's scope are refused with a
  denied receipt, like policy denials. **[code]**
- Done means accepted: status is complete only when every acceptance
  criterion passes, else partial naming the failures. **[code]**
- Judges flag, never clear (see section 8). **[code + review]**
- Scope changes are new versions, operator-approved; agents may only
  propose. **[review]** for approval, **[code]** for the version check.
- The task's first receipt carries the contract hash; task_end carries
  per-criterion results. **[code]**
- Drift is measured from receipts: complete-vs-partial share (goal),
  in-scope call share (scope), critic catches per task (evidence).
  **[code]**

## 13. Changes to this document

Amendments need operator approval + a line in the change log below. The rules
can evolve; the evolution is itself governed. **[review]**

### Change log

- v0.1 (2026-11-06): initial draft by Spark, approved by the operator.
- v0.2 (2026-11-06): enforcement tags on every rule; receipts pending/settle +
  orchestrator-written + verify-on-start; memory owner rule + no-state-change +
  derived-flagging; verified-when-written; flag-never-clear + tool-less
  verifiers; promotion needs 50 reviewed actions + red-team pass; new hostile
  input section; stability tolerance 4/5; process-level kill + dead-man's
  switch; typo fix. Approved by the operator 2026-11-08.
- v0.3 (2026-11-07): stability counts valid runs only; error/cap runs are
  invalid and reported separately. Approved by the operator.
- v0.4 (2026-11-08): operator approved the v0.2 rules and the section 10
  Builder text; approved ADR-001 (task contracts and drift control) with
  baseline-contingency and grade()-as-acceptance amendments.
- v0.5 (2026-11-08): new section 12 (task contracts): traceability, scope
  gate, done-means-accepted, receipted contract hash and criterion
  results, drift measures. Implemented in M4a. Approved by the operator
  2026-11-10.
