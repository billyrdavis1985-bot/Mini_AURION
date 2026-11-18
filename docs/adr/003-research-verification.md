# ADR-003: Verifying research claims from outside sources

**Status:** proposed 2026-11-14 by the reviewer, for operator approval.
**Related:** OPERATIONS sections 1 (no agent's word is truth), 8 (tiers;
flag never clear), 9 (hostile input). ADR-001 (contracts), ADR-002
(demos, not gates). VISION.md B2. EXPERIMENTS F8 (verification
dominates cost), F11 (a passing verifier only shows it did not spoil a
correct answer).

## Context

B2 brings outside sources into the loop, for research in the operator's
domains: trade and marketing predictive value, probabilistic threat
mapping, economic pressure. Three gaps in today's code decide whether
B2's output can be trusted at all:

1. **URL evidence is weaker than file evidence.** A file citation must
   carry a 12 to 200 character quote that appears on its cited line
   (`memory.py`). A URL citation is only checked as "was fetched". No
   snapshot is kept, so nothing can check what the page said.
2. **The verifier seat checks relevance, not support.** The judge sees
   the question and the claim, never the evidence. It cannot tell a
   supported claim from an unsupported one, so a cross-provider
   verifier currently adds a second opinion on topic, not on truth.
3. **Forecasts cannot be verified when written.** "X is the weakest
   point in the economy" or "a 30% chance of Y by June" have no line to
   quote. Principle 1 ("verified when written") cannot apply to them as
   it does to facts.

This ADR decides only what B2's build depends on. Everything else is
deferred, by name, at the end.

## Decision

### D1. A URL citation is a quote inside a stored snapshot

- `fetch_url` stores every page it returns as a **snapshot**: the
  extracted text, saved content-addressed (`sha256` of the text) under
  the task's state directory, with URL, final URL, fetch time, HTTP
  status and content type. The `tool_call` receipt carries the hash.
- A `url` evidence item carries `ref` (the URL), `snapshot` (the hash)
  and `quote` (12 to 200 characters, the same bounds as file quotes).
- The critic checks mechanically: the snapshot exists, was fetched in
  this task (read-to-cite, as for files), and contains the quote
  verbatim after whitespace normalization. Any failure is a catch.
- **[code].** No model judgment. This is M5's file rule applied to
  pages.

### D2. Every research claim has a type

| Type | Example | Needs | Verified how |
| --- | --- | --- | --- |
| `fact` | "Container rates rose 18% in October." | at least one D1 citation | D1 (quote exists) + D3 (quote supports claim) |
| `assessment` | "Freight is the most exposed link." | the facts it rests on, each cited | its facts by D1/D3; the reasoning is flagged as judgment, never marked verified |
| `forecast` | "35% chance rates exceed X by 2027-03-31." | probability (0 to 1), resolution date, resolution criterion, the facts it rests on | inputs by D1/D3; the forecast goes to the register (D4) and is scored only when it resolves |

A claim with no type, or a `fact` with no citation, is refused by the
critic like any evidence-free claim today. **[code].**

### D3. The verifier seat checks support, flag-only

- For each `fact` and each fact under an `assessment` or `forecast`,
  the verifier seat receives the claim and **its quotes only** (not
  whole pages; F8 says the verifier is the cost, and quotes are
  short). It answers one line: `SUPPORTED`, `UNSUPPORTED`, or
  `OVERREACH` (the quote is real but the claim says more than it).
- `UNSUPPORTED` and `OVERREACH` flag the claim; flagged claims are
  listed, not silently dropped. `SUPPORTED` clears nothing on its own:
  a claim stands only if D1 passed **and** the verifier did not flag
  it (OPERATIONS section 8: lower tiers flag, never clear; the
  verifier is a flag, the mechanical check is the gate).
- The research task's verifier makes **one call per finding** covering
  relevance and support together, replacing the relevance-only judge
  for this task class. Code-QA keeps its current judge.
- Unparseable verifier replies are receipted errors that keep the
  claim flagged as "unverified", never cleared.

**As built (B2 demo, PR #83; recorded here per the B2 backlog).**
D3 as built differs from the above in four ways: two verifier
calls per finding (positive phrasing plus reason-why-not) that
must agree exactly on `SUPPORTED`, otherwise the claim fails;
no `OVERREACH` verdict (overreach reads as `UNSUPPORTED` with a
reason); model errors are a distinct `error` verdict that fails
closed and invalidates the run (M3), not a flag; relevance is not
checked (nothing filed in the demo was off-topic; multi-topic
briefs would need it back). The frozen demo measured agreement
8/8 (3 supported, 5
unsupported, 0 splits, 0 unparseable): the second call changed no
verdict at twice the dominant cost (F8). Retained for now on an
n=8 sample; the agreement count in `support_check` receipts is the
standing metric, and the first observed split reopens this
decision.

### D4. Forecasts go to a register; calibration comes later

- Every `forecast` is appended to `predictions.jsonl` (append-only,
  receipted like any action): claim, probability, resolution date,
  resolution criterion, task id, the models that made it, the
  snapshot hashes it rests on.
- **Nothing scores forecasts in B2.** When forecasts start resolving,
  calibration (Brier score, reliability by bucket, by model) is
  computed from the register. That is a later milestone, and it can
  only start once enough forecasts have resolved.
- This is what "calibration over time" means here, at the cost of one
  append per forecast now.

### D5. Fetched content is hostile input

- Page text is data, never instructions (section 9). It reaches the
  model only through the existing tool-result wrapping.
- `fetch_url` refuses loopback, private, link-local and multicast
  addresses **after** DNS resolution, connects to the address it
  checked (never re-resolving the name, which a rebinding DNS server
  would answer differently the second time), and re-checks every
  redirect hop (or refuses redirects). F12 showed the operator console lives on
  loopback; a page that says "fetch http://127.0.0.1:8787/" must get
  nothing.
- Size cap, content-type allowlist (HTML and plain text in B2), and a
  timeout. No cookies, no credentials, no JavaScript execution.

## The demo question this ADR makes answerable

B2's demo carries one planted claim whose quote is **real and correctly
located** but does not support the claim. D1 passes it; only D3 can
catch it. Running the same planted claim through a verifier on the
**same** provider as the investigator and one on a **different**
provider (two runs) records whether a verifier catches it at all, and
whether provider diversity changes that. Either result is a finding,
not a gate (ADR-002). It is the first direct evidence on the question
the tiers exist for.

## Deferred, by name

- Forecast scoring and calibration reports (needs resolved forecasts).
- Source reputation or ranking; multi-source corroboration rules.
- Numeric re-derivation (checking that a quoted number supports a
  computed figure).
- PDFs and non-text content.
- Freshness rules ("data older than N days is stale"). Snapshots carry
  fetch time, so this can be added without re-fetching.

## Consequences

- Research findings become as checkable as code findings: every
  `fact` points at bytes on disk that a reviewer can open.
- Verification cost stays bounded: one verifier call per finding, on
  quotes, not pages.
- The register starts collecting the data calibration needs, from the
  first B2 run, at almost no cost.
- `assessment` claims are honestly marked as judgment. The system will
  not pretend to verify reasoning it can only check the inputs of.

## Alternatives considered

- **Verifier reads whole pages.** Rejected: multiplies the dominant
  cost (F8) and gives an injected page the verifier's attention too.
- **No snapshots; re-fetch to verify.** Rejected: pages change, so a
  later check cannot prove what the agent saw.
- **Score forecasts now with a judge.** Rejected: a model cannot know
  whether a future event happens; only time can. The register is the
  honest version.
- **Design calibration fully before B2.** Rejected: it would be the F1
  pattern, designing ahead of any build. The register keeps the door
  open at almost no cost.
