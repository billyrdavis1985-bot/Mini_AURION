# Mini-AURION

[![qualification](https://github.com/billyrdavis1985-bot/Mini_AURION/actions/workflows/qual.yml/badge.svg?branch=main)](https://github.com/billyrdavis1985-bot/Mini_AURION/actions/workflows/qual.yml)
![python](https://img.shields.io/badge/python-3.11%20%7C%203.13-blue)
![platforms](https://img.shields.io/badge/CI-Linux%20%7C%20Windows-informational)
![dependencies](https://img.shields.io/badge/dependencies-stdlib%20only-brightgreen)
![providers](https://img.shields.io/badge/providers-Meta%20%C2%B7%20DeepSeek%20%C2%B7%20OpenAI%20%C2%B7%20Anthropic%20%C2%B7%20Google%20%C2%B7%20local-purple)
[![license](https://img.shields.io/badge/license-MIT-lightgrey)](LICENSE)

A light orchestrated multi-agent research system: cheap tiers do volume,
premium tiers decide and verify, and every action leaves a receipt.

Mini-AURION is a **testbed**. Its product is findings about how governed,
multi-model agent systems behave, recorded in
[EXPERIMENTS.md](EXPERIMENTS.md) with their cost in dollars and seconds.
Those findings feed two larger systems: **AURION** (an orchestrated,
tiered, multi-LLM research and automation system) and **PRAXIS** (a
different architecture).

## Where it stands

[VISION.md](VISION.md) is the source of truth: each commitment, its
status, and the demo that proves it. In short:

| Commitment | Status |
| --- | --- |
| Every action leaves a receipt | Built |
| Cheap tiers do volume | Built ([B1 demo](docs/demos/B1.md)) |
| Premium tiers decide and verify | Built ([B1 demo](docs/demos/B1.md)) |
| Operator control (local Monitor) | In progress: app merged, demo pending |
| Research from outside sources | Next (B2) |
| Multi-agent collaboration | Later (B3) |
| Autonomous workflow | Partial |

## Rules

[OPERATIONS.md](OPERATIONS.md) is the constitution. Highlights:

- No agent's word is truth: claims need cited, verifiable evidence.
- Every action leaves an append-only, hash-chained receipt.
- Spend caps refuse *before* spending (reserve-then-settle).
- Capabilities graduate `experimental → gated → trusted` on evidence only.
- The human holds the merge gate, the spend caps, and the kill switch.

Every PR states what a user can do now that they could not before
(the [PR template](.github/pull_request_template.md)); milestones close
on a demo, not a gate ([ADR-002](docs/adr/002-recalibration.md)).

## Quick start

Standard library only. Tested on Python 3.11 and 3.13, Linux and Windows.

```bash
python3 -m unittest discover -s tests -q
```

No API key, no network, no spend: every test runs on fixtures and fake
API servers.

## Live use (operator machine)

Set a key and prices for each provider a routing table names. Prices are
USD per million tokens; unknown prices refuse to run, because spend
cannot be reserved without them.

| Provider | Key | Model | Prices |
| --- | --- | --- | --- |
| Meta | `MODEL_API_KEY` | `MINI_AURION_MUSE_MODEL` | `MINI_AURION_PRICE_INPUT_USD_PER_M`, `MINI_AURION_PRICE_OUTPUT_USD_PER_M` |
| DeepSeek | `DEEPSEEK_API_KEY` | `MINI_AURION_DEEPSEEK_MODEL` | `MINI_AURION_DEEPSEEK_PRICE_IN`, `_PRICE_OUT` |
| OpenAI | `OPENAI_API_KEY` | `MINI_AURION_OPENAI_MODEL` | `MINI_AURION_OPENAI_PRICE_IN`, `_PRICE_OUT` |
| Anthropic | `ANTHROPIC_API_KEY` | `MINI_AURION_ANTHROPIC_MODEL` | `MINI_AURION_ANTHROPIC_PRICE_IN`, `_PRICE_OUT` |
| Google | `GOOGLE_API_KEY` | `MINI_AURION_GOOGLE_MODEL` | `MINI_AURION_GOOGLE_PRICE_IN`, `_PRICE_OUT` |
| Local (Ollama/vLLM) | none | `MINI_AURION_LOCAL_MODEL` | free; base URL `MINI_AURION_LOCAL_BASE_URL` |

```bash
# Two providers, both seats, one drift case (smoke, not an eval)
python eval/smoke_tiers.py --live --tree <frozen-tree> --out <outside-tree>/tiers.json \
    --routing eval/routing-b1.json

# Watch runs, kill one, edit caps, answer approvals (loopback only)
python -m mini_aurion.monitor --root <state-dir> --port 8787
```

Routing tables (`eval/routing-*.json`) assign a provider and model to each
seat. The router refuses a table whose verifier shares the specialist's
provider unless the table says `same_provider_ok`, and the table's hash is
receipted at task start.

## Layout

| Path | Responsibility |
|---|---|
| `mini_aurion/receipts.py` | hash-chained append-only action receipts, per-task index |
| `mini_aurion/ledger.py` | spend reservations, persisted caps, per-provider settlement |
| `mini_aurion/policy.py` | deterministic action gate (reads free, writes gated) |
| `mini_aurion/contract.py` | frozen task contracts and the scope gate (ADR-001) |
| `mini_aurion/tools.py` | trust wrapper: gate + receipts + ledger per call |
| `mini_aurion/specialist.py` | bounded tool-loop investigator, escalates honestly |
| `mini_aurion/critic.py` | tool-less citation verifier, quote-anchored |
| `mini_aurion/orchestrator.py` | hub-spoke runner: plan, delegate, verify, synthesize |
| `mini_aurion/router.py` | routing table to seats; cross-provider verifier rule |
| `mini_aurion/muse.py` | OpenAI-compatible family (Meta, DeepSeek, OpenAI, local) |
| `mini_aurion/anthropic.py`, `google.py` | native adapters |
| `mini_aurion/provider_http.py` | shared fail-closed transport (https, no redirects) |
| `mini_aurion/monitor.py` | local operator app: live view, receipts, kill, caps, approvals |
| `mini_aurion/memory.py` | provenance-tracked notes (collaboration substrate for B3) |
| `mini_aurion/messages.py` | typed inter-agent claims with cited evidence |
| `mini_aurion/fixture.py` | scripted fake model + tools for $0 tests |
| `eval/` | frozen eval sets, graders, routing tables, smoke runner |
| `tests/` | suite over disposable state; `test_review_findings*` are regression guards |
| `OPERATIONS.md` | operating constitution |
| `VISION.md` | commitments, statuses, build order; a gate, not a description |
| `EXPERIMENTS.md` | findings, with cost in dollars and seconds |
| `docs/adr/` | architecture decision records |
| `docs/tasks/` | builder task specs, each with its demo |
| `docs/demos/` | demo transcripts that close milestones |
| `docs/BACKLOG.md` | non-blocking findings, scheduled by milestone |

## How it is built

Designed by Billy Davis (operator: merge gate, spend caps, promotions).
Implemented with AI assistance: a builder agent writes the code on
branches, a reviewer agent checks every PR against VISION.md and
OPERATIONS.md, and review findings land as tests that fail until fixed.
All merges and final decisions are the human operator's.

Released under the [MIT License](LICENSE).
