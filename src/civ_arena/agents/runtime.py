"""AgentProfile + runtime registry. The LLM seam: scripted policies now,
model-backed runtimes later behind the identical ``take_turn(facade)`` shape.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import Any

from civ_arena.session.player_session import AgentRuntime


@dataclass
class AgentProfile:
    agent_id: str
    player_id: int
    policy: str
    seed: int
    model: str | None = None  # deferred to the post-spike LLM stage


@dataclass
class ScriptedRuntime:
    """Deterministic policy bot. Owns its threaded rng (checkpointed)."""

    profile: AgentProfile
    rng: random.Random = field(default=None)  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.rng is None:
            self.rng = random.Random(self.profile.seed)

    async def take_turn(self, facade: Any) -> None:
        from civ_arena.agents.scripted import run_policy

        await run_policy(self, facade)


def build_runtime(profile: AgentProfile) -> AgentRuntime:
    if profile.policy == "expansionist":
        return ScriptedRuntime(profile=profile)
    if profile.policy == "turtler":
        return ScriptedRuntime(profile=profile)
    raise ValueError(f"unknown policy: {profile.policy}")


def rng_doc(runtime: ScriptedRuntime) -> list[Any]:
    from civ_arena.canonical import rng_to_doc

    return rng_to_doc(runtime.rng)


def load_rng(runtime: ScriptedRuntime, doc: list[Any]) -> None:
    from civ_arena.canonical import rng_from_doc

    runtime.rng = rng_from_doc(doc)
