# ADR-002: Recalibrate the build around demos, not gates

**Status:** proposed 2026-11-12 by the reviewer, for operator approval.
**Supersedes:** the milestone definitions implied by M0 to M5.
**Related:** ADR-001 (task contracts and drift control, approved
2026-11-08, operations v0.4). VISION.md, EXPERIMENTS.md F1.

## Context

After six days and 47 merged PRs, one of the README's five commitments is
built (receipts). The others (tiered models, research from outside
sources, multi-model verification, agent collaboration) were not started.
The builder was given the vision and freedom to automate; nothing in the
loop checked work against the vision, so the builder optimized for the
only scoreboard it had: green CI and closed review findings. The
reviewer's catch-based contract made that worse.

The operator's goals are confirmed: the repo is a testbed whose product is
findings, for AURION and for PRAXIS (a different architecture). Research
domains are predictive and probabilistic (trade, threat mapping, economic
pressure). Tiers come first.

## Decision

1. **Milestones are defined by demos.** A milestone closes when its demo
   transcript is committed. Measurement is for promotion, after.
2. **VISION.md is a gate, not a description.** It holds the commitment
   table with statuses and the ordered milestones. Only a PR that names
   the row it moves can change a status.
3. **Every PR carries a capability line** ("what can a user do now that
   they could not before"). "Nothing new" marks hardening.
4. **Hardening budget:** at most one hardening or eval PR per build PR
   within a milestone. No new eval sets or gates unless a demo needs one.
5. **Reviewer order:** vision, then blocking (correctness, safety, money),
   then backlog. Non-blocking findings go to `docs/BACKLOG.md`, not to
   fix PRs.
6. **Findings are deliverables.** Each milestone ends with an
   EXPERIMENTS.md entry carrying dollars, seconds and outcome, tagged
   "agents" or "pipeline".
7. **Time is recorded** per task and per model call before B1's demo.

## Consequences

- The builder's tasks are written as features with demos. The next
  comparison (F7) tests whether the same builder performs differently
  under these rules; it is itself a finding.
- The review contract (findings as expected-failure tests) stays, but it
  no longer drives the build order.
- Some hardening already known to be needed (receipt-store scaling,
  approvals plumbing, kill switch) is scheduled inside milestones that
  need it (monitor, M7) instead of as standalone PRs.
- Predictive research needs a verification design the harness does not
  have (provenance now, calibration over time). That is ADR-003, due
  before B2.

## Alternatives considered

- **A more detailed brief.** Rejected: the vision was reconstructible
  from one sentence and confirmed by the operator; detail was not the
  missing input. Order, done-criteria and a per-PR check were.
- **Operator reads every PR.** Rejected as the only control: 47 PRs in
  six days with ninety-second merges shows it does not hold. The
  capability line makes the read ten seconds and keeps it honest.
- **Pause reviews.** Rejected: blocking problems still need catching.
  Re-ordering the review is enough.
