# Baseline: code-qa.v1 (M3 exit)

Set `code-qa.v1` frozen at `c55a621`. Five runs per case, 4-of-5 stability
over valid runs only (OPERATIONS v0.3). M4 must not drop any case below
its baseline passes, and must hold scope adherence ≥90%.

> **Superseded** by the [M4 measurement plan](#m4-measurement-plan-pre-registered)
> below once the operator merges it. Kept here as the rule originally
> approved (ADR-001 baseline-contingency amendment).

| case | de1005e | 950f8ea | bc9be76 | verdict |
| --- | --- | --- | --- | --- |
| qa-unknown-tool | 5/5 | 4/5 | 5/5 | solid |
| qa-race-lock | 4/5 | 4/5 | 2/5 | wobble |
| qa-settle-source | 0/5* | 3/5 | 3/5 | wobble |
| qa-tier-summary | 2/5 | 1/5 | 5/5 | solid (prompt v2) |
| qa-newsletter | 5/5 | 5/5 | 5/5 | solid |
| stable | 3/5 | 3/5 | 3/5 | |

\* 4 of 5 answered correctly; the grader rejected the "return code"
spelling (fixed by the matcher change, frozen set untouched).

Failure analysis: the wobblers need multi-element answers (function +
mechanism, report-vs-code judgment) under single-shot prompting. Live
traces show the model reading the answer file first, then more files,
then declining — a composition ceiling, not a discovery or critic
failure. M4's decomposition (one subtask per acceptance criterion) is
the prescribed fix. Batches cost ~$0.65–1.00 at the $3.00 cap.

## M4 measurement plan (pre-registered)

**Status:** proposed by the reviewer on 2026-11-09; binding once the
operator merges it. Written before any counted M4 batch. M4a runs made
before this merge are exploratory and do not count.

**Why it replaces the rule above.** That rule compares each case with a
single 5-run sample. If M4 changed nothing at all, some case would still
fall below its baseline about **70%** of the time. And 5 runs cannot tell
a 67% case from an 85% one, which is the lift M4 is betting on.

### 1. Freeze

- One **candidate commit** per measurement, recorded in the results table.
  No change to prompts, planner, judge, grader, contract or runner while
  it is measured. Any change makes a new candidate and the counts restart.
- Grader: the post-#27 matcher (case- and underscore-insensitive
  `must_mention`), the same for every batch.
- Tree: a fresh worktree of `c55a621` per invocation, `--out` outside it.

### 2. Questions and decision rules (fixed in advance)

| Question | Cases | Valid runs | Rule | Error rates |
| --- | --- | --- | --- | --- |
| **Q1.** Does M4 lift the wobblers? | `qa-race-lock` + `qa-settle-source`, pooled | 20 each, 40 total | **≥32/40:** lift shown; adopt M4 for code-QA. **23–31/40:** no detectable lift; claim nothing, read the traces. **≤22/40:** M4 hurts; stop and revert the planner path for code-QA. | Baseline 20/30 (67%), pooled across the three M3 batches. If M4 is truly no better, false "lift": 4.9%. If M4 truly reaches 85%, detected 86% of the time. False "hurts": 8.2%. |
| **Q2.** Do the solid cases hold? | `qa-unknown-tool`, `qa-tier-summary`, `qa-newsletter` | 10 each | A case **regresses at ≤7/10**. Any regression blocks adoption until explained from the receipts. | At a true 93%, a false alarm on any of the three: ~8%. A real drop to 60% is caught 83% of the time per case. |

M4 is adopted for code-QA only if Q1 shows a lift **and** Q2 shows no
regression. Section 7's 4-of-5 stability is still reported per batch, but
it is not used to compare versions.

### 3. Run rules

- Invalid runs (cause `error` or `cap`) do not count; re-run until each
  case has its planned number of valid runs. If more than 20% of a batch
  is invalid, stop and fix the infrastructure; do not analyze it.
- Q1 first, then Q2, each as its own invocation under the $3.00 cap. At M3
  rates (~$0.03–0.04 per run) that is about $1.05–1.60 for Q1 and
  $0.80–1.20 for Q2. M4 adds planner and judge calls per run, so the Q1
  batch sets the real rate; if it would exceed the cap, split it.
- Every batch goes in the results table below, failed or not, with its
  commit, cost and invalid count. Misses are preserved (section 7).

### 4. What this plan does not decide

- **Scope adherence (the "≥90%" above).** The `code-qa.v1` contract has
  empty in- and out-of-scope lists, so the scope gate can only refuse
  tree escapes and criterion A2 always passes. The measure moves to drift
  cases with real scope (`code-qa-drift.v1`, 2–3 cases, not yet written).
- **Promotion to `gated`.** Prompt v2, the #27 matcher and planner v2 were
  each tuned on these five cases, so `code-qa.v1` is now a development
  set. The `gated` decision needs a held-out set (`code-qa.v2`, about 10
  cases) frozen before any further tuning (section 7: never tune on the
  test set).

### 5. Before the first counted batch

- [ ] Spark: `--cases <id,id>` filter on `eval/grade_code_qa.py` (runs only
  the named cases; the report lists which ran).
- [ ] Operator: approve OPERATIONS v0.5 (section 12 is still marked
  pending, while M4a is already merged).
- [ ] Operator: merge this plan, which makes it binding.

### Results

| batch | candidate commit | question | cases | valid runs | passes | invalid | cost | notes |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
