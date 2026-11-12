# Monitor: local control and observation app (builder task)

**Moves VISION.md row:** "Operator control" from Not started to Built.
Delivers the first `[kernel]` items from OPERATIONS section 4.

**Capability line when done:** the operator can watch a live task, read
its receipts, change caps, approve gated actions, and kill the run, from
one local page.

## Principles

- **Read-only over the state directory** for everything except the three
  operator controls. The app never writes receipts (OPERATIONS section 5:
  anything an agent can write it can forge; the monitor is not an agent,
  but it does not need the write path either).
- **Standard library only**, like the rest of the repo: `http.server` plus
  JSON endpoints and one static page. Binds to loopback only.
- **Separate process** from the agents, so the kill switch works when the
  orchestrator is wedged (section 4 `[kernel]`).

## Views

1. **Live:** tasks in flight; current step; elapsed seconds; spend so far
   against cap; per tier once B1 lands.
2. **Receipts:** per task, the chain in order: model calls, tool calls
   with args, denials with reasons, critic verdicts with the claim text,
   relevance checks, task_end with per-criterion results. Chain
   verification status shown.
3. **Experiments:** batches from BASELINE.md and DRIFT.md with commit,
   cost, result; link to EXPERIMENTS.md entries.

## Controls (the only writes)

- **Kill:** sends SIGTERM to the task's process group; records a
  `killed_by_operator` receipt from the monitor's own identity. Must work
  while the orchestrator is blocked on a model call.
- **Caps:** adjust task/daily/global caps in the ledger; takes effect at
  the next reservation.
- **Approvals:** `NeedsApproval` requests appear as a queue; approving
  re-issues the tool call with `approved=True`. Requires the orchestrator
  to pause on approval instead of escalating (a small change to
  `specialist.py`, named in the PR).

## Prerequisite inside this task

The receipt store rereads its whole file on every append and every read
(EXPERIMENTS F6). A polling monitor makes that worse. Keep the count and
tail hash in the existing anchor file so append is constant-time, and add
a per-task index (SQLite) that the monitor reads. Same chain guarantees;
`verify()` unchanged.

## Demo (closes the milestone)

Transcript under `docs/demos/monitor.md`: start a live task, watch it in
the Live view, kill it from the page mid-call, show the kill receipt and
the ledger refund of the in-flight reservation.

## Non-goals

- No remote access, no auth, no multi-user. Loopback only.
- No charts beyond spend-vs-cap. This is a control surface, not a
  dashboard.
- Dead-man's switch is M7; leave a hook, do not build it here.
