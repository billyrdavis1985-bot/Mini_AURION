"""Mini-AURION command line. Milestone 3: live-check only."""
from __future__ import annotations

import os
import sys
from decimal import Decimal
from pathlib import Path

from .critic import Critic
from .ledger import Ledger
from .memory import Memory
from .muse import MuseConfig, MuseError, MuseModel
from .orchestrator import Orchestrator
from .receipts import ReceiptStore, verify_on_start
from .specialist import Specialist
from .tools import ToolRegistry, ToolWrapper

NANO_PER_USD = 1_000_000_000
QUESTION = ("Read note.txt in the corpus and report its exact topic sentence."
            " Cite the file.")
SEED_TEXT = "Mini-AURION proves governed agents on real tasks.\n"


class CorpusReader:
    """Single read-only tool for the live check, confined to one directory."""

    last_cost_nano = 0

    def __init__(self, root: Path):
        self.root = root.resolve()

    def call(self, name: str, args: dict):
        if name != "read_file":
            raise KeyError(f"unknown tool {name!r}")
        target = (self.root / str(args.get("path", ""))).resolve()
        try:
            target.relative_to(self.root)
        except ValueError:
            raise PermissionError("path escapes the corpus") from None
        return target.read_text(encoding="utf-8")


def live_check(corpus_dir: str, state_dir: str, cap_usd: str) -> int:
    try:
        cap_nano = int(Decimal(cap_usd) * NANO_PER_USD)
    except Exception:
        print(f"refusing to run: bad spend cap {cap_usd!r}")
        return 2
    if cap_nano <= 0:
        print("refusing to run: spend cap must be positive")
        return 2
    corpus = Path(corpus_dir)
    corpus.mkdir(parents=True, exist_ok=True)
    note = corpus / "note.txt"
    if not note.exists():
        note.write_text(SEED_TEXT, encoding="utf-8")
    state = Path(state_dir)
    state.mkdir(parents=True, exist_ok=True)
    receipts = ReceiptStore(state / "receipts.jsonl")
    try:
        verify_on_start(receipts.path)
    except Exception as exc:
        print(f"refusing to run: receipt check failed: {exc}")
        return 2
    try:
        config = MuseConfig.from_env()
    except MuseError as exc:
        print(f"refusing to run: {exc}")
        return 2
    ledger = Ledger(state / "ledger.sqlite3", task_cap_nano=cap_nano,
                    daily_cap_nano=cap_nano, global_cap_nano=cap_nano)
    memory = Memory(state / "memory.sqlite3", file_root=corpus,
                    receipt_store=receipts)
    try:
        reader = CorpusReader(corpus)
        registry = ToolRegistry()
        registry.register("read_file", "READ", 0, reader)
        model = MuseModel(config)
        estimate = model.worst_case_nano([{"role": "brief", "text": QUESTION}])
        tools = ToolWrapper(registry, receipts=receipts, ledger=ledger,
                            stage="experimental", task_id="live-1",
                            agent_id="spec")
        spec = Specialist("spec", model, tools,
                          model_estimate_nano=estimate)
        orc = Orchestrator("orc", model, {"spec": spec},
                           Critic("critic", memory), receipts=receipts,
                           ledger=ledger, model_estimate_nano=estimate)
        result = orc.run("live-1", QUESTION)
        totals = ledger.totals()
    except MuseError as exc:
        print(f"live call failed (fail-closed, nothing verified): {exc}")
        return 1
    finally:
        ledger.close()
        memory.close()
    print(f"status: {result['status']}")
    print(f"text: {result['text'][:500]}")
    print(f"evidence: {result['evidence']}")
    print(f"spend settled: {totals['settled_nano']} nanoUSD "
          f"({totals['settled_nano'] / NANO_PER_USD:.8f} USD)")
    print(f"receipts: {receipts.verify()} verified")
    return 0


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv == ["live-check"]:
        return live_check(
            os.environ.get("MINI_AURION_CORPUS", "var/live-corpus"),
            os.environ.get("MINI_AURION_STATE", "var/live-check"),
            os.environ.get("MINI_AURION_SPEND_CAP_USD", "1.00"))
    print("usage: python -m mini_aurion live-check")
    print("  env: MODEL_API_KEY (required), MINI_AURION_SPEND_CAP_USD"
          " (default 1.00)")
    return 2


if __name__ == "__main__":
    sys.exit(main())
