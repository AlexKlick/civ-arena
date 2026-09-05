"""Network-free model doubles implementing the ModelClient protocol.

``script`` holds one assistant block list per REQUEST; past its end the last
entry is re-served (a never-ending model is just a one-entry script).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from civ_arena.agents.llm.client import ModelReply


def use(name: str, input: dict | None = None, id: str | None = None) -> dict:
    return {"type": "tool_use", "id": id or f"id-{name}", "name": name,
            "input": dict(input or {})}


def text(t: str) -> dict:
    return {"type": "text", "text": t}


@dataclass
class FakeModel:
    script: list[list[dict]]
    requests: list[dict[str, Any]] = field(default_factory=list)
    usage: tuple[int, int] = (10, 20)
    model_name: str = "fake-model"
    posts_sent: int = 0
    closed: bool = False
    on_post: Any = None  # mirrors the real client's durable-spend hook

    async def create(self, *, system: str, messages: list[dict],
                     tools: list[dict], tool_choice: dict | None = None) -> ModelReply:
        self.requests.append({
            "system": system,
            "messages": [dict(m) for m in messages],
            "tools": tools,
        })
        if tool_choice is not None:
            self.requests[-1]['tool_choice'] = dict(tool_choice)
        self.posts_sent += 1
        if self.on_post is not None:
            self.on_post()
        blocks = self.script[min(self.posts_sent - 1, len(self.script) - 1)]
        return ModelReply(content=[dict(b) for b in blocks],
                          stop_reason="tool_use", model=self.model_name,
                          input_tokens=self.usage[0],
                          output_tokens=self.usage[1])

    async def aclose(self) -> None:
        self.closed = True


def never_ends_model() -> FakeModel:
    """Answers get_overview forever — never calls end_turn."""
    return FakeModel(script=[[use("get_overview")]])


def garbage_model() -> FakeModel:
    """Round 1: unknown tool + an argument-less action; round 2: recovers."""
    return FakeModel(script=[
        [use("no_such_tool"), use("move_unit", {})],
        [use("end_turn")],
    ])
