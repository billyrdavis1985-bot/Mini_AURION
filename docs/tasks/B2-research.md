# B2: Research (builder task)

**Moves VISION.md row:** "Research system" from Not started to Built.

**Capability line when done:** the operator asks a real question in
their own domain and gets a cited brief built from pages the system
actually fetched, where every factual claim points at a stored quote,
every forecast is registered, and the verifier's flags are listed.

**Decisions this builds on:** ADR-003 (D1 to D5). Read it first.

## Order of PRs (each with its capability line)

1. **`fetch_url` with safety and snapshots (D1, D5).** READ tool in the
   registry. DNS-resolve, refuse loopback/private/link-local/multicast,
   connect to the checked address (no second resolution), re-check
   every redirect hop, size cap, HTML/plain-text only, timeout,
   no cookies. HTML to text with the standard library. Store the
   snapshot content-addressed; the receipt carries the hash. Fake-server
   tests for every refusal, including a redirect to `127.0.0.1`. Live
   evidence: one real fetch (PR template rule).
2. **URL quotes in the critic (D1).** `url` evidence needs `snapshot`
   and a 12 to 200 character quote found in that snapshot, fetched in
   this task. Tests mirror the file-quote tests.
3. **Claim types and the forecast register (D2, D4).** The research
   investigator prompt asks for typed claims. The critic refuses untyped
   claims and uncited facts. Forecasts append to `predictions.jsonl`
   with a receipt.
4. **Support verifier (D3).** One verifier call per finding (relevance
   and support together) for the research task class, on the
   verifier/orchestrator seat from the routing table. Flag-only. Code-QA
   keeps its judge unchanged.
5. **Research task runner + demo.** A runner for one question under a
   research contract (scope: the web, minus the D5 refusals; budget
   from the contract). Demo transcript in `docs/demos/B2.md`, then an
   EXPERIMENTS.md entry.

PRs 1 to 4 do not need the operator's question. PR 5 does.

## Demo (closes the milestone)

- One real question from the operator. The brief: typed claims, every
  `fact` with its URL, snapshot hash and quote; forecasts listed with
  probability and resolution date; flagged claims listed with the
  verifier's reason.
- One planted claim with a real, correctly located quote that does not
  support it. Run the brief's verification twice: verifier on the
  **same** provider as the investigator, and on a **different** one.
  Record both outcomes. Not a pass/fail gate.
- Seats, dollars and seconds per tier, as in B1.

## Non-goals

- No forecast scoring, source ranking, PDFs or freshness rules
  (ADR-003, deferred list).
- No new eval set. The demo is the deliverable; measurement comes later
  if the planted-claim result is interesting.
- No change to the code-QA path or its prompts.

Hardening budget inside B2: one hardening PR per build PR.
