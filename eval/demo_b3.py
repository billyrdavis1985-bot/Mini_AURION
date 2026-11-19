"""B3 demo runner: serial vs parallel wall-clock, frozen question.

Live use (operator PC with provider keys):
  python eval/demo_b3.py --live --tree <eval-tree> --out eval/b3demo.json
      --cap-usd 5.00 --routing eval/routing-b3.json

One script, two legs: the frozen two-part question runs first serial
(max_workers=1), then parallel (max_workers=2), with the same tree,
routing, and caps. Wall-clock comes from the receipts, not the
operator's stopwatch. This is a demo runner, not an eval set: the
transcript plus this report are the deliverable.
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import sys
import time
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from eval.grade_code_qa import (EXACTNESS_NOTE, NANO_PER_USD,  # noqa: E402
                                QUOTE_NOTE, TreeReader, grade,
                                repo_state, tree_commit)
from mini_aurion.contract import contract_hash, load_contract  # noqa: E402
from mini_aurion.contract import scope_denials  # noqa: E402
from mini_aurion.critic import Critic  # noqa: E402
from mini_aurion.ledger import Ledger  # noqa: E402
from mini_aurion.memory import Memory  # noqa: E402
from mini_aurion.muse import (PLANNER_PROMPT, RELEVANCE_PROMPT,  # noqa: E402
                              SYSTEM_PROMPT)
from mini_aurion.orchestrator import Orchestrator  # noqa: E402
from mini_aurion.receipts import ReceiptStore  # noqa: E402
from mini_aurion.router import (Router, RoutingError,  # noqa: E402
                                load_routing_table)
from mini_aurion.specialist import Specialist  # noqa: E402
from mini_aurion.tools import ToolRegistry, ToolWrapper  # noqa: E402

EVAL_DIR = Path(__file__).resolve().parent
QUESTION = ("Part 1: Which function refuses unregistered tools, and what "
            "receipt does the caller write for the refusal? Cite path:line. "
            "Part 2: Which function serializes concurrent spend reservations "
            "so caps cannot race? Cite path:line.")
PART_CASES = ("qa-unknown-tool", "qa-race-lock")


def _refuse(message: str) -> int:
    print(f"refusing to run: {message}")
    return 2


def _fresh_dir(path: Path) -> Path:
    if not path.exists():
        return path
    n = 2
    while Path(f"{path}-a{n}").exists():
        n += 1
    return Path(f"{path}-a{n}")


def _write_proc(state_dir: Path, task_id: str, ledger, routing_hash,
                status: str, end_status: str = "",
                seats: dict | None = None) -> None:
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


def _wall_clock(receipts_path: Path) -> dict:
    """Wall-clock vs model-work time, both from receipts. Wall is
    task_end minus task_start; work sums model_call durations. Their
    ratio is what the parallel leg is allowed to move."""
    start = end = None
    work_ms = 0
    for line in receipts_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        rec = json.loads(line)
        if rec.get("action") == "task_start":
            start = rec.get("ts")
        elif rec.get("action") == "task_end":
            end = rec.get("ts")
        elif rec.get("action") == "model_call":
            work_ms += int(rec.get("detail", {}).get("duration_ms", 0))
    wall_ms = None
    if start and end:
        fmt = "%Y-%m-%dT%H:%M:%SZ"
        wall_ms = int((datetime.datetime.strptime(end, fmt)
                       - datetime.datetime.strptime(start, fmt))
                      .total_seconds() * 1000)
    return {"wall_ms": wall_ms, "work_ms": work_ms}


def _seat_of(model) -> dict:
    return {"provider": getattr(model, "provider", "?"),
            "model": getattr(model, "model", "?")}


def legs_for(name: str) -> list:
    """Which legs one invocation runs. Single legs let the operator
    re-run a flaky leg without redoing the clean one; every attempt
    keeps its receipts under a -aN variant."""
    legs = {"serial": [("serial", 1)], "parallel": [("parallel", 2)],
            "both": [("serial", 1), ("parallel", 2)]}
    return legs[name]


def build_brief(question: str, tree: Path) -> str:
    """Demo brief, assembled exactly like the code-QA loop's: question
    plus the exactness/quote discipline notes plus the tree layout.
    The frozen question text itself is untouched."""
    reader = TreeReader(tree)
    return (question + EXACTNESS_NOTE + QUOTE_NOTE
            + "\n\nTop-level layout of the repo you are"
            " searching (orient yourself, then read the file most"
            " likely to hold the answer):\n" + reader.layout())


def run_leg(question: str, brief: str, task_id: str, tree: Path,
            state_dir: Path, contract, cases: dict, router: Router,
            cap_nano: int, max_workers: int, tag: str) -> dict:
    """One demo leg. The two legs differ only in max_workers and their
    state dir; question, tree, routing, contract, and caps are shared."""
    state_dir = _fresh_dir(state_dir)
    state_dir.mkdir(parents=True, exist_ok=True)
    ledger = Ledger(state_dir / "ledger.sqlite3", task_cap_nano=cap_nano,
                    daily_cap_nano=cap_nano, global_cap_nano=cap_nano)
    receipts = ReceiptStore(state_dir / "receipts.jsonl")
    memory = Memory(state_dir / "memory.sqlite3", file_root=tree,
                    receipt_store=receipts,
                    mechanical_checks=frozenset({"critic"}))
    try:
        registry = ToolRegistry()
        reader = TreeReader(tree)
        registry.register(
            "read_file", "READ", 0, reader,
            description="Read a file from the repo tree. Cite path:line.",
            parameters={"type": "object",
                        "properties": {"path": {"type": "string"}},
                        "required": ["path"]})
        registry.register(
            "list_dir", "READ", 0, reader,
            description="List a directory in the repo tree. Start with"
                        " path \".\" (the tree root, never \"/\"), then read"
                        " the file most likely to hold the answer.",
            parameters={"type": "object",
                        "properties": {"path": {"type": "string"}}})
        # Distinct seats, distinct model instances: sharing one model
        # would serialize the calls whose parallel gain we measure.
        spec_a_model = router.model_for("specialist")
        spec_b_model = router.model_for("specialist_b")
        orc_model = router.model_for("orchestrator")
        routing_hash = router.table_hash()
        specialists = {}
        for name, model in (("spec-a", spec_a_model),
                            ("spec-b", spec_b_model)):
            estimate = model.worst_case_nano(
                [{"role": "brief", "text": question}])
            tools = ToolWrapper(registry, receipts=receipts, ledger=ledger,
                                stage="experimental", task_id=task_id,
                                agent_id=name, scope=contract)
            specialists[name] = Specialist(
                name, model, tools, model_estimate_nano=estimate,
                investigator_prompt=SYSTEM_PROMPT)
        orc_estimate = orc_model.worst_case_nano(
            [{"role": "brief", "text": question}])

        def grade_both(candidate):
            result = {"status": candidate["status"],
                      "text": candidate["text"],
                      "evidence": candidate["evidence"]}
            for case_id in PART_CASES:
                ok, _ = grade(cases[case_id], result, tree)
                if not ok:
                    return False
            return True

        orc = Orchestrator(
            "orc", orc_model, specialists,
            Critic("critic", memory, receipts=receipts),
            receipts=receipts, ledger=ledger,
            model_estimate_nano=orc_estimate, contract=contract,
            message_budget=contract.messages,
            planner_prompt=PLANNER_PROMPT,
            judge_prompt=RELEVANCE_PROMPT,
            routing_hash=routing_hash, max_workers=max_workers,
            acceptance=(("A1", grade_both),
                        ("A2", lambda cand: scope_denials(receipts) == 0)))
        seats = {"specialist": _seat_of(spec_a_model),
                 "specialist_b": _seat_of(spec_b_model),
                 "orchestrator": _seat_of(orc_model)}
        _write_proc(state_dir, task_id, ledger, routing_hash,
                    status="running", seats=seats)
        result = orc.run(task_id, brief)
        _write_proc(state_dir, task_id, ledger, routing_hash,
                    status="ended", end_status=result["status"], seats=seats)
        clock = _wall_clock(state_dir / "receipts.jsonl")
        notes = [e for e in memory.recall(include_unverified=True)
                 if e.get("task_id") == task_id]
        return {
            "tag": tag, "task_id": task_id, "status": result["status"],
            "text": result["text"], "evidence": result["evidence"],
            "spent_nano": ledger.spend(task_id)["settled_nano"],
            "wall_ms": clock["wall_ms"], "work_ms": clock["work_ms"],
            "findings": [(f["subtask"], f["status"])
                         for f in result["findings"]],
            "notes_filed": len(notes),
            "notes_verified": sum(1 for n in notes
                                  if n["status"] == "verified"),
            "receipts": str(state_dir / "receipts.jsonl"),
        }
    finally:
        memory.close()
        ledger.close()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--tree", required=True)
    parser.add_argument("--routing", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--cap-usd", default="3.00")
    parser.add_argument("--task-id", default="b3demo")
    parser.add_argument("--legs", choices=("serial", "parallel", "both"),
                        default="both")
    args = parser.parse_args(argv)
    if not args.live:
        return _refuse("live model calls need --live")
    tree = Path(args.tree)
    if not tree.is_dir():
        return _refuse(f"no tree at {tree}")
    try:
        router = Router(load_routing_table(args.routing))
    except RoutingError as exc:
        return _refuse(str(exc))
    try:
        cap_nano = int(Decimal(args.cap_usd) * NANO_PER_USD)
    except Exception:
        return _refuse(f"bad cap {args.cap_usd!r}")
    if cap_nano <= 0:
        return _refuse("cap must be positive")
    repo = Path(__file__).resolve().parent.parent
    head, dirty = repo_state(repo)
    if head is None or dirty:
        return _refuse("runner tree is unknown or has uncommitted"
                       " tracked changes; commit or stash first")
    frozen = json.loads((EVAL_DIR / "code_qa_v1.json").read_text())[
        "frozen_commit"]
    got = tree_commit(tree)
    if got != frozen:
        return _refuse(f"tree is at {got}, eval is frozen at {frozen}")
    try:
        spec_b_model = router.model_for("specialist_b")
    except RoutingError as exc:
        return _refuse(f"demo routing needs a specialist_b seat: {exc}")
    del spec_b_model
    contract = load_contract(EVAL_DIR / "contract-b3-demo-v1.json",
                             QUESTION)
    if not contract.decompose:
        return _refuse("demo contract must leave decomposition on")
    cases = {c["id"]: c for c in json.loads(
        (EVAL_DIR / "code_qa_v1.json").read_text())["cases"]}
    missing = [c for c in PART_CASES if c not in cases]
    if missing:
        return _refuse(f"frozen set lacks demo parts: {missing}")
    brief = build_brief(QUESTION, tree)
    print(f"cap: {args.cap_usd} USD (flag override)")
    print(f"contract: {contract.contract_id} v{contract.version}")
    print(f"routing: {args.routing} {router.table_hash()[:19]}")
    for seat, who in sorted(router.resolved().items()):
        print(f"seat {seat}: {who['provider']}/{who['model']}")
    print(f"runner: {head}")
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    legs = {}
    for tag, workers in legs_for(args.legs):
        task_id = f"{args.task_id}-{tag}"
        state_dir = out_path.parent / task_id
        leg = run_leg(QUESTION, brief, task_id, tree, state_dir,
                      contract, cases, router, cap_nano, workers, tag)
        legs[tag] = leg
        spent = leg["spent_nano"] / NANO_PER_USD
        print(f"{tag}: status={leg['status']} spent=${spent:.4f} "
              f"wall_ms={leg['wall_ms']} work_ms={leg['work_ms']} "
              f"notes={leg['notes_verified']}/{leg['notes_filed']}")
    report = {
        "question": QUESTION,
        "contract": contract_hash(contract),
        "routing": router.table_hash(),
        "runner_commit": head,
        "cap_usd": args.cap_usd,
        "legs": legs,
    }
    serial_wall = legs.get("serial", {}).get("wall_ms")
    parallel_wall = legs.get("parallel", {}).get("wall_ms")
    report["speedup"] = None
    if serial_wall and parallel_wall:
        report["speedup"] = round(serial_wall / parallel_wall, 2)
        print(f"speedup: {report['speedup']}x "
              f"(serial {serial_wall}ms / parallel {parallel_wall}ms)")
    out_path.write_text(json.dumps(report, indent=1), encoding="utf-8")
    print(f"wrote {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
