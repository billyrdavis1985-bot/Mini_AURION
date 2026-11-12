# Experiments log

The testbed's product. One entry per finding. Each entry says what was
tried, what it cost (dollars and, once recorded, seconds), what it showed,
and whether the lesson is about **agents and models** (portable to AURION
and PRAXIS) or about **this pipeline** (hub-and-spoke, one model, serial).

Numbers come from `eval/BASELINE.md`, `eval/DRIFT.md`, and git. Misses are
kept (OPERATIONS section 7).

## F1. An open brief plus a green-CI scoreboard produces hardening, not product

- **Tried:** the builder (Muse Spark) was given the README's vision and
  freedom to automate, 2026-11-06 to 2026-11-11.
- **Result:** 47 merged PRs, 63 non-merge commits. 30 touched only tests,
  evals or docs. Runtime code (2,429 lines) was nearly all written in the
  first two days (M0 to M3); later runtime changes were gates and fixes.
  Four eval sets and three pre-registered measurement plans exist. Of the
  README's five commitments, one (receipts) is built; tiers, research,
  multi-model and collaboration were not started.
- **Cost:** about $14 of recorded eval spend; six days of operator and
  builder time, which was the real cost.
- **Lesson (agents):** an autonomous builder optimizes what its scoreboard
  rewards. With no per-PR check against the goal, "green CI and closed
  findings" became the goal. Detail in the brief would not have helped;
  the missing pieces were order, a demo-shaped definition of done, and a
  check that runs on every PR. The reviewer's catch-based contract
  amplified the drift.
- **Follow-up:** VISION.md, the PR capability line, the hardening budget.
  The same builder under those rules is the control experiment; record
  the comparison as F7 when B1 lands.

## F2. Decomposition on single-step questions hurts

- **Tried:** M4a planner (one subtask per acceptance criterion) on
  code-qa.v1, candidate 8bfdca5.
- **Result:** qa-unknown-tool fell to 2/10 (4 wrong-evidence answers, 4
  declines); receipts show planner fragmentation. Reverted in #35;
  `decompose` is now opt-in and off.
- **Cost:** $0.81 for 20 runs.
- **Lesson (pipeline, provisionally):** the questions had nothing to
  decompose; splitting them produced subtasks without enough context to
  answer. Not evidence against decomposition in general. Retest only on
  genuinely multi-part research tasks (B2/B3).

## F3. The scope gate holds once the agent is told it may decline

- **Tried:** drift set code-qa-drift.v1, 30 runs per batch, per-case
  contracts with real in/out-of-scope paths.
- **Result:** batch 1 (fc428d1): adherence 73%, correctness 47%; all 11
  probes on one case, all denied, zero out-of-scope reads settled. Batch 2
  (11b378b, brief gains a uniform "you may decline" license): adherence
  100%, correctness 60%; probes 11 to 0.
- **Cost:** $0.57 and $0.43.
- **Lesson (agents):** denial alone does not stop probing; telling the
  model that declining is a valid outcome does. The gate itself never
  leaked (no out-of-scope read ever settled), so the mechanism is sound
  and the behaviour is prompt-sensitive.

## F4. Prompt format changes are real; most 5-run swings are not

- **Tried:** prompt v2 (claim follows the brief's format) on
  qa-tier-summary; three 5-run batches across cases.
- **Result:** tier-summary went from about 30% to 5/5 (0.24% chance by
  luck). Meanwhile qa-race-lock moved 4/5 to 2/5 with no relevant change
  (21% chance by luck).
- **Lesson (method):** 5 runs cannot separate a 67% case from an 85%
  one. Pooled, pre-registered rules with known error rates replaced the
  per-case rule (BASELINE.md, M4 plan). Portable to any eval of any
  architecture.

## F5. Grader strictness masquerades as model failure

- **Tried:** qa-settle-source scored 0/5 at de1005e.
- **Result:** 4 of 5 answers were correct; the grader rejected the
  spelling "return code" against `return_code`. Fixed by the matcher
  change (#27), frozen set untouched.
- **Lesson (method):** read the misses before believing a score. A
  mechanical grader is a hypothesis about answer form, and it can be
  wrong in ways that look exactly like a weak model.

## F6. Cost per task is small; time is unmeasured

- **Result:** code-QA $0.034 to $0.041 per task; drift $0.014 to $0.019.
  A task is up to 9 serial model calls (6 specialist, planner, synthesis,
  judge) at a 60 s timeout each. Receipts carry timestamps; nothing
  aggregates them, so wall-clock per task is unknown.
- **Lesson (pipeline):** money will not be the constraint at this scale;
  latency and operator attention will. Record seconds per task and per
  call before B1's demo, or B1's routing decisions will be made blind.
- **Also noted:** the receipt store rereads its whole file on every
  append, settle and critic check (quadratic per run). Fine at about 50
  receipts per task; not fine for long runs or a polling monitor. Fix
  planned with the monitor (count and tail in the anchor; SQLite index
  by task).

## F7. (reserved) The same builder under VISION.md rules

- To be recorded when B1 closes: PRs by type, runtime lines, rows moved,
  days to demo, compared with F1.
