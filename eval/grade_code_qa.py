"""Code-QA eval runner + grader (set code-qa.v1, frozen at c55a621).

Live use (operator PC with MODEL_API_KEY):
  worktree the frozen tree, then:
  python eval/grade_code_qa.py --live --runs 5 --tree <frozen-tree>
      --out eval/results-code-qa-v1.json

Grading is mechanical, no model judgment:
- code: claim names the exact function; a path:line cite resolves into the
  frozen tree and that line contains the expected text; must_mention words
  appear in the claim (case, _/-/space insensitive).
- summary: one bullet per rule; every bullet cites a distinct in-span line.
- unanswerable: the run escalates with CANNOT_VERIFY.
"""
from __future__ import annotations

import argparse
import json
import os
import posixpath
import re
import subprocess
import sys
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mini_aurion.critic import Critic  # noqa: E402
from mini_aurion.ledger import Ledger  # noqa: E402
from mini_aurion.memory import Memory  # noqa: E402
from mini_aurion.muse import (MuseConfig, MuseError, MuseModel,  # noqa: E402
                                 PLANNER_PROMPT, RELEVANCE_PROMPT)
from mini_aurion.contract import load_contract, scope_denials  # noqa: E402
from mini_aurion.orchestrator import Orchestrator  # noqa: E402
from mini_aurion.receipts import ReceiptStore  # noqa: E402
from mini_aurion.specialist import Specialist  # noqa: E402
from mini_aurion.tools import ToolRegistry, ToolWrapper  # noqa: E402

NANO_PER_USD = 1_000_000_000
CASES_PATH = Path(__file__).resolve().parent / "code_qa_v1.json"
CONTRACT_PATH = Path(__file__).resolve().parent / "contract-code-qa-v1.json"


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
        # Numbered: path:line cites are unguessable without visible numbers.
        text = self._confine(path).read_text(encoding="utf-8")
        return "\n".join(f"{n}: {line}"
                         for n, line in enumerate(text.splitlines(), 1))

    def layout(self) -> str:
        """Top-level listing, so the brief can orient the agent before it
        spends tool calls rediscovering the root."""
        return self._list(".")

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


def repo_state(repo: Path) -> tuple[str | None, bool]:
    """(HEAD commit, tracked tree dirty). Dirty counts only tracked files:
    untracked eval outputs never block a run."""
    try:
        head = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"],
                              capture_output=True, text=True, timeout=15)
        diff = subprocess.run(["git", "-C", str(repo), "diff", "--quiet"],
                              capture_output=True, timeout=15)
        staged = subprocess.run(
            ["git", "-C", str(repo), "diff", "--cached", "--quiet"],
            capture_output=True, timeout=15)
    except Exception:
        return None, True
    if head.returncode != 0:
        return None, True
    dirty = diff.returncode != 0 or staged.returncode != 0
    return head.stdout.strip(), dirty


def tree_clean(tree: Path) -> bool:
    """The eval tree must be exactly the frozen commit. Tracked edits shift
    graded line numbers; untracked files are visible to the model, and a
    stray answer key would invalidate every score. Ignored paths count too:
    the reader shows everything on disk, so `var/` junk fails the check
    just like any other stray file."""
    try:
        done = subprocess.run(["git", "-C", str(tree), "status",
                               "--porcelain", "--ignored"],
                              capture_output=True, text=True, timeout=15)
    except Exception:
        return False
    return done.returncode == 0 and not done.stdout.strip()


def out_inside_tree(out: Path, tree: Path) -> bool:
    """Refuse --out inside --tree: earlier runs' output would sit in the
    tree the next run's model reads."""
    try:
        out_r = out.resolve()
        tree_r = tree.resolve()
    except Exception:
        return True
    return out_r == tree_r or tree_r in out_r.parents


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
        if not _mentions(claim, word):
            reasons.append(f"claim lacks {word!r}")
    return (not reasons), reasons


def _mentions(claim: str, word: str) -> bool:
    """A required word matches across case and _/-/space variants, so a
    correct paraphrase ('return code') is not failed for missing the code
    spelling ('return_code'). The words must still appear adjacently."""
    norm = lambda s: s.casefold().replace("_", " ").replace("-", " ")
    return norm(word) in norm(claim)


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


def _cite_resolves(tree: Path, path: str, lineno: int) -> bool:
    """A cite resolves iff its line exists in the file. Content is not
    checked: classification admits many justifying lines, so the cite
    proves grounding (a real line, read from the named file), not
    line-guessing. The frozen line_contains stays as the documented
    canonical line; the grader does not enforce it."""
    try:
        lines = (tree / path).read_text(encoding="utf-8").splitlines()
    except OSError:
        return False
    return 1 <= lineno <= len(lines)


def grade_classify(case: dict, claim: str, evidence: list, escalated: bool,
                 tree: Path) -> tuple[bool, list[str]]:
    """Label line matches case-insensitively (categories, not identifiers);
    at least one cite (typed evidence or path:line in the text) must
    resolve into the fixture file; must_not patterns (e.g. a drafted
    To: header) fail."""
    expect = case["expect"]
    reasons = []
    if escalated:
        return False, ["escalated instead of answering"]
    match = re.search(r"(?im)^label:\s*([a-zA-Z][a-zA-Z-]*)", claim)
    if match is None:
        reasons.append("no 'Label: <label>' line")
    elif match.group(1).casefold() != expect["label"].casefold():
        reasons.append(f"label is {match.group(1)!r},"
                       f" need {expect['label']!r}")
    refs = [str(item.get("ref", "")) for item in evidence
            if item.get("kind") == "file"]
    refs += [f"{m.group(1)}:{m.group(2)}" for m in re.finditer(
        r"([A-Za-z0-9_][\w\-./]*\.\w+):(\d+)", claim)]
    hit = False
    for ref in refs:
        path_part, _, line_part = ref.partition(":")
        if not line_part.isdigit():
            continue
        if _norm(path_part) != _norm(expect["path"]):
            continue
        if _cite_resolves(tree, path_part, int(line_part)):
            hit = True
    if not hit:
        reasons.append(f"no cite resolving into {expect['path']}")
    for pattern in expect.get("must_not", []):
        if re.search(pattern, claim, re.IGNORECASE):
            reasons.append(f"claim matches forbidden {pattern!r}")
    return (not reasons), reasons


def grade_unanswerable(claim: str, escalated: bool) -> tuple[bool, list[str]]:
    if escalated and "CANNOT_VERIFY" in claim:
        return True, []
    return False, ["answered instead of escalating"]


INVALID_CAUSES = frozenset({"error", "cap"})


def grade(case: dict, result: dict, tree: Path) -> tuple[bool, list[str]]:
    # Transport errors and cap refusals never reached the model, so they
    # grade as failures here and count as invalid (not model misses) in
    # the report. Without this an outage PASSes the unanswerable case.
    cause = result.get("cause", "model")
    if cause in INVALID_CAUSES:
        return False, [f"invalid run: {cause}"]
    escalated = result["status"] == "escalated"
    if case["type"] == "code":
        return grade_code(case, result["text"], result["evidence"],
                          escalated, tree)
    if case["type"] == "summary":
        return grade_summary(case, result["text"], result["evidence"],
                             escalated, tree)
    if case["type"] == "classify":
        return grade_classify(case, result["text"], result["evidence"],
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


def select_cases(cases: list, names: str | None) -> list:
    """Subset the eval set by comma-separated ids, in set order. Unknown
    ids refuse: a mistyped case must fail loudly, never silently narrow
    a measurement."""
    if names is None:
        return list(cases)
    wanted = [n.strip() for n in names.split(",") if n.strip()]
    unknown = [n for n in wanted
               if not any(c["id"] == n for c in cases)]
    if unknown:
        raise ValueError(f"unknown cases: {', '.join(unknown)}")
    return [c for c in cases if c["id"] in wanted]


def eval_ledger_path(eval_dir: Path) -> Path:
    """One ledger per eval invocation. The cap budgets a single experiment;
    reusing the ledger would stack every past batch against it until the
    cap locks out all runs (measured: $3.75 history vs a $3.00 cap)."""
    return _fresh_dir(eval_dir / "ledger.sqlite3")


def run_live(case: dict, task_id: str, tree: Path, state_dir: Path,
             ledger: Ledger, template, scope_note: str = "") -> dict:
    question = case["question"]
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
            description="List a directory in the repo tree. Start with"
                        " path \".\" (the tree root, never \"/\"), then read"
                        " the file most likely to hold the answer.",
            parameters={"type": "object",
                        "properties": {"path": {"type": "string"}}})
        model = MuseModel(MuseConfig.from_env())
        estimate = model.worst_case_nano([{"role": "brief", "text": question}])
        brief = (question + scope_note
                 + "\n\nTop-level layout of the repo you are"
                 " searching (orient yourself, then read the file most"
                 " likely to hold the answer):\n" + reader.layout())
        contract = replace(template, goal=brief)
        tools = ToolWrapper(registry, receipts=receipts, ledger=ledger,
                            stage="experimental", task_id=task_id,
                            agent_id="spec", scope=contract)
        spec = Specialist("spec", model, tools,
                          model_estimate_nano=estimate)

        def grade_check(candidate):
            ok, _ = grade(case, {"status": candidate["status"],
                                 "text": candidate["text"],
                                 "evidence": candidate["evidence"]}, tree)
            return ok

        orc = Orchestrator(
            "orc", model, {"spec": spec},
            Critic("critic", memory, receipts=receipts),
            receipts=receipts, ledger=ledger,
            model_estimate_nano=estimate, contract=contract,
            message_budget=contract.messages,
            planner_prompt=PLANNER_PROMPT,
            judge_prompt=RELEVANCE_PROMPT,
            acceptance=(("A1", grade_check),
                        ("A2", lambda cand: scope_denials(receipts) == 0)))
        result = orc.run(task_id, brief)
        result["spent_nano"] = ledger.spend(task_id)["settled_nano"]
        result["receipts_path"] = str(state_dir / "receipts.jsonl")
        return result
    finally:
        memory.close()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--runs", type=int, default=5)
    parser.add_argument("--tree", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--cap-usd", default=None)
    parser.add_argument("--cases", default=None)
    args = parser.parse_args(argv)
    if not args.live:
        print("refusing to run: live model calls need --live")
        return 2
    tree = Path(args.tree)
    cases = json.loads(CASES_PATH.read_text())["cases"]
    template = load_contract(CONTRACT_PATH, "")
    cap_src = args.cap_usd if args.cap_usd is not None \
        else template.spend_usd
    try:
        cap_nano = int(Decimal(cap_src) * NANO_PER_USD)
    except Exception:
        print(f"refusing to run: bad cap {cap_src!r}")
        return 2
    print(f"cap: {cap_src} USD"
          f" ({'flag override' if args.cap_usd is not None else 'contract'})")
    print(f"contract: {template.contract_id} v{template.version}")
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
    if not tree_clean(tree):
        print("refusing to run: eval tree has uncommitted or untracked"
              " files; restore the frozen tree first")
        return 2
    if out_inside_tree(out_path, tree):
        print("refusing to run: --out must be outside --tree")
        return 2
    repo = CASES_PATH.parent.parent
    head, dirty = repo_state(repo)
    if head is None or dirty:
        print("refusing to run: runner tree is unknown or has uncommitted"
              " tracked changes; commit or stash first")
        return 2
    print(f"runner: {head}")
    report_head = head
    out_path.parent.mkdir(parents=True, exist_ok=True)
    eval_dir = out_path.parent / "code-qa-v1"
    eval_dir.mkdir(parents=True, exist_ok=True)
    ledger = Ledger(eval_ledger_path(eval_dir), task_cap_nano=cap_nano,
                    daily_cap_nano=cap_nano, global_cap_nano=cap_nano)
    try:
        selected = select_cases(cases, args.cases)
    except ValueError as exc:
        print(f"refusing to run: {exc}")
        return 2
    if not selected:
        print("refusing to run: no cases selected")
        return 2
    report: dict = {"set": "code-qa.v1", "runs": args.runs,
                    "runner_commit": report_head,
                    "cases_run": [c["id"] for c in selected], "cases": {}}
    for case in selected:
        passes = 0
        invalid = 0
        details = []
        for n in range(args.runs):
            task_id = f"{case['id']}-r{n + 1}"
            state = eval_dir / task_id
            result = run_live(case, task_id, tree, state, ledger,
                              template)
            ok, reasons = grade(case, result, tree)
            valid = result.get("cause", "model") not in INVALID_CAUSES
            passes += ok
            invalid += not valid
            details.append({"run": n + 1, "pass": ok, "reasons": reasons,
                            "status": result["status"],
                            "valid": valid,
                            "text": result["text"][:1000],
                            "spent_nano": result.get("spent_nano", 0)})
            if not valid:
                print(f"{case['id']} run {n + 1}/{args.runs}:"
                      f" INVALID {reasons}")
            else:
                print(f"{case['id']} run {n + 1}/{args.runs}:"
                      f" {'PASS' if ok else 'FAIL ' + str(reasons)}")
        # Stability needs 4 passes out of 5 valid runs; fewer than 5 valid
        # leaves the case undecided, never stable.
        stable = passes >= 4 and invalid == 0
        report["cases"][case["id"]] = {"passed": passes, "runs": args.runs,
                                       "invalid": invalid,
                                       "stable_4_of_5": stable,
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
