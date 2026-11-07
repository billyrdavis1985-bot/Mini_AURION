# Mini-AURION Operations Manual

Constitution for running a heterogeneous multi-agent research system.
Approved by the operator. Amendments need operator approval (section 11).

## 1. Principles

1. No agent's word is truth. Claims are accepted only with cited, verifiable evidence.
2. Every action leaves a receipt. If it isn't in a receipt, it didn't happen.
3. Cost is a first-class signal, tracked like latency.
4. Capabilities graduate on evidence, never on hope.
5. The human holds the merge gate, the spend caps, and the kill switch. Always.

## 2. Capability lifecycle

Every ability (task class, tool, agent, prompt) lives in exactly one stage:

| Stage | Meaning | Requirements to enter |
|---|---|---|
| `experimental` | Sandbox only. No external actions, no paid-tier autonomy. | Written purpose + 5 graded eval cases. |
| `gated` | May act in the real world, but a human approves each action. | Eval pass rate meets its bar; misses documented. |
| `trusted` | Autonomous within documented bounds (caps, tools, hours). | 30 days in `gated` without incident, or explicit promotion with evidence. |
| `retired` | Disabled. Receipts preserved. | Superseded, unsafe, or uneconomical. |

No stage may be skipped. Regression returns the capability to the previous stage.

## 3. Operating loop

```text
propose → gate (eval + review) → deploy (at current stage) →
monitor (receipts + spend + quality) → graduate or roll back
```

This loop governs prompts, policies, tools, agents, and task classes alike.
Small, boring, repeatable.

## 4. Budgets and circuit breakers

- Per-task spend cap, per-capability daily cap, global daily cap. Caps REFUSE
  before spending (reserve-then-settle), never apologize after.
- Per-task message cap between agents. Chatter is metered.
- Circuit breaker: any cap breach, error burst, or quality anomaly freezes the
  paid tiers for that capability until human review.
- One command freezes ALL paid tiers immediately (kill switch). Tested monthly.

## 5. Receipts

Every agent action records: agent id, task id, action/tool + arguments,
evidence pointers, cost, timestamp, policy version, outcome.
Receipts are append-only. Debugging and monitoring read receipts, not logs.
Monthly: verify receipt chain integrity on a sample.

## 6. Memory and provenance

Stored knowledge is structured notes, not a vector blob. Every entry carries:
author agent, sources, verification status (`verified` / `unverified` /
`disputed`), and expiry. Unverified entries expire in 7 days unless verified
or renewed. Disputed entries are quarantined from retrieval until resolved.

## 7. Eval discipline

- Each capability owns a frozen graded eval set (facts + judgments versioned).
- New prompts/models must pass a promotion gate: beat or match the incumbent
  on held-out cases over N runs BEFORE adoption. Never tune on the test set.
- Misses are preserved with analysis, never deleted or relabeled.
- Stability: repeat runs must agree within the documented tolerance, or the
  capability drops a stage.

## 8. Tier rules

- Cost flows uphill, verification flows downhill: cheap tiers do volume,
  premium tiers decide and verify.
- Local tiers (Pi/GPU) hold no paid-tier keys. They queue work; the
  orchestrator spends.
- External actions (send, publish, purchase, delete) require `gated` minimum,
  full-text logging before execution, and dry-run mode as default.
- Sandboxed execution only for agent-run code. No agent code on bare metal.

## 9. Roles

- **Operator (Billy):** merge gate, spend caps, promotions, kill switch, final
  word on scope. Only human who can move a capability up a stage.
- **Builder (Spark):** implements on branches, writes tests + docs, reports
  honestly. Never merges, never spends beyond the milestone cap, never touches
  credentials.
- **Reviewer:** checks diffs and eval evidence before merge. Scores catches,
  not agreement.
- **Agents (runtime):** operate within stage bounds. Escalate (`can't verify`)
  instead of guessing. Escalation is success, not failure.

## 10. Incident review

Any of these triggers a written review within 48h: cap breach, external action
without approval, confident error that reached the operator, cost-per-success
drift >2x,72h of unexplained quality drop. Format: what happened (receipt ids),
why, what changes (loop/probe/gate), who owns it. Blameless. Filed in-repo.

## 11. Changes to this document

Amendments need operator approval + a line in the change log below. The rules
can evolve; the evolution is itself governed.

### Change log

- v0.1 (2026-11-06): initial draft by Spark, approved by the operator.
