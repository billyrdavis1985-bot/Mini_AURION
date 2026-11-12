# Mini-AURION

A light orchestrated multi-agent research system: cheap tiers do volume,
premium tiers decide and verify, and every action leaves a receipt.

Part of the AURION line (Agentic Unified Reasoning Intelligence Operation
Network): an orchestrated multi-LLM system for real-world problems. This repo
is the lighter build-and-test version. If it works, it helps build the full
system.

**Status: recalibrated 2026-11-12.** The governance layer (receipts,
ledger, policy, contracts, scope gate) is built and measured. The tiered,
multi-model, research and collaboration commitments are not yet started.
[VISION.md](VISION.md) holds the commitment table and the build order
(tiers first); [EXPERIMENTS.md](EXPERIMENTS.md) holds the findings. The
repo is a testbed for AURION and for PRAXIS.

## Rules

[OPERATIONS.md](OPERATIONS.md) is the constitution. Highlights:

- No agent's word is truth: claims need cited, verifiable evidence.
- Every action leaves an append-only, hash-chained receipt.
- Spend caps refuse *before* spending (reserve-then-settle).
- Capabilities graduate `experimental → gated → trusted` on evidence only.
- The human holds the merge gate, the spend caps, and the kill switch.

## Quick start (standard library only; Python 3.11–3.13)

```bash
python3 -m unittest discover -s tests -q
```

No API key, no network, no spend. All Milestone 0 tests run on fixtures.

## Layout

| Path | Responsibility |
|---|---|
| `mini_aurion/receipts.py` | hash-chained append-only action receipts |
| `mini_aurion/ledger.py` | spend reservations, caps, settlement |
| `mini_aurion/policy.py` | deterministic action gate (reads free, writes gated) |
| `mini_aurion/memory.py` | provenance-tracked notes, write-time evidence checks |
| `mini_aurion/messages.py` | typed inter-agent claims with cited evidence |
| `mini_aurion/tools.py` | trust wrapper: gate + receipts + ledger per call |
| `mini_aurion/specialist.py` | bounded tool-loop investigator, escalates honestly |
| `mini_aurion/critic.py` | tool-less citation verifier, scored on catches |
| `mini_aurion/orchestrator.py` | hub-spoke runner: delegate, verify, retry, synthesize |
| `mini_aurion/muse.py` | live Muse adapter: metered, fail-closed, pinned prompt |
| `mini_aurion/fixture.py` | scripted fake model + tools for $0 tests |
| `eval/` | frozen Code-QA set + script grader (live runs on operator PC) |
| `tests/` | suite over disposable state |
| `OPERATIONS.md` | operating constitution |
| `VISION.md` | commitments, statuses, build order; a gate, not a description |
| `EXPERIMENTS.md` | findings, with cost in dollars and seconds |
| `docs/adr/` | architecture decision records |
| `docs/tasks/` | builder task specs, each with its demo |
| `docs/BACKLOG.md` | non-blocking findings, scheduled by milestone |

## How it is built

Designed by Billy Davis, implemented with AI assistance. All commits, merges,
and final decisions are the human operator's.
