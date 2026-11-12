# B1: Tiers (builder task)

**Moves VISION.md rows:** "Cheap tiers do volume" and "Premium tiers decide
and verify" from Not started to Built.

**Capability line when done:** a task runs with a cheap model
investigating and a model from a different provider verifying, and the
operator can see what each tier cost and did.

## Deliverables

1. **Provider interface.** One protocol every adapter implements:
   `complete(messages, tools, system_prompt) -> reply`, `last_cost_nano`,
   `worst_case_nano(messages)`, plus `provider` and `model` identifiers
   that go into every receipt and ledger row. `MuseModel` becomes one
   implementation of it.
2. **OpenAI-compatible family via config.** The existing adapter already
   speaks the OpenAI chat shape; make base URL, model name, prices and
   key env var per provider so OpenAI, DeepSeek, Meta and a local server
   (vLLM or Ollama, operator's choice) are configuration, not code. Keep
   https-only and no-redirects for remote hosts; plain http stays
   loopback-only.
3. **Native adapters** for Anthropic and Google, each with a fake-API
   test in the style of `tests/test_muse.py` (no network, no key, $0).
   Fail-closed rules from `muse.py` apply unchanged: unparseable answers
   become evidence-free replies, missing usage settles at worst case,
   transport errors raise.
4. **Router.** A frozen routing table (JSON beside the contract) maps each
   role (planner, specialist, critic-judge, synthesizer, verifier) to a
   provider and model. The orchestrator asks the router for a model per
   role; nothing constructs a model directly. The table's hash goes into
   the task's first receipt next to the contract hash.
5. **Verifier role, cross-provider.** The flag-only judge runs on the
   verifier's model. The router refuses a table where verifier and
   specialist share a provider unless the table says `same_provider_ok`.
   Verifier identity comes from the orchestrator, never from a string an
   agent supplies (closes the parked finding).
6. **Ledger per provider.** Add provider and model columns; `totals()`
   and `spend()` report per tier. Prices are per provider in config.
7. **Timing.** Record wall-clock per model call (in the `model_call`
   receipt) and per task (in `task_end`). The runner prints seconds next
   to dollars.

## Demo (closes the milestone)

Run `live-check` with a routing table where the specialist is a cheap
model and the verifier is a different provider. Commit the transcript
under `docs/demos/B1.md`: the routing table, the receipts showing which
provider handled each role, the ledger totals per tier, and seconds per
call. Then an EXPERIMENTS.md entry (F7 plus a B1 finding) with the cost
per verified answer for that tier pair.

## Non-goals

- No new eval set. code-qa.v1 under the new router is a smoke check, not
  a measurement.
- No prompt tuning. Prompts stay as they are unless an adapter needs a
  format change, and that change is named in the PR.
- No web tools. That is B2.

## Order of PRs (each with its capability line)

1. Provider interface + config-driven OpenAI-compatible family + timing.
2. Router + ledger columns + cross-provider rule.
3. Anthropic adapter with fake-API test.
4. Google adapter with fake-API test.
5. Demo transcript + experiments entry.

Hardening budget inside B1: at most five hardening/eval PRs total.
