"""Frozen routing table: which provider answers each seat.

Seats, not roles: a task has three model objects at most — the
specialist's, the orchestrator's (planner, judge, and synthesis
share it), and optionally a separate verifier's for the judge. The
table is an operator-owned JSON file; its hash lands in task_start
next to the contract hash, so a transcript names the exact tier
assignment behind every receipt.

Cross-provider rule: the judge's seat must differ in provider from
the specialist's unless the table says same_provider_ok. A verifier
sharing the specialist's provider is a second opinion from the same
head. Models are built eagerly so a missing key or model refuses
before the task starts, never mid-run.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from .muse import PROVIDERS, MuseConfig, MuseModel

SEATS = ("specialist", "orchestrator", "verifier")


class RoutingError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.code = code


def load_routing_table(path: str | Path) -> dict:
    """Parse and structurally validate a routing table. Semantic
    checks (known providers, cross-provider rule) happen in Router,
    which also has the environment."""
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RoutingError("BAD_TABLE",
                           f"cannot read routing table: {exc}") from exc
    if not isinstance(raw, dict):
        raise RoutingError("BAD_TABLE", "routing table must be an object")
    table = {"same_provider_ok": raw.get("same_provider_ok", False)}
    if not isinstance(table["same_provider_ok"], bool):
        raise RoutingError("BAD_TABLE", "same_provider_ok must be a bool")
    for seat, entry in raw.items():
        if seat == "same_provider_ok":
            continue
        if seat not in SEATS:
            raise RoutingError("BAD_TABLE", f"unknown seat {seat!r}")
        if not isinstance(entry, dict):
            raise RoutingError("BAD_TABLE",
                               f"seat {seat!r} must be an object")
        provider = entry.get("provider", "")
        if not isinstance(provider, str) or not provider:
            raise RoutingError("BAD_TABLE",
                               f"seat {seat!r} names no provider")
        model = entry.get("model", "")
        if not isinstance(model, str):
            raise RoutingError("BAD_TABLE",
                               f"seat {seat!r} model must be a string")
        table[seat] = {"provider": provider, "model": model}
    if "specialist" not in table or "orchestrator" not in table:
        raise RoutingError("BAD_TABLE",
                           "table needs specialist and orchestrator seats")
    return table


class Router:
    def __init__(self, table: dict, environ=None):
        self.table = table
        self.models: dict[str, MuseModel] = {}
        for seat in SEATS:
            if seat not in table:
                continue
            provider = table[seat]["provider"]
            if provider != "meta" and provider not in PROVIDERS:
                raise RoutingError("BAD_PROVIDER",
                                   f"seat {seat!r}: unknown provider"
                                   f" {provider!r}")
            overrides = {}
            if table[seat].get("model", ""):
                overrides["model"] = table[seat]["model"]
            try:
                if provider == "meta":
                    config = MuseConfig.from_env(environ, **overrides)
                else:
                    config = MuseConfig.from_env(
                        environ, provider=provider, **overrides)
            except Exception as exc:
                raise RoutingError("NO_MODEL",
                                   f"seat {seat!r}: {exc}") from exc
            self.models[seat] = MuseModel(config)
        judge_seat = "verifier" if "verifier" in self.models \
            else "orchestrator"
        if self.models["specialist"].provider == \
                self.models[judge_seat].provider \
                and not table.get("same_provider_ok", False):
            raise RoutingError(
                "SAME_PROVIDER",
                f"specialist and {judge_seat} share provider"
                f" {self.models['specialist'].provider!r}; set"
                " same_provider_ok to allow it")

    def model_for(self, seat: str):
        if seat == "verifier" and seat not in self.models:
            return self.models["orchestrator"]
        if seat not in self.models:
            raise RoutingError("BAD_SEAT", f"unknown seat {seat!r}")
        return self.models[seat]

    def table_hash(self) -> str:
        canonical = json.dumps(self.table, sort_keys=True,
                               separators=(",", ":"))
        return "sha256:" + hashlib.sha256(
            canonical.encode("utf-8")).hexdigest()
