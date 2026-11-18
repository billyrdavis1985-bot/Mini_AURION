"""Research task runner (B2 PR5): one question, one brief.

Live use (operator PC with provider keys):
  python eval/run_research.py --live --question "..." --out eval/brief-q1.json
      --cap-usd 5.00 --routing eval/routing-research-same.json
  python eval/run_research.py --live --verify eval/brief-q1.json
      --planted eval/planted-q1.json --out eval/verify-q1-same.json
      --cap-usd 5.00 --routing eval/routing-research-same.json --tag same

Run mode investigates with fetch_url, critic-checks every finding
(D1/D2), support-checks each one that passed (D3), registers the
forecasts (D4), and writes the brief. Verify mode re-runs support
verification over the brief's findings plus one planted claim on the
routing table's verifier seat. This is a demo runner, not an eval
set: the brief plus the two verify records are the deliverable.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from eval.grade_code_qa import (INVALID_CAUSES, NANO_PER_USD,  # noqa: E402
                                repo_state)
from mini_aurion.contract import (Criterion, TaskContract,  # noqa: E402
                                  contract_hash)
from mini_aurion.critic import Critic  # noqa: E402
from mini_aurion.fetcher import FetchBackend  # noqa: E402
from mini_aurion.ledger import Ledger  # noqa: E402
from mini_aurion.memory import Memory  # noqa: E402
from mini_aurion.orchestrator import Orchestrator  # noqa: E402
from mini_aurion.receipts import ReceiptStore  # noqa: E402
from mini_aurion.research import RESEARCH_INVESTIGATOR_PROMPT  # noqa: E402
from mini_aurion.router import (Router, RoutingError,  # noqa: E402
                                load_routing_table)
from mini_aurion.specialist import Specialist  # noqa: E402
from mini_aurion.tools import ToolRegistry, ToolWrapper  # noqa: E402

CONTRACT_ID = "research.v1"


def research_contract(question: str, cap_usd: str, max_tools: int = 12,
                      max_models: int = 8) -> TaskContract:
    """Scope is the web minus the D5 refusals, which live in
    fetch_url itself: the scope gate only sees path args, and
    fetch_url takes a url, so the contract carries budget and
    acceptance while the fetcher enforces the boundary. The tool
    and model budgets live here (not beside the contract) so the
    receipted contract hash reflects what the run allowed."""
    return TaskContract(
        contract_id=CONTRACT_ID, version=1, goal=question[:500],
        deliverable="cited brief: typed findings with URL, snapshot"
                    " hash and quote; forecasts with probability and"
                    " resolution date; flagged claims with the"
                    " verifier's reason",
        acceptance=(Criterion("A1", "mechanical",
                              "every fact carries a D1 citation"),
                    Criterion("A2", "mechanical",
                              "every passed forecast is registered")),
        spend_usd=cap_usd, messages=20, decompose=False,
        max_tools=max_tools, max_models=max_models)


def _a1_facts_cited(candidate: dict) -> bool:
    return all(f.get("critic") == "pass"
               for f in candidate["findings"]
               if f.get("type") == "fact")


def _a2_forecasts_registered(candidate: dict) -> bool:
    return all(f.get("registered", False)
               for f in candidate["findings"]
               if f.get("type") == "forecast"
               and f.get("critic") == "pass")


RESEARCH_ACCEPTANCE = (("A1", _a1_facts_cited),
                       ("A2", _a2_forecasts_registered))


def _fresh_dir(path: Path) -> Path:
    """First unused -aN variant, like the code-qa runner: reruns
    keep earlier receipts instead of deleting them."""
    if not path.exists():
        return path
    n = 2
    while Path(f"{path}-a{n}").exists():
        n += 1
    return Path(f"{path}-a{n}")


def _refuse(message: str) -> int:
    print(f"refusing to run: {message}")
    return 2


def _write_proc(state_dir: Path, task_id: str, ledger, routing_hash,
                status: str, end_status: str = "",
                seats: dict | None = None) -> None:
    """The Monitor's process table entry, mirroring the code-QA
    runner: written before the run and re-written after, so the
    demo run is visible in the Live view while it works."""
    proc = {
        "task_id": task_id,
        "pid": os.getpid(),
        "started": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "receipts": str(state_dir / "receipts.jsonl"),
        "ledger": str(ledger.path),
        "routing": routing_hash,
        "seats": seats or {},
        "status": status,
        "end_status": end_status,
    }
    (state_dir / "proc.json").write_text(json.dumps(proc, indent=1),
                                         encoding="utf-8")


def _load_routing(path: str) -> Router:
    try:
        return Router(load_routing_table(path))
    except RoutingError as exc:
        raise SystemExit(_refuse(str(exc)))


def _cap_nano(cap_src: str) -> int:
    try:
        return int(Decimal(cap_src) * NANO_PER_USD)
    except Exception:
        raise SystemExit(_refuse(f"bad cap {cap_src!r}"))


def _print_header(cap_src: str, flag: bool, routing_path: str,
                  router: Router, head: str) -> None:
    print(f"cap: {cap_src} USD ({'flag override' if flag else 'contract'})")
    print(f"contract: {CONTRACT_ID} v1")
    print(f"routing: {routing_path} {router.table_hash()[:19]}")
    for seat, who in sorted(router.resolved().items()):
        print(f"seat {seat}: {who['provider']}/{who['model']}")
    print(f"runner: {head}")


def run_mode(args, head: str) -> int:
    eval_started = time.perf_counter()
    router = _load_routing(args.routing)
    cap_nano = _cap_nano(args.cap_usd)
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    state_dir = _fresh_dir(out_path.parent / out_path.stem)
    state_dir.mkdir(parents=True, exist_ok=True)
    task_id = args.task_id or out_path.stem.replace("_", "-")
    _print_header(args.cap_usd, True, args.routing, router, head)
    print(f"budgets: {args.max_tools} tools, {args.max_models} models")
    contract = research_contract(args.question, args.cap_usd,
                                 max_tools=args.max_tools,
                                 max_models=args.max_models)
    receipts = ReceiptStore(state_dir / "receipts.jsonl")
    ledger = Ledger(state_dir / "ledger.sqlite3", task_cap_nano=cap_nano,
                    daily_cap_nano=cap_nano, global_cap_nano=cap_nano)
    memory = Memory(state_dir / "memory.sqlite3", file_root=state_dir,
                    receipt_store=receipts, snapshot_dir=state_dir,
                    mechanical_checks=frozenset({"critic"}))
    try:
        registry = ToolRegistry()
        registry.register(
            "fetch_url", "READ", 0,
            FetchBackend(state_dir),
            description="Fetch a public web page. The result names"
                        " the snapshot hash of what you read; cite that"
                        " hash, never a URL you did not fetch.",
            parameters={"type": "object",
                        "properties": {"url": {"type": "string"}},
                        "required": ["url"]})
        spec_model = router.model_for("specialist")
        orc_model = router.model_for("orchestrator")
        verifier_model = router.model_for("verifier")
        tools = ToolWrapper(registry, receipts=receipts, ledger=ledger,
                            stage="experimental", task_id=task_id,
                            agent_id="spec", scope=contract)
        spec = Specialist(
            "spec", spec_model, tools,
            max_model_calls=contract.max_models,
            max_tool_calls=contract.max_tools,
            model_estimate_nano=spec_model.worst_case_nano(
                [{"role": "brief", "text": args.question}]),
            investigator_prompt=RESEARCH_INVESTIGATOR_PROMPT)
        orc = Orchestrator(
            "orc", orc_model, {"spec": spec},
            Critic("critic", memory, receipts=receipts),
            receipts=receipts, ledger=ledger,
            model_estimate_nano=verifier_model.worst_case_nano(
                [{"role": "claim", "text": args.question}]),
            contract=contract, message_budget=contract.messages,
            verifier_model=verifier_model,
            routing_hash=router.table_hash(),
            acceptance=RESEARCH_ACCEPTANCE)
        seats = router.resolved()
        verifier_seat = seats.get("verifier", seats["orchestrator"])
        models = [f"{seats['specialist']['provider']}"
                  f"/{seats['specialist']['model']}",
                  f"{verifier_seat['provider']}"
                  f"/{verifier_seat['model']}"]
        _write_proc(state_dir, task_id, ledger, router.table_hash(),
                    status="running", seats=seats)
        result = orc.run_research(
            task_id, args.question, contract=contract,
            predictions_path=state_dir / "predictions.jsonl",
            models=models)
        # M3: an errored run is invalid, in the code-QA grader's
        # words. The receipts keep the underlying status; the brief
        # and the console say invalid so no transcript can cite it.
        cause = result.get("cause", "model")
        reasons = [f"invalid run: {cause}"] \
            if cause in INVALID_CAUSES else []
        status = "invalid" if reasons else result["status"]
        _write_proc(state_dir, task_id, ledger, router.table_hash(),
                    status="ended", end_status=status, seats=seats)
        by_provider = ledger.spend(task_id)["by_provider"]
        wall_ms = next((
            r["detail"].get("duration_ms", 0)
            for r in receipts.read_all()
            if r["action"] == "task_end"), 0)
        brief = {
            "question": args.question, "task_id": task_id,
            "status": status, "error": result["error"],
            "cause": cause,
            "contract": contract_hash(contract),
            "routing": router.table_hash(), "seats": seats,
            "runner_commit": head, "cap_usd": args.cap_usd,
            "state_dir": str(state_dir),
            "receipts": str(state_dir / "receipts.jsonl"),
            "predictions": str(state_dir / "predictions.jsonl"),
            "findings": result["findings"],
            "registered": result["registered"],
            "spent_nano": result["cost_nano"],
            "by_provider": by_provider, "wall_ms": wall_ms,
        }
        out_path.write_text(json.dumps(brief, indent=1))
        for i, finding in enumerate(result["findings"], 1):
            print(f"finding {i} [{finding['type']}]"
                  f" critic={finding['critic']}"
                  f" support={finding['support']}")
        flagged = [f for f in result["findings"]
                   if f["support"] != "supported"]
        print(f"status={status}"
              f" findings={len(result['findings'])}"
              f" registered={result['registered']}"
              f" flagged={len(flagged)}")
        for reason in reasons:
            print(f"INVALID {reason}")
        print(f"wrote {out_path}")
        print(f"eval spend: {result['cost_nano']} nanoUSD"
              f" ({result['cost_nano'] / NANO_PER_USD:.8f} USD)")
        for tier, nano in sorted(by_provider.items()):
            print(f"  tier {tier}: ${nano / NANO_PER_USD:.4f}")
        print(f"eval time: {time.perf_counter() - eval_started:.1f} s")
        return 0
    finally:
        memory.close()
        ledger.close()


def _replay_finding(finding: dict) -> dict:
    out = {"type": finding.get("type"), "claim": finding.get("claim", ""),
           "evidence": finding.get("evidence", [])}
    for key in ("probability", "resolves_at", "criterion"):
        if key in finding:
            out[key] = finding[key]
    return out


def verify_mode(args, head: str) -> int:
    eval_started = time.perf_counter()
    router = _load_routing(args.routing)
    cap_nano = _cap_nano(args.cap_usd)
    try:
        brief = json.loads(Path(args.verify).read_text())
        planted = json.loads(Path(args.planted).read_text())
    except (OSError, ValueError) as exc:
        return _refuse(f"cannot read inputs: {exc}")
    state_dir = Path(brief.get("state_dir", ""))
    if not state_dir.is_dir():
        return _refuse("brief names no state directory")
    tag = args.tag or "replay"
    task_id = args.task_id or f"verify-{tag}"
    _print_header(args.cap_usd, True, args.routing, router, head)
    receipts = ReceiptStore(state_dir / "receipts.jsonl")
    ledger = Ledger(_fresh_dir(state_dir / f"ledger-verify-{tag}.sqlite3"),
                    task_cap_nano=cap_nano, daily_cap_nano=cap_nano,
                    global_cap_nano=cap_nano)
    memory = Memory(state_dir / "memory.sqlite3", file_root=state_dir,
                    receipt_store=receipts, snapshot_dir=state_dir,
                    mechanical_checks=frozenset({"critic"}))
    try:
        verifier_model = router.model_for("verifier")
        orc = Orchestrator(
            "orc", router.model_for("orchestrator"), {},
            Critic("critic", memory, receipts=receipts),
            receipts=receipts, ledger=ledger,
            model_estimate_nano=verifier_model.worst_case_nano(
                [{"role": "claim", "text": brief.get("question", "")}]),
            verifier_model=verifier_model,
            routing_hash=router.table_hash())
        receipts.append(
            agent_id="orc", task_id=task_id, action="task_start",
            detail={"question": f"re-verify {brief.get('task_id', '?')}"
                    f" + planted claim [{tag}]",
                    "routing": router.table_hash()}, outcome="ok")
        # Forecasts replay with their register fields: without them
        # the D2 shape gate fails a finding the brief passed.
        findings = [_replay_finding(f) for f in brief.get("findings", [])]
        findings.append(planted)
        result = orc.verify_findings(
            task_id, findings, brief_task_id=brief.get("task_id", ""))
        receipts.append(
            agent_id="orc", task_id=task_id, action="task_end",
            detail={"status": "complete",
                    "findings": len(result["findings"]),
                    "flagged": len(
                        [f for f in result["findings"]
                         if f["support"] != "supported"])},
            outcome="ok")
        try:
            spent = ledger.spend(task_id)["settled_nano"]
        except Exception:
            spent = 0
        record = {
            "brief": args.verify, "tag": tag, "task_id": task_id,
            "planted_claim": planted.get("claim", ""),
            "verifier_seat": router.resolved().get("verifier", {}),
            "routing": router.table_hash(), "runner_commit": head,
            "findings": result["findings"], "spent_nano": spent,
            "by_provider": ledger.spend(task_id)["by_provider"],
            "elapsed_s": round(time.perf_counter() - eval_started, 1),
        }
        Path(args.out).write_text(json.dumps(record, indent=1))
        for i, finding in enumerate(result["findings"], 1):
            mark = "PLANTED" if i == len(result["findings"]) else \
                f"finding {i}"
            print(f"{mark} d1={finding['d1']} support={finding['support']}"
                  f" :: {finding['support_reason'][:80]}")
        print(f"wrote {args.out}")
        print(f"eval spend: {spent} nanoUSD"
              f" ({spent / NANO_PER_USD:.8f} USD)")
        print(f"eval time: {time.perf_counter() - eval_started:.1f} s")
        return 0
    finally:
        memory.close()
        ledger.close()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--question", default=None)
    parser.add_argument("--verify", default=None)
    parser.add_argument("--planted", default=None)
    parser.add_argument("--tag", default=None)
    parser.add_argument("--out", required=True)
    parser.add_argument("--cap-usd", default="5.00")
    parser.add_argument("--routing", required=True)
    parser.add_argument("--task-id", default=None)
    parser.add_argument("--max-tools", type=int, default=12)
    parser.add_argument("--max-models", type=int, default=8)
    args = parser.parse_args(argv)
    if not args.live:
        return _refuse("live model calls need --live")
    if bool(args.verify) == bool(args.question):
        return _refuse("pass --question or --verify, not both or neither")
    if args.verify and not args.planted:
        return _refuse("--verify needs --planted")
    if args.max_tools is not None and args.max_tools < 1:
        return _refuse("--max-tools must be a positive int")
    if args.max_models is not None and args.max_models < 1:
        return _refuse("--max-models must be a positive int")
    repo = Path(__file__).resolve().parent.parent
    head, dirty = repo_state(repo)
    if head is None or dirty:
        return _refuse("runner tree is unknown or has uncommitted"
                       " tracked changes; commit or stash first")
    if args.verify:
        return verify_mode(args, head)
    return run_mode(args, head)


if __name__ == "__main__":
    raise SystemExit(main())
