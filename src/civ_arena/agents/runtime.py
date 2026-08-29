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
    model: str | None = None  # display hint only; the wire label is LLMSpec.model_id
    llm: Any = None  # config.LLMSpec when policy == "llm"


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


def build_runtime(profile: AgentProfile, *, telemetry: Any = None,
                  diary: Any = None, strategy: Any = None,
                  on_post: Any = None) -> AgentRuntime:
    if profile.policy == "llm":
        from civ_arena.agents.llm.runtime import LLMAgentRuntime

        if profile.llm is None:
            raise ValueError(
                f"agent {profile.agent_id!r}: policy 'llm' requires an LLMSpec "
                "on the profile (config validation should have caught this)"
            )
        return LLMAgentRuntime.build(profile, telemetry=telemetry, diary=diary,
                                     strategy=strategy, on_post=on_post)
    if profile.policy == "expansionist":
        return ScriptedRuntime(profile=profile)
    if profile.policy == "turtler":
        return ScriptedRuntime(profile=profile)
    raise ValueError(f"unknown policy: {profile.policy}")


def rng_doc(runtime: Any) -> list[Any]:
    from civ_arena.canonical import rng_to_doc

    return rng_to_doc(runtime.rng)


def load_rng(runtime: Any, doc: list[Any]) -> None:
    from civ_arena.canonical import rng_from_doc

    runtime.rng = rng_from_doc(doc)
