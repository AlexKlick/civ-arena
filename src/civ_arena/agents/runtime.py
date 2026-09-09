"""AgentProfile + runtime registry. The LLM seam: scripted policies now,
model-backed runtimes later behind the identical ``take_turn(facade)`` shape.
"""

from __future__ import annotations

import json
import random
from dataclasses import dataclass, field
from pathlib import Path
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
    proposer: Any = None  # config.LLMSpec when policy == "planner" + proposer block
    case_base: Any = None  # config.CaseBaseSpec when policy == "planner" + case_base
    decision_mode: str = "legacy"
    growth_autopilot: bool = False


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
                  on_post: Any = None, match_id: str | None = None,
                  audit: Any = None, opening_units_frozen: bool = False) -> AgentRuntime:
    if profile.decision_mode not in ("legacy", "strategic_autopilot"):
        raise ValueError("unknown decision_mode")
    if profile.decision_mode != "legacy" and profile.policy != "llm":
        raise ValueError("strategic_autopilot requires policy llm")
    if type(profile.growth_autopilot) is not bool:
        raise ValueError("growth_autopilot must be an explicit boolean")
    if profile.growth_autopilot and profile.decision_mode != "strategic_autopilot":
        raise ValueError("growth_autopilot requires strategic_autopilot")
    if profile.policy == "llm":
        from civ_arena.agents.llm.runtime import LLMAgentRuntime

        if profile.llm is None:
            raise ValueError(
                f"agent {profile.agent_id!r}: policy 'llm' requires an LLMSpec "
                "on the profile (config validation should have caught this)"
            )
        if profile.decision_mode == "strategic_autopilot" and not match_id:
            raise ValueError("strategic_autopilot requires trusted match_id")
        briefing = getattr(profile.llm, 'research_building_briefing', False)
        if type(briefing) is not bool or briefing and (
                profile.decision_mode != 'strategic_autopilot'
                or profile.llm.adaptive_context is None):
            raise ValueError('research_building_briefing requires strategic adaptive context')
        own_economy = getattr(profile.llm, 'own_economy_context', False)
        if type(own_economy) is not bool:
            raise ValueError('own_economy_context requires boolean opt-in')
        economic = getattr(profile.llm, 'economic_forecast', False)
        if type(economic) is not bool or economic and (
                profile.decision_mode != 'strategic_autopilot'
                or profile.llm.adaptive_context is None):
            raise ValueError('economic_forecast requires strategic adaptive context')
        runtime = LLMAgentRuntime.build(profile, telemetry=telemetry, diary=diary,
                                        strategy=strategy, on_post=on_post)
        if profile.decision_mode == "strategic_autopilot":
            from civ_arena.agents.llm.strategic_controller import StrategicController

            runtime.configure_strategic_controller(StrategicController(
                match_id=match_id, audit=audit, opening_units_frozen=opening_units_frozen,
                growth_autopilot=profile.growth_autopilot,
                economic_forecast=economic))
        return runtime
    if profile.policy == "expansionist":
        return ScriptedRuntime(profile=profile)
    if profile.policy == "turtler":
        return ScriptedRuntime(profile=profile)
    if profile.policy == "planner":
        from civ_arena.planner.runtime import PlannerRuntime

        proposer_client = None
        if profile.proposer is not None:
            from civ_arena.agents.llm.client import MiniMaxMessagesClient

            proposer_client = MiniMaxMessagesClient(
                profile.proposer, on_post=on_post)
        # M19b: the case base loads LOUDLY at construction (from_file raises
        # on missing/corrupt/wrong-schema) — a bad artifact fails the match
        # at startup, pre-spend, never mid-match
        case_base = None
        if profile.case_base is not None:
            from civ_arena.planner.casebase import CaseBase

            case_base = CaseBase.from_file(Path("configs") / profile.case_base.path)
        return PlannerRuntime(profile.player_id, profile.seed,
                              proposer=proposer_client, case_base=case_base)
    raise ValueError(f"unknown policy: {profile.policy}")


def rng_doc(runtime: Any) -> list[Any]:
    from civ_arena.canonical import rng_to_doc

    return rng_to_doc(runtime.rng)


def load_rng(runtime: Any, doc: list[Any]) -> None:
    from civ_arena.canonical import rng_from_doc

    runtime.rng = rng_from_doc(doc)


def strategy_audit_event(payload: dict) -> dict:
    """Keep strategy probabilities inside a JSON string, outside simulator canonical values."""
    envelope = {key: payload[key] for key in ('audit', 'match_id', 'agent_id', 'player_id', 'turn')}
    details = {key: value for key, value in payload.items() if key not in envelope}
    envelope['strategy_payload_json'] = json.dumps(
        details, sort_keys=True, separators=(',', ':'), allow_nan=False)
    return envelope
