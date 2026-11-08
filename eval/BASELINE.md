# Baseline: code-qa.v1 (M3 exit)

Set `code-qa.v1` frozen at `c55a621`. Five runs per case, 4-of-5 stability
over valid runs only (OPERATIONS v0.3). M4 must not drop any case below
its baseline passes, and must hold scope adherence ≥90%.

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
