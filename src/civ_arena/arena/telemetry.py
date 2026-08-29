"""Per-agent attributable telemetry.

Token counters and the wire ``model`` label are reported by the LLM runtime
via ``note_model_usage`` (the referee never sees tokens); scripted-only
agents leave them at zero/None — the schema is identical either way.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class AgentStats:
    tool_calls: dict[str, int] = field(default_factory=dict)
    tool_errors: dict[str, int] = field(default_factory=dict)
    # model-side protocol failures (unknown tool name, malformed arguments)
    # live OUTSIDE tool_calls: nothing executed, no event was emitted, so
    # counting them as calls would break the telemetry-vs-log recount parity
    # (and event-ifying them would diverge the model-free replay)
    model_errors: dict[str, int] = field(default_factory=dict)
    total_calls: int = 0
    total_errors: int = 0
    total_ms: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    model: str | None = None

    def note_call(self, tool: str, ms: int, ok: bool) -> None:
        self.tool_calls[tool] = self.tool_calls.get(tool, 0) + 1
        self.total_calls += 1
        self.total_ms += max(0, int(ms))
        if not ok:
            self.tool_errors[tool] = self.tool_errors.get(tool, 0) + 1
            self.total_errors += 1

    def note_model_error(self, key: str) -> None:
        self.model_errors[key] = self.model_errors.get(key, 0) + 1

    def to_doc(self) -> dict[str, Any]:
        return {
            "tool_calls": dict(self.tool_calls),
            "tool_errors": dict(self.tool_errors),
            "model_errors": dict(self.model_errors),
            "total_calls": self.total_calls,
            "total_errors": self.total_errors,
            "total_ms": self.total_ms,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "model": self.model,
        }


class TelemetryRegistry:
    def __init__(self) -> None:
        self._agents: dict[str, AgentStats] = {}

    def stats_for(self, agent_id: str) -> AgentStats:
        if agent_id not in self._agents:
            self._agents[agent_id] = AgentStats()
        return self._agents[agent_id]

    def note_call(self, agent_id: str, tool: str, ms: int, ok: bool) -> None:
        self.stats_for(agent_id).note_call(tool, ms, ok)

    def note_model_error(self, agent_id: str, key: str) -> None:
        self.stats_for(agent_id).note_model_error(key)

    def note_model_usage(self, agent_id: str, model: str,
                         input_tokens: int, output_tokens: int) -> None:
        """Runtime-reported model usage (the referee never sees tokens).
        ``model`` is the wire label from the reply — what actually served
        the request, not a config display hint."""
        stats = self.stats_for(agent_id)
        stats.input_tokens += int(input_tokens)
        stats.output_tokens += int(output_tokens)
        stats.model = model

    def snapshot(self) -> dict[str, dict[str, Any]]:
        return {aid: s.to_doc() for aid, s in sorted(self._agents.items())}

    def merge_snapshot(self, docs: dict[str, dict[str, Any]]) -> None:
        """Fold a checkpointed snapshot back in (resume): a resumed match's
        summary then reports the WHOLE match, not just the post-crash leg."""
        for aid, doc in docs.items():
            stats = self.stats_for(aid)
            stats.input_tokens += int(doc.get("input_tokens", 0))
            stats.output_tokens += int(doc.get("output_tokens", 0))
            stats.total_ms += int(doc.get("total_ms", 0))
            if doc.get("model"):
                stats.model = doc["model"]
            for tool, n in (doc.get("tool_calls") or {}).items():
                stats.tool_calls[tool] = stats.tool_calls.get(tool, 0) + int(n)
                stats.total_calls += int(n)
            for tool, n in (doc.get("tool_errors") or {}).items():
                stats.tool_errors[tool] = stats.tool_errors.get(tool, 0) + int(n)
            for key, n in (doc.get("model_errors") or {}).items():
                stats.model_errors[key] = stats.model_errors.get(key, 0) + int(n)
                stats.total_errors += int(n)
