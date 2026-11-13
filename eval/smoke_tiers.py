"""Two-tier live smoke: one drift case, two providers, both seats.

Live use (operator PC, both keys set):
  python eval/smoke_tiers.py --live --tree <frozen-tree>
      --out eval/results-tiers-smoke.json

Seats come from a routing table (default eval/routing-b1.json):
run A runs it as written, run B swaps specialist and orchestrator.
Each run grades, then reports which providers its receipts name and
what each tier cost. This is a smoke signal, not an eval set: two
runs prove the tiers connect, nothing about quality. Needs the key
row for every provider the table names.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from eval.grade_code_qa import (EXACTNESS_NOTE, NANO_PER_USD,  # noqa: E402
                                QUOTE_NOTE, eval_ledger_path, grade,
                                out_inside_tree, repo_state, run_live,
                                select_cases, tree_clean, tree_commit)
from eval.grade_drift import (CONTRACT_ID, DRIFT_PATH,  # noqa: E402
                              drift_contract, drift_note, frozen_match)
from mini_aurion.ledger import Ledger  # noqa: E402
from mini_aurion.receipts import ReceiptStore  # noqa: E402
from mini_aurion.router import (Router, RoutingError,  # noqa: E402
                                load_routing_table)


def providers_in(receipts_path: str) -> tuple[list, int]:
    """Providers named by model_call receipts, plus task wall ms."""
    seen: set = set()
    wall_ms = 0
    for row in ReceiptStore(receipts_path).read_all():
        if row["action"] == "model_call":
            seen.add(row["detail"].get("provider", "?"))
        elif row["action"] == "task_end":
            wall_ms = row["detail"].get("duration_ms", 0)
    return sorted(seen), wall_ms


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--tree", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--cap-usd", default="1.00")
    parser.add_argument("--case", default="drift-ledger-mechanism")
    parser.add_argument("--routing", default=None)
    args = parser.parse_args(argv)
    if not args.live:
        print("refusing to run: live model calls need --live")
        return 2
    eval_started = time.perf_counter()
    tree = Path(args.tree)
    frozen = json.loads(DRIFT_PATH.read_text())["frozen_tree"]
    got = tree_commit(tree)
    if not frozen_match(got, frozen):
        print(f"refusing to run: tree is at {got}, eval is frozen at"
              f" {frozen}")
        return 2
    out_path = Path(args.out)
    if not tree_clean(tree):
        print("refusing to run: eval tree has uncommitted or untracked"
              " files; restore the frozen tree first")
        return 2
    if out_inside_tree(out_path, tree):
        print("refusing to run: --out must be outside --tree")
        return 2
    repo = DRIFT_PATH.parent.parent
    head, dirty = repo_state(repo)
    if head is None or dirty:
        print("refusing to run: runner tree is unknown or has uncommitted"
              " tracked changes; commit or stash first")
        return 2
    try:
        cap_nano = int(Decimal(args.cap_usd) * NANO_PER_USD)
    except Exception:
        print(f"refusing to run: bad cap {args.cap_usd!r}")
        return 2
    routing_path = Path(args.routing) if args.routing else \
        DRIFT_PATH.parent / "routing-b1.json"
    try:
        table = load_routing_table(routing_path)
        router_a = Router(table)
    except RoutingError as exc:
        print(f"refusing to run: {exc}")
        return 2
    print(f"cap: {args.cap_usd} USD")
    print(f"contract: {CONTRACT_ID} v1")
    print(f"routing: {routing_path} {router_a.table_hash()[:19]}")
    for seat in ("specialist", "orchestrator", "verifier"):
        try:
            model = router_a.model_for(seat)
        except RoutingError:
            continue
        print(f"seat {seat}: {model.provider}/{model.model}")
    print(f"runner: {head}")
    cases = json.loads(DRIFT_PATH.read_text())["cases"]
    try:
        selected = select_cases(cases, args.case)
    except ValueError as exc:
        print(f"refusing to run: {exc}")
        return 2
    if len(selected) != 1:
        print("refusing to run: name exactly one case")
        return 2
    case = selected[0]
    out_path.parent.mkdir(parents=True, exist_ok=True)
    eval_dir = out_path.parent / "tiers-smoke"
    eval_dir.mkdir(parents=True, exist_ok=True)
    ledger = Ledger(eval_ledger_path(eval_dir), task_cap_nano=cap_nano,
                    daily_cap_nano=cap_nano, global_cap_nano=cap_nano)
    swapped = dict(table)
    swapped["specialist"] = table["orchestrator"]
    swapped["orchestrator"] = table["specialist"]
    try:
        router_b = Router(swapped)
    except RoutingError as exc:
        print(f"refusing to run swapped seats: {exc}")
        return 2
    seats = (("A", router_a), ("B", router_b))
    report: dict = {"case": case["id"], "runner_commit": head,
                    "runs": []}
    for label, router in seats:
        task_id = f"smoke-{label.lower()}"
        contract = drift_contract(case)
        spec = router.model_for("specialist")
        orc = router.model_for("orchestrator")
        assignment = (f"spec={spec.provider} orc={orc.provider}")
        result = run_live(case, task_id, tree, eval_dir / task_id,
                          ledger, contract,
                          scope_note=drift_note(case),
                          exactness_note=EXACTNESS_NOTE,
                          quote_note=QUOTE_NOTE, router=router)
        ok, reasons = grade(case, result, tree)
        providers, wall_ms = providers_in(result["receipts_path"])
        spent = result.get("spent_nano", 0)
        tiers = ledger.spend(task_id)["by_provider"]
        tag = "PASS" if ok else f"FAIL {reasons}"
        print(f"{label} {assignment}: {tag}"
              f" providers={','.join(providers)}"
              f" ${spent / NANO_PER_USD:.4f} {wall_ms / 1000:.0f}s")
        for tier, nano in sorted(tiers.items()):
            print(f"  tier {tier}: ${nano / NANO_PER_USD:.4f}")
        report["runs"].append(
            {"label": label, "assignment": assignment,
             "routing": router.table_hash(), "pass": ok,
             "reasons": reasons, "status": result["status"],
             "providers": providers, "text": result["text"][:1000],
             "spent_nano": spent, "by_provider": tiers,
             "wall_ms": wall_ms})
    totals = ledger.totals()
    ledger.close()
    report["spent_nano"] = totals["settled_nano"]
    report["elapsed_s"] = round(time.perf_counter() - eval_started, 1)
    out_path.write_text(json.dumps(report, indent=1))
    print(f"wrote {out_path}")
    print(f"eval spend: {totals['settled_nano']} nanoUSD"
          f" ({totals['settled_nano'] / NANO_PER_USD:.8f} USD)")
    print(f"eval time: {time.perf_counter() - eval_started:.1f} s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
