# Monitor demo: watch a live task, kill it, keep the receipts

Closes the Monitor demo: the operator watches a live task in the
Live view, kills it mid-call from the page, and the kill lands as a
receipt with the in-flight spend refunded.

## Setup

Terminal 1, the Monitor (main at 9c0a184, PRs #70/#71/#74):

```
cd C:\Users\Billy\repos\Mini_AURION
python -m mini_aurion.monitor --root eval --port 8787
```

```
monitor on http://127.0.0.1:8787/ watching eval
```

Terminal 2, a live eval (contract cap $3.00, tree frozen at
c55a621, runner 1623751f):

```
$t = "C:\Users\Billy\repos\eval-tree"
$o = "eval/results-monitor-demo.json"
python eval/grade_code_qa.py --live --tree $t --out $o --runs 5
```

## Transcript

The eval's first runs passed while the Live view showed
`qa-unknown-tool-r5` as `running` (pid 47616, seats
specialist/orchestrator meta/muse-spark-1.1, last receipt advancing).
The operator clicked **kill** on that row, six seconds after the task
started. The eval process died mid-run; the terminal shows the kill,
not a traceback:

```
contract: code-qa.v1 v2
model: muse-spark-1.1 at 1.25/4.25 USD per M tokens (in/out)
runner: 1623751fe8c783f5f6e7efa1d893ddf348c30c16
qa-unknown-tool run 1/5: PASS
qa-unknown-tool run 2/5: PASS
qa-unknown-tool run 3/5: PASS
qa-unknown-tool run 4/5: PASS
PS C:\Users\Billy\repos\Mini_AURION>
```

The Live row for the task changed to `killed` and its kill button
disappeared (the page only offers kill while a task is running). The
kill itself is receipt seq 21 on the task's own chain:

```
action    : kill
agent_id  : monitor
detail    : @{pid=47616; signal=SIGTERM}
outcome   : ok
seq       : 21
task_id   : qa-unknown-tool-r5
ts        : 2026-11-14T15:50:24Z
```

And the final `proc.json` carries the refund of the reservation that
was in flight when the process died:

```json
{
 "task_id": "qa-unknown-tool-r5",
 "pid": 47616,
 "started": "2026-11-14T15:50:18Z",
 "status": "killed",
 "end_status": "killed",
 "killed_at": "2026-11-14T15:50:24Z",
 "refund": {
  "count": 1,
  "released_nano": 17453000
 }
}
```

One reservation, 17,453,000 nanoUSD ($0.0175), released. A second
kill of the same task is refused (`task ... is not running`), so the
refund cannot double-spend; covered by
`test_kill_refuses_a_task_that_already_ended`.

## Cost

The killed run died before its summary line, so its own settled
spend is in the ledger, not on screen. The untouched full eval
directly before it cost $0.726 over 686 s; the kill released $0.0175
back. The demo's own extra spend was the four passing runs of the
killed invocation.

## What this does not show

Caps edits and approval decisions ride the same POST handler and are
covered by tests, not by this transcript (no WRITE or EXTERNAL tool
is registered yet, so a forged approval has nothing to approve;
K1–K3 in `tests/test_review_findings_monitor.py`). The dead-man's
switch is still a hook: the Live view flags tasks with no receipt in
five minutes as STALE.
