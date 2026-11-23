# Synth-JSON: teach the synthesis call the reply shape (builder task)

**Moves VISION.md row:** none to Built. No row changes; the
B3 demo legs stop falling back, which the transcripts show.
A VISION row moves only when a capability is built or
measured, and this is a repair to an existing call.

**Capability line when done:** multi-part briefs synthesize
from verified notes instead of falling back to
note-concatenation — or the transcript says why the gate
refused the fix.

**Decisions this builds on:** backlog row from the B3 demo
(F14): the synthesis call never sees the JSON reply shape,
so live syntheses arrive as prose, fail NO_EVIDENCE, and
fall back. OPERATIONS section 7 (promotion gate for new
prompts) governs adoption. This is the first prompt change
under that gate.

## Starting state (honest)

- Specialists get `SYSTEM_PROMPT`
  (`m0.mini-aurion-investigator.v3`) with the JSON reply
  shape; the adapter parses `{"claim", "evidence",
  "confidence"}` and evidence flows to the critic.
- Synthesis gets only `SYNTH_FORMAT`, a prose
  instruction with no JSON shape
  (`mini_aurion/orchestrator.py`). The adapter finds
  no shape, returns `evidence=[]`, and
  `require_evidence` raises NO_EVIDENCE. The code
  then falls back to verified-notes concatenation.
- Every recorded B3 leg fell back. The fallback is
  honest (verified notes only) but it is not
  synthesis, and every M7 brief inherited it.
- The code gates around synthesis stay whatever the
  prompt says: notes outside the task's verified
  set, and evidence pointers no finding cited, both
  force fallback in code. The prompt change cannot
  widen what synthesis may claim — only whether a
  well-formed claim survives.

## Order of PRs (each with its capability line)

1. **This task doc (proposal).** No code; reviewer
   and operator shape the gate below before
   anything is built.
2. **Synth shape + fixture proofs.** A synth system
   prompt carrying the JSON shape (versioned like
   the investigator's), passed to the synthesis
   `_complete` call. Fixture tests prove: shaped
   synthesis with in-set evidence passes; bad-cite
   and fresh-pointer syntheses still fall back;
   prose synthesis still falls back (the old
   behavior is preserved as the failure mode).
   No live calls ($0).
3. **Section-7 gate + re-baseline.** Live, operator
   at the PC. The gate (proposed, reviewer shapes
   it): the new prompt must beat or match the
   incumbent on the frozen code-QA set over 5
   runs per case (no regression — this is the
   re-baseline the backlog row requires), AND B3
   demo legs must synthesize without NO_EVIDENCE
   fallback in at least 4 of 5 runs each. If
   either fails, no adoption: PR 3 records the
   miss and closes the milestone unadopted.
   Then an EXPERIMENTS.md entry either way.

PR 2 needs no live calls. PR 3's spend is one
code-QA run per prompt version plus five B3 leg
pairs — roughly $2–3 at recent prices; the
operator confirms the cap before the run.

## Demo (closes the milestone)

Adopted: a B3 serial+parallel pair whose briefs
carry synthesized text (not the fallback
marker), with receipts showing critic pass on
the synthesis, plus the re-baselined code-QA
numbers in `eval/BASELINE.md`. Refused: the
gate numbers showing why, preserved with
analysis per section 7.

## Open questions (operator + reviewer)

- Gate shape: code-QA-no-regression plus
  B3-synthesis-4-of-5 (proposed), or a cheaper
  gate (B3 legs only, code-QA re-baselined
  after adoption)?
- B3 gate legs: the frozen two-part demo
  question (proposed — no new eval set, rule 3),
  or a second frozen question?
- SYNTH_FORMAT: extend in place, or a separate
  versioned synth system prompt (proposed —
  matches the investigator/planner pattern)?

## Constraints

- No new eval sets (rule 3); the gate runs on
  frozen code-QA cases and frozen B3 demo legs.
- No tuning against the gate: one prompt
  version enters PR 3. If it fails, a new
  version means a new gate run, not edits
  mid-run (section 7: never tune on the test
  set — the gate legs are the test set once
  named).
- Misses preserved with analysis, never
  relabeled (section 7).
- Hardening budget 1:1 per rule 2; PR 2 counts
  as build (it is the milestone), not hardening.
