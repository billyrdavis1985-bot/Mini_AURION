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

## F7. The same builder under VISION.md rules

- **Tried:** the F1 builder (Muse Spark) under VISION.md: capability
  line per PR, hardening budget, demo-shaped done, reviewer order.
  Milestone B1, 2026-11-12 to 2026-11-13.
- **Result:** 5 PRs to demo: #50 local-adapter build (pre-rules work,
  later deleted), #52 interface build, #53 smoke build, #54 router
  build, #55 this docs close. Zero hardening or eval PRs inside the
  milestone (budget 0:4). Runtime net +344/-34 lines in `mini_aurion/`
  (the deleted adapter included). Two VISION rows moved
  (cheap tiers, premium tiers). One operator redirect mid-milestone
  ("no calls" -> live smoke first) still applied.
- **Cost:** $0.04 of live smoke spend; about a day of operator and
  builder time.
- **Lesson (agents):** against F1 (47 PRs, 6 days, 0 rows), the
  difference was the per-PR capability line plus a demo as done. The
  builder still over-built once (PR-1 verified with fixtures only);
  the operator's redirect caught it in one round instead of six days.
  Scoreboards steer; this one steered at the right layer.
- **Lesson (pipeline):** deleting the local adapter (-91 lines) beat
  extending it. Configuration, not code, for providers that share a
  wire shape.

## F11. Local seat: connected, quality model-bound

- **Tried:** local seat (llama3.1:8b, Windows Ollama 0.40.10),
  both directions against Meta, twice (smoke_tiers, runner b5ca1af).
- **Result:** run B PASS 2/2 (local judge+synthesis, $0.00 both
  times). Run A 0/2: attempt 1 investigated 19 s, found reserve,
  but wrote a sloppy claim the strict grader failed; attempt 2
  escalated in 2 s. Transport and tools proven $0.00; exactness
  beyond this 8B model. Side finding: gemma3:12b and mistral-nemo
  reject EVERY tools call on both endpoints (six probes) — model
  templates, not payload.
- **Cost:** about $0.024 (the Meta seats); local $0.00.
- **Correction:** #67 (omit tool_choice for local) shipped on
  confounded evidence — gemma rejects everything, so the WITH
  CHOICE failure never isolated the flag. Harmless either way
  (omission equals the default), but unproven as necessary. The
  honest rule stands: one raw tools call against a known-good
  model before payload bisection.
- **Lesson (agents, portable):** for local seats, model capability
  (tools template, exactness) dominates adapter work. Qualify the
  MODEL first: one raw tools call decides seat-vs-model in a minute.
- **Lesson (pipeline):** a failing seat needs a known-good-model
  control before bisection. We spent six probes on broken models.

## F10. OpenAI-compatible is a family resemblance, not a standard

- **Tried:** OpenAI seat verification (gpt-5-mini-2025-08-07), both
  directions against Meta (smoke_tiers, runner 97d721b).
- **Result:** PASS/PASS after three wire fixes: max_completion_tokens
  (#63), omitted temperature (#64), tool_choice gated on tools (#65).
  Cost: A $0.0104 (openai $0.0037, 28 s) / B $0.0130 (openai $0.0021,
  19 s) — the slowest seats yet. Dialects now carried as data:
  meta/deepseek classic; anthropic no temperature; openai renamed
  cap + no temperature + gated tool_choice; google everything.
- **Cost:** about $0.06 live spend including failed rounds.
- **Lesson (agents, portable):** price of admission per provider is
  about three live rounds against its strictest model. The
  per-provider table absorbed all three fixes as rows plus small
  branches — no adapter rewrites. That is the design working.
- **Lesson (pipeline):** two debugging rounds across natives burned
  on stale runners. Check the runner hash before bisecting, always.

## F9. Vendors remove knobs: qualify every provider live

- **Tried:** native Anthropic (#56) and Google (#57) seats, each
  smoke-verified both directions against Meta on one drift case
  (smoke_tiers, runners a60b556).
- **Result:** both PASS/PASS. Cost per run: Anthropic pair A $0.0078
  (haiku $0.0010) / B $0.0121 (haiku $0.0003); Google pair A $0.0114
  (flash $0.0035) / B $0.0134 (flash $0.0006). With the B1 DeepSeek
  pair ($0.0106/$0.0097), the family totals four verified providers
  plus local-configured. Google accepted temperature 0.0; Anthropic
  answered 400 to the field itself (fixed #59, six lines).
- **Cost:** about $0.10 live spend all-in, including the failed
  Anthropic rounds (their Meta seats still settled).
- **Lesson (agents, portable):** a field valid at build time can 400
  at run time. Fake-API tests prove the harness side only; adapter
  qualification must include one live call per provider, on the
  branch, before merge. Merging #56 on fakes alone cost four
  debugging rounds.
- **Lesson (pipeline):** the env-gated request dump (#60) is now
  permanent tooling for provider 400s. Error shorts stay
  identifiers-only per the pinned contract.

## F8. Verification dominates tier cost

- **Tried:** B1 demo run B (cheap DeepSeek investigates, Meta judges
  and synthesizes), one drift case, `docs/demos/B1.md`.
- **Result:** $0.0097 per verified answer: $0.0006 investigation (2
  calls, 2.9 s) + $0.0091 judge and synthesis (2 calls, 5.2 s), 8 s
  wall. Verification cost 15x the investigation it checked. n=1: a
  smoke signal, not a measurement.
- **Lesson (agents, portable):** in a verify-heavy pipeline the
  premium seat is the budget, not the volume seat. AURION's tier
  economics should price the verifier first; cheap investigation is
  nearly free either way.
- **Lesson (pipeline):** per-tier ledger columns plus per-call
  receipts make the split visible instead of arguable. Closes F6's
  timing gap for per-call and per-task seconds.
