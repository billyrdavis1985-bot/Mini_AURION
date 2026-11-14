# Vision

Mini-AURION is a **testbed**. Its product is findings about how
multi-agent, multi-model systems behave when governed, so that two larger
systems can be built on evidence instead of hope:

- **AURION** (Agentic Unified Reasoning Intelligence Operation Network):
  an orchestrated, tiered, multi-LLM research and automation system.
- **PRAXIS**: a different architecture that will consume the same
  findings. Its shape is not yet documented here; until it is, findings
  are written so that what was learned about *agents and models* is kept
  separate from what was learned about *this hub-and-spoke pipeline*.

The research the full systems will do: marketing and trade predictive
value, probabilistic threat mapping, economic pressure analysis (where the
weak points are now). All of it needs outside sources, time-bound claims,
and probabilities, which is why the later milestones exist.

## The sentence, and what it commits to

> A light orchestrated multi-agent research system: cheap tiers do volume,
> premium tiers decide and verify, and every action leaves a receipt.

| Commitment | Meaning (confirmed by the operator 2026-11-12) | Status | Proven by |
| --- | --- | --- | --- |
| Every action leaves a receipt | Hash-chained receipts, spend ledger, policy gate, task contracts, scope gate | **Built** | `tests/`, drift-batch-2 (gate holds) |
| Cheap tiers do volume | Different models (ideally different providers) in different roles, chosen by a router | **Built** | `docs/demos/B1.md` |
| Premium tiers decide and verify | A verifier on a different model/provider than the investigator; flags, never clears | **Built** | `docs/demos/B1.md` |
| Research system | Open questions answered from real outside sources, not only the repo's own code | Not started | B2 demo |
| Multi-agent | Agents that build on each other's **verified** findings through shared memory, not one model wearing several hats | In name only | B3 demo |
| Autonomous workflow | A task runs several steps without the operator between them, inside stage bounds | Partial (single task, read-only) | B3 demo + M7 |
| Operator control | Kill switch, caps and approvals with a surface the operator can use | Not started | Monitor demo |

A row moves only by a PR that says, in its capability line, which row it
moves. The reviewer checks that line first.

## Build order (operator's decision: tiers first)

Each milestone closes when its **demo transcript** is committed, not when a
measurement passes. Measurements come after, and only to promote a
capability (OPERATIONS section 2).

| Milestone | Builds | Demo (done means) |
| --- | --- | --- |
| **B1 Tiers** | Provider interface, router (role to provider+model), per-provider pricing in the ledger. OpenAI-compatible family first (OpenAI, DeepSeek, Meta, local), then native adapters (Anthropic, Google) — all verified live (F9-F11; local connected, answer quality model-bound). | One task where a cheap model investigates and a model from a **different provider** verifies; the ledger shows each tier's cost and the receipts show which model did what. Spec: `docs/tasks/B1-tiers.md`. |
| **Monitor** | Local, read-only web app over the state directory; kill switch, caps, approvals as the only writes. | Operator watches a live task, reads its receipts, and kills it from the app. Spec: `docs/tasks/monitor.md`. |
| **B2 Research** | `fetch_url` read tool (memory already tracks fetched URLs), a research task class, ADR-003 on verifying predictive claims (provenance now, calibration over time). | A cited brief on one real question, built from pages actually fetched, with every claim's provenance receipted. |
| **B3 Collaboration** | Specialists in parallel; verified notes written to shared memory; a synthesizer that builds only on verified notes. | Several agents on one question, visibly building on each other's verified findings; wall-clock shows the parallel gain. |
| **M7 Autonomy prerequisites** | Saved state and resume, process-level kill, dead-man's switch, receipt store agents cannot write. | Required before any WRITE or EXTERNAL capability. Partly delivered by the monitor. |

## Rules that keep the build on the vision

1. **Capability line.** Every PR states what a user can do now that they
   could not before. "Nothing new" is allowed; it marks the PR as
   hardening. (`.github/pull_request_template.md`)
2. **Hardening budget.** At most one hardening or eval PR per build PR
   inside a milestone. The reviewer computes the ratio from git at each
   review.
3. **No new eval sets or gates** unless a milestone's demo needs one.
4. **Reviewer order.** Vision first (does the PR move a row), blocking
   problems second (correctness, safety, money), everything else to
   `docs/BACKLOG.md`.
5. **Findings are deliverables.** Every milestone ends with an entry in
   `EXPERIMENTS.md`: what was tried, what it cost in money and time, what
   it showed, and whether the lesson is about agents or about this
   pipeline.

## What the testbed must measure

Recorded since B1 (PR #52): wall-clock per task (`task_end`) and
per model call (`model_call`), runners print seconds next to dollars.
Every finding from then on carries three numbers: dollars, seconds,
and the outcome.

## Open questions for the operator

- PRAXIS: a one-paragraph description of its architecture, so findings
  can be tagged for it.
- B2's first real question, in the operator's own domain.
- Which local models (and serving stack) count as the "cheap tier" for B1.
