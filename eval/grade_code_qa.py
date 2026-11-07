"""Code-QA eval runner + grader (set code-qa.v1, frozen at c55a621).

Live use (operator PC with MODEL_API_KEY):
  worktree the frozen tree, then:
  python eval/grade_code_qa.py --live --runs 5 --tree <frozen-tree>
      --out eval/results-code-qa-v1.json

Grading is mechanical, no model judgment:
- code: claim names the exact function; a path:line cite resolves into the
  frozen tree and that line contains the expected text; must_mention strings
  appear in the claim.
- summary: one bullet per rule; every bullet cites a distinct in-span line.
- unanswerable: the run escalates with CANNOT_VERIFY.
"""
from __future__ import annotations

import argparse
import json
import os
import posixpath
import subprocess
import sys
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mini_aurion.critic import Critic  # noqa: E402
from mini_aurion.ledger import Ledger  # noqa: E402
from mini_aurion.memory import Memory  # noqa: E402
from mini_aurion.muse import MuseConfig, MuseError, MuseModel  # noqa: E402
from mini_aurion.orchestrator import Orchestrator  # noqa: E402
from mini_aurion.receipts import ReceiptStore  # noqa: E402
from mini_aurion.specialist import Specialist  # noqa: E402
from mini_aurion.tools import ToolRegistry, ToolWrapper  # noqa: E402

NANO_PER_USD = 1_000_000_000
CASES_PATH = Path(__file__).resolve().parent / "code_qa_v1.json"


class TreeReader:
    """Read-only file tool confined to the frozen eval tree."""

    last_cost_nano = 0

    def __init__(self, root: Path):
        self.root = root.resolve()

    def call(self, name: str, args: dict):
        if name == "list_dir":
            return self._list(str(args.get("path", ".")))
        if name != "read_file":
            raise KeyError(f"unknown tool {name!r}")
        return self._read(str(args.get("path", "")))

    def _confine(self, path: str):
        target = (self.root / path).resolve()
        try:
            target.relative_to(self.root)
        except ValueError:
            raise PermissionError("path escapes the tree") from None
        return target

    def _read(self, path: str) -> str:
        return self._confine(path).read_text(encoding="utf-8")

    def _list(self, path: str) -> str:
        target = self._confine(path)
        if not target.is_dir():
            raise NotADirectoryError(f"not a directory: {path!r}")
        entries = sorted(p.name + ("/" if p.is_dir() else "")
                         for p in target.iterdir())
        return "\n".join(entries)


def _norm(path: str) -> str:
    return posixpath.normpath(path.replace("\\", "/"))


def tree_commit(tree: Path) -> str | None:
    try:
        done = subprocess.run(["git", "-C", str(tree), "rev-parse", "HEAD"],
                              capture_output=True, text=True, timeout=15)
    except Exception:
        return None
    return done.stdout.strip() if done.returncode == 0 else None


def line_contains(tree: Path, path: str, lineno: int, text: str) -> bool:
    try:
        lines = (tree / path).read_text(encoding="utf-8").splitlines()
    except OSError:
        return False
    if lineno < 1 or lineno > len(lines):
        return False
    return text in lines[lineno - 1]


def grade_code(case: dict, claim: str, evidence: list, escalated: bool,
               tree: Path) -> tuple[bool, list[str]]:
    expect = case["expect"]
    reasons = []
    if escalated:
        return False, ["escalated instead of answering"]
    if expect["function"] not in claim:
        reasons.append(f"claim lacks {expect['function']}")
    hit = False
    for item in evidence:
        if item.get("kind") != "file":
            continue
        ref = str(item.get("ref", ""))
        path_part, _, line_part = ref.partition(":")
        if not line_part.isdigit():
            continue
        if _norm(path_part) != _norm(expect["path"]):
            continue
        if line_contains(tree, path_part, int(line_part),
                         expect["line_contains"]):
            hit = True
    if not hit:
        reasons.append(f"no {expect['path']}:<line> cite containing"
                       f" {expect['line_contains']!r}")
    for word in expect.get("must_mention", []):
        if word not in claim:
            reasons.append(f"claim lacks {word!r}")
    return (not reasons), reasons


def grade_summary(case: dict, text: str, evidence: list, escalated: bool,
                  tree: Path) -> tuple[bool, list[str]]:
    expect = case["expect"]
    reasons = []
    if escalated:
        return False, ["escalated instead of answering"]
    lo, hi = expect["span"]
    cited: set[int] = set()
    for item in evidence:
        if item.get("kind") != "file":
            continue
        ref = str(item.get("ref", ""))
        path_part, _, line_part = ref.partition(":")
        if _norm(path_part) != _norm(expect["path"]) \
                or not line_part.isdigit():
            continue
        lineno = int(line_part)
        if lo <= lineno <= hi:
            cited.add(lineno)
    if len(cited) < expect["bullets"]:
        reasons.append(f"cited {len(cited)} distinct in-span lines,"
                       f" need {expect['bullets']}")
    bullets = [line for line in text.splitlines()
               if line.strip().startswith("- ")]
    if len(bullets) != expect["bullets"]:
        reasons.append(f"answer has {len(bullets)} bullets,"
                       f" need {expect['bullets']}")
    return (not reasons), reasons


def grade_unanswerable(claim: str, escalated: bool) -> tuple[bool, list[str]]:
    if escalated and "CANNOT_VERIFY" in claim:
        return True, []
    return False, ["answered instead of escalating"]


def grade(case: dict, result: dict, tree: Path) -> tuple[bool, list[str]]:
    escalated = result["status"] == "escalated"
    if case["type"] == "code":
        return grade_code(case, result["text"], result["evidence"],
                          escalated, tree)
    if case["type"] == "summary":
        return grade_summary(case, result["text"], result["evidence"],
                             escalated, tree)
    return grade_unanswerable(result["text"], escalated)


def _fresh_dir(path: Path) -> Path:
    """A fresh directory per run. Earlier runs' receipts are never deleted;
    reruns spill to -a2, -a3, ... (OPERATIONS 5 and 7)."""
    if not path.exists():
        return path
    n = 2
    while (path.parent / f"{path.name}-a{n}").exists():
        n += 1
    return path.parent / f"{path.name}-a{n}"


def run_live(question: str, task_id: str, tree: Path, state_dir: Path,
             ledger: Ledger) -> dict:
    state_dir = _fresh_dir(state_dir)
    state_dir.mkdir(parents=True, exist_ok=True)
    receipts = ReceiptStore(state_dir / "receipts.jsonl")
    memory = Memory(state_dir / "memory.sqlite3", file_root=tree,
                    receipt_store=receipts)
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
            description="List a directory in the repo tree. Start here"
                        " to discover the layout.",
            parameters={"type": "object",
                        "properties": {"path": {"type": "string"}}})
        model = MuseModel(MuseConfig.from_env())
        estimate = model.worst_case_nano([{"role": "brief", "text": question}])
        tools = ToolWrapper(registry, receipts=receipts, ledger=ledger,
                            stage="experimental", task_id=task_id,
                            agent_id="spec")
        spec = Specialist("spec", model, tools,
                          model_estimate_nano=estimate)
        orc = Orchestrator("orc", model, {"spec": spec},
                           Critic("critic", memory, receipts=receipts),
                           receipts=receipts,
                           ledger=ledger, model_estimate_nano=estimate)
        result = orc.run(task_id, question)
        result["spent_nano"] = ledger.spend(task_id)["settled_nano"]
        return result
    finally:
        memory.close()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--runs", type=int, default=5)
    parser.add_argument("--tree", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--cap-usd", default="5.00")
    args = parser.parse_args(argv)
    if not args.live:
        print("refusing to run: live model calls need --live")
        return 2
    tree = Path(args.tree)
    cases = json.loads(CASES_PATH.read_text())["cases"]
    try:
        cap_nano = int(Decimal(args.cap_usd) * NANO_PER_USD)
    except Exception:
        print(f"refusing to run: bad cap {args.cap_usd!r}")
        return 2
    try:
        config = MuseConfig.from_env()
    except MuseError as exc:
        print(f"refusing to run: {exc}")
        return 2
    print(f"model: {config.model} at {config.input_usd_per_m}/"
          f"{config.output_usd_per_m} USD per M tokens (in/out)")
    frozen = json.loads(CASES_PATH.read_text())["frozen_commit"]
    got = tree_commit(tree)
    if got != frozen:
        print(f"refusing to run: tree is at {got}, eval is frozen at"
              f" {frozen}")
        return 2
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    eval_dir = out_path.parent / "code-qa-v1"
    eval_dir.mkdir(parents=True, exist_ok=True)
    ledger = Ledger(eval_dir / "ledger.sqlite3", task_cap_nano=cap_nano,
                    daily_cap_nano=cap_nano, global_cap_nano=cap_nano)
    report: dict = {"set": "code-qa.v1", "runs": args.runs, "cases": {}}
    for case in cases:
        passes = 0
        details = []
        for n in range(args.runs):
            task_id = f"{case['id']}-r{n + 1}"
            state = eval_dir / task_id
            result = run_live(case["question"], task_id, tree, state, ledger)
            ok, reasons = grade(case, result, tree)
            passes += ok
            details.append({"run": n + 1, "pass": ok, "reasons": reasons,
                            "status": result["status"],
                            "spent_nano": result.get("spent_nano", 0)})
            print(f"{case['id']} run {n + 1}/{args.runs}:"
                  f" {'PASS' if ok else 'FAIL ' + str(reasons)}")
        report["cases"][case["id"]] = {"passed": passes, "runs": args.runs,
                                       "stable_4_of_5": passes >= 4,
                                       "details": details}
    totals = ledger.totals()
    ledger.close()
    report["spent_nano"] = totals["settled_nano"]
    out_path.write_text(json.dumps(report, indent=1))
    print(f"wrote {out_path}")
    stable = sum(1 for c in report["cases"].values() if c["stable_4_of_5"])
    print(f"{stable}/{len(report['cases'])} cases stable at 4-of-5")
    print(f"eval spend: {totals['settled_nano']} nanoUSD"
          f" ({totals['settled_nano'] / NANO_PER_USD:.8f} USD)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
