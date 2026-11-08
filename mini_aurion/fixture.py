"""Scripted fake model + tools for $0 deterministic tests.

FakeModel returns queued responses in order. When the queue is empty it
returns the honesty default: no tool calls and CANNOT_VERIFY text, which is
what a well-behaved agent says instead of guessing.

FakeTools serves canned outputs per tool name and records every invocation.
Costs are always zero; total_cost_nano pins that property for tests.
"""
from __future__ import annotations

ESCALATE_TEXT = "CANNOT_VERIFY"


class FixtureError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.code = code


class FakeModel:
    def __init__(self, script: list[dict] | None = None, *, model_id: str = "fake.m0"):
        self.script = list(script or [])
        self.model_id = model_id
        self.calls = 0
        self.last_cost_nano = 0
        self.seen: list[list] = []
        self.offered: list[list] = []
        self.systems: list = []

    def complete(self, messages: list[dict], tools: list[str],
                 system_prompt: str | None = None) -> dict:
        self.calls += 1
        self.seen.append(list(messages))
        self.offered.append(list(tools))
        self.systems.append(system_prompt)
        if self.script:
            reply = self.script.pop(0)
            return {"text": reply.get("text", ""),
                    "tool_calls": list(reply.get("tool_calls", [])),
                    "evidence": list(reply.get("evidence", [])),
                    "confidence": reply.get("confidence", 0.0),
                    "subtasks": list(reply.get("subtasks", []))}
        return {"text": ESCALATE_TEXT, "tool_calls": []}

    def total_cost_nano(self) -> int:
        return 0


class FakeTools:
    def __init__(self, outputs: dict[str, object] | None = None):
        self.outputs = dict(outputs or {})
        self.invocations: list[dict] = []

    def call(self, name: str, args: dict) -> object:
        self.invocations.append({"tool": name, "args": dict(args)})
        if name not in self.outputs:
            raise FixtureError("UNKNOWN_TOOL", f"no canned output for {name!r}")
        output = self.outputs[name]
        return output(args) if callable(output) else output

    def total_cost_nano(self) -> int:
        return 0
