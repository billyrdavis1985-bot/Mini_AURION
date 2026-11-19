# B3: Collaboration (builder task)

**Moves VISION.md rows:** "Multi-agent" from In name only to Built.
"Autonomous workflow" stays Partial until M7.

**Capability line when done:** several specialists work one
question in parallel, file their verified findings as shared notes,
and a synthesis built only from verified notes answers the
question — with wall-clock showing the parallel gain.

**Decisions this builds on:** OPERATIONS sections 6 (verification
needs an owner: only a different agent or a mechanical check
verifies) and 8 (lower tiers flag, never clear). Memory module
exists but no agent writes it in the loop (backlog); the planner
sits at decompose-off (F2). Read both before starting.

## Open decisions (operator + reviewer, before PR 1)

1. Threads or processes for parallel delegation. Threads share the
   receipt store and ledger handles (needs locking: appends and
   SQLite reserves must serialize). Processes need an
   inter-process handoff for findings. Recommendation: threads
   (stdlib `concurrent.futures`), because the monitor, receipts,
   and ledger already assume one box and one process tree.
2. Who verifies a shared note. Section 6 forbids self-verification.
   Options: the critic (mechanical: quote checks it already does)
   or a second specialist on another seat. Recommendation: the
   critic verifies shape and quotes mechanically at write time;
   model-judged support stays flag-only and never gates a write.
3. Demo question. Must be multi-part (it also retests the planner
   per the backlog) and single-envelope per part (F13: one page
   works, two pages refuse). Recommendation: a two-part code
   question over the frozen eval tree (no live web in the demo).

## Order of PRs (each with its capability line)

1. **Verified-note writes.** Critic-passed findings append to
   shared memory via `memory.write` with sources, author, and
   `derived_from`; status starts unverified and only the
   decision-2 owner flips it. Expiry and taint flags ride along
   unchanged. Tests: write path, owner refusal on self-verify,
   unverified-never-recalled-by-default.
2. **Parallel delegation.** The orchestrator fans subtasks across
   specialists concurrently; receipt appends and ledger reserves
   serialize without losing or forking the chain. Deterministic
   merge order for the brief (receipt seq, not finish order).
   Tests: concurrent fixture runs keep one chain, caps hold under
   contention, no interleaved JSONL.
3. **Verified-only synthesis.** The synthesizer recalls verified
   notes only, cites note ids, and refuses when zero verified
   notes exist. Unverified text never enters the synthesis, even
   quoted. Tests mirror the file-quote tests: planted unverified
   note must not appear.
4. **Demo + measurement.** Multi-part question, serial vs parallel
   wall-clock from receipts, transcript in `docs/demos/B3.md`,
   then an EXPERIMENTS.md entry. Frozen question and frozen
   routing, as in B2.

PRs 1–3 do not need the operator's demo question. PR 4 does.

## Frozen demo question (PR4, operator-approved)

Part 1: Which function refuses unregistered tools, and what receipt does the caller write for the refusal? Cite path:line. Part 2: Which function serializes concurrent spend reservations so caps cannot race? Cite path:line.

Routing: `eval/routing-b3.json` (all meta, `same_provider_ok`). Legs:
serial at `max_workers=1`, then parallel at `max_workers=2`, same
question, tree, contract, and caps. Wall-clock comes from receipts.
Runner: `eval/demo_b3.py`; contract `eval/contract-b3-demo-v1.json`.

## Demo (closes the milestone)

- One multi-part question (decision 3). The brief: per-part
  findings with the notes they became, the synthesis with note
  citations, and the serial-vs-parallel wall-clock from receipt
  timestamps.
- One planted unverified note in memory before the run. The
  synthesis must not use it; the transcript shows its absence.
- Seats, dollars and seconds per tier, as in B1/B2.

## Non-goals

- No cross-task memory: entries keep their existing expiry, and no
  pruning or reputation policy (later milestone).
- No multi-page research briefs (F13 envelope stands; B3 does not
  re-argue it).
- No new eval set. The demo is the deliverable.
- No change to the research or code-QA prompts.

Hardening budget inside B3: one hardening PR per build PR.
