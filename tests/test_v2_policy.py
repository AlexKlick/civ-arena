from __future__ import annotations

from typing import Any

import pytest

from civ_arena.agents.llm.runtime import LLMAgentRuntime
from civ_arena.agents.runtime import AgentProfile
from civ_arena.arena.diary import DiaryStore
from civ_arena.arena.telemetry import TelemetryRegistry
from civ_arena.config import LLMSpec
from civ_arena.strategy.store import StrategyStore
from civ_arena.v2.contracts import (
    ActionKindV2,
    PolicyDescriptorV2,
    PolicyKindV2,
    TurnContextV2,
    TurnProposalV2,
    TurnTerminationV2,
)
from civ_arena.v2.enumeration import ActionEnumeratorV2
from civ_arena.v2.environment import simulator_facets_v2
from civ_arena.v2.executor import TransactionalExecutorV2
from civ_arena.v2.graph import ActionGraphCompilerV2
from civ_arena.v2.policy import (
    ALL_ACTION_KINDS_V2,
    LegacyPolicyRuntimeV2,
    PolicyServicesV2,
    ReplayPolicyRuntimeV2,
    build_policy_runtime_v2,
    policy_descriptor_v2,
)
from civ_arena.v2.schemas import ContractError
from fakes import FakeModel, use


async def _turn_context(
    profile: AgentProfile,
) -> tuple[Any, Any, TurnContextV2, PolicyDescriptorV2]:
    environment, monitor = simulator_facets_v2()
    await environment.reset({"seed": 81})
    observation = await environment.begin_turn(profile.player_id, 1)
    legal = ActionEnumeratorV2().enumerate(observation)
    graph = ActionGraphCompilerV2().compile(observation, legal)
    descriptor = policy_descriptor_v2(profile)
    return environment, monitor, TurnContextV2(observation, legal, graph, descriptor), descriptor


class _InspectingRuntime:
    def __init__(self) -> None:
        self.views: dict[str, Any] = {}

    async def take_turn(self, facade: Any) -> None:
        assert not hasattr(facade, "adapter")
        assert not hasattr(facade, "referee")
        assert not hasattr(facade, "environment")
        assert not hasattr(facade, "context")
        assert "execute_authorized" not in dir(facade)
        assert set(facade.names()) == {
            "attack",
            "end_turn",
            "fortify",
            "found_city",
            "get_available_production",
            "get_available_research",
            "get_cities",
            "get_overview",
            "get_strategy",
            "get_units",
            "get_visible_map",
            "move_unit",
            "purchase",
            "recall_lessons",
            "record_lesson",
            "record_prediction",
            "set_city_production",
            "set_goal",
            "set_research",
            "write_diary",
        }
        self.views = {
            "overview": await facade.get_overview(),
            "units": await facade.get_units(),
            "cities": await facade.get_cities(),
            "map": await facade.get_visible_map(),
            "research": await facade.get_available_research(),
        }
        # Returned values are detached from the typed observation.
        self.views["overview"]["you"]["gold"] = 99_999
        assert (await facade.get_overview())["you"]["gold"] != 99_999

        await facade.write_diary("remember the observable frontier")
        await facade.record_lesson("research before closing the turn")
        research = self.views["research"][0]
        await facade.set_research(research["tech_id"])
        # end_turn is absent while research is mandatory, but it remains a
        # typed untrusted intent for the executor to remap after revalidation.
        await facade.end_turn()


@pytest.mark.asyncio
async def test_policy_adapter_only_observes_and_proposes_then_executor_mutates() -> None:
    profile = AgentProfile("safe-script", 0, "expansionist", 9)
    environment, monitor, context, descriptor = await _turn_context(profile)
    telemetry = TelemetryRegistry()
    services = PolicyServicesV2(
        agent_id=profile.agent_id,
        player_id=profile.player_id,
        diary=DiaryStore(),
        strategy=StrategyStore(),
        telemetry=telemetry,
    )
    legacy = _InspectingRuntime()
    runtime = LegacyPolicyRuntimeV2(legacy, descriptor, services)
    before = monitor.state_hash()

    proposal = await runtime.propose_turn(context)

    assert monitor.state_hash() == before
    assert [intent.action_kind for intent in proposal.intents] == [
        ActionKindV2.SET_RESEARCH,
        ActionKindV2.END_TURN,
    ]
    assert services.diary.get(0) == "remember the observable frontier"
    assert services.strategy.lesson_list(0)[0].text == (
        "research before closing the turn"
    )
    assert all("private" not in str(value).lower() for value in legacy.views.values())

    receipt = await TransactionalExecutorV2(
        environment,
        descriptor,
        episode_id="policy-adapter",
    ).execute(proposal, context.observation, context.graph)
    assert receipt.termination is TurnTerminationV2.COMPLETED
    assert monitor.state_hash() != before
    assert [result.status.value for result in receipt.results] == [
        "accepted",
        "accepted",
    ]
    stats = telemetry.snapshot()[profile.agent_id]
    assert stats["tool_calls"]["set_research"] == 1
    assert stats["tool_calls"]["write_diary"] == 1


@pytest.mark.asyncio
async def test_real_scripted_runtime_produces_one_proposal_without_mutation() -> None:
    profile = AgentProfile("expansion-v2", 0, "expansionist", 17)
    environment, monitor, context, _descriptor = await _turn_context(profile)
    services = PolicyServicesV2(profile.agent_id, profile.player_id)
    runtime = build_policy_runtime_v2(profile, services=services)
    before = monitor.state_hash()

    proposal = await runtime.propose_turn(context)

    assert monitor.state_hash() == before
    assert proposal.intents
    assert proposal.intents[-1].action_kind is ActionKindV2.END_TURN
    assert all(intent.action_kind in ALL_ACTION_KINDS_V2 for intent in proposal.intents)
    receipt = await TransactionalExecutorV2(
        environment,
        runtime.descriptor,
        episode_id="scripted-v2",
    ).execute(proposal, context.observation, context.graph)
    assert receipt.termination is TurnTerminationV2.COMPLETED


@pytest.mark.asyncio
async def test_real_planner_runtime_uses_the_same_proposal_boundary() -> None:
    profile = AgentProfile("planner-v2", 0, "planner", 23)
    environment, monitor, context, _descriptor = await _turn_context(profile)
    services = PolicyServicesV2(profile.agent_id, profile.player_id)
    runtime = build_policy_runtime_v2(profile, services=services)
    before = monitor.state_hash()

    proposal = await runtime.propose_turn(context)

    assert monitor.state_hash() == before
    assert proposal.intents[-1].action_kind is ActionKindV2.END_TURN
    receipt = await TransactionalExecutorV2(
        environment,
        runtime.descriptor,
        episode_id="planner-v2",
    ).execute(proposal, context.observation, context.graph)
    # Founding reveals a new mandatory production decision whose city identity
    # was not observable when the first proposal was authored. It is refused
    # honestly, then a fresh policy proposal resolves it; no forced end-turn.
    assert receipt.termination is TurnTerminationV2.MANDATORY_UNRESOLVED
    for _ in range(2):
        observation = await environment.observe(profile.player_id)
        legal = ActionEnumeratorV2().enumerate(observation)
        graph = ActionGraphCompilerV2().compile(observation, legal)
        proposal = await runtime.propose_turn(
            TurnContextV2(observation, legal, graph, runtime.descriptor)
        )
        receipt = await TransactionalExecutorV2(
            environment,
            runtime.descriptor,
            episode_id="planner-v2",
        ).execute(proposal, observation, graph)
        if receipt.termination is TurnTerminationV2.COMPLETED:
            break
    assert receipt.termination is TurnTerminationV2.COMPLETED


@pytest.mark.asyncio
async def test_real_llm_runtime_uses_the_same_proposal_boundary() -> None:
    llm = LLMSpec(
        base_url="http://fake.local/anthropic/v1",
        api_key_env="NEVER_READ",
        model_id="fake-model",
    )
    profile = AgentProfile("llm-v2", 0, "llm", 29, llm=llm)
    environment, monitor, context, _descriptor = await _turn_context(profile)
    services = PolicyServicesV2(
        profile.agent_id,
        profile.player_id,
        telemetry=TelemetryRegistry(),
    )
    fake = FakeModel(
        script=[
            [use("set_research", {"tech_id": "MINING"})],
            [use("end_turn")],
        ]
    )
    legacy = LLMAgentRuntime.build(
        profile,
        client=fake,
        telemetry=services.telemetry,
        diary=services.diary,
        strategy=services.strategy,
        on_post=services.note_model_post,
    )
    runtime = build_policy_runtime_v2(profile, services=services, runtime=legacy)
    before = monitor.state_hash()

    proposal = await runtime.propose_turn(context)

    assert monitor.state_hash() == before
    assert [intent.action_kind for intent in proposal.intents] == [
        ActionKindV2.SET_RESEARCH,
        ActionKindV2.END_TURN,
    ]
    assert services.model_posts == 2
    receipt = await TransactionalExecutorV2(
        environment,
        runtime.descriptor,
        episode_id="llm-v2",
    ).execute(proposal, context.observation, context.graph)
    assert receipt.termination is TurnTerminationV2.COMPLETED


class _AbsentActionRuntime:
    async def take_turn(self, facade: Any) -> None:
        result = await facade.move_unit("u-does-not-exist", "0,0")
        assert result["status"] == "rejected"
        await facade.end_turn()


@pytest.mark.asyncio
async def test_absent_policy_action_is_refused_while_mandatory_fallback_completes() -> None:
    profile = AgentProfile("absent-v2", 0, "turtler", 3)
    environment, monitor, context, descriptor = await _turn_context(profile)
    services = PolicyServicesV2(profile.agent_id, profile.player_id)
    runtime = LegacyPolicyRuntimeV2(_AbsentActionRuntime(), descriptor, services)
    before = monitor.state_hash()

    proposal = await runtime.propose_turn(context)
    assert len(proposal.intents) == 3
    receipt = await TransactionalExecutorV2(
        environment,
        descriptor,
        episode_id="absent-policy-v2",
    ).execute(proposal, context.observation, context.graph)

    # The absent move has a refused authorization and therefore no result;
    # the policy-side mandatory fallback resolves research before end_turn.
    assert monitor.state_hash() != before
    assert receipt.termination is TurnTerminationV2.COMPLETED
    refused = [item for item in receipt.authorizations if item.action_id is None]
    assert len(refused) == 1
    assert refused[0].reason_code.value == "action_absent"
    assert all(
        result.authorization_id != refused[0].authorization_id
        for result in receipt.results
    )


@pytest.mark.asyncio
async def test_replay_runtime_requires_exact_policy_and_observation_binding() -> None:
    profile = AgentProfile("replay-v2", 0, "turtler", 4)
    _environment, _monitor, context, _descriptor = await _turn_context(profile)
    replay_descriptor = PolicyDescriptorV2.create(
        policy_kind=PolicyKindV2.REPLAY,
        policy_version="2.0",
        registered_action_kinds=ALL_ACTION_KINDS_V2,
    )
    proposal = TurnProposalV2.create(
        policy_id=replay_descriptor.descriptor_id,
        observation_id=context.observation.observation_id,
        intents=[],
    )
    replay_context = TurnContextV2(
        context.observation,
        context.legal_actions,
        context.graph,
        replay_descriptor,
    )
    runtime = ReplayPolicyRuntimeV2(proposal, replay_descriptor)
    assert await runtime.propose_turn(replay_context) == proposal

    wrong = PolicyDescriptorV2.create(
        policy_kind=PolicyKindV2.REPLAY,
        policy_version="2.1",
        registered_action_kinds=ALL_ACTION_KINDS_V2,
    )
    with pytest.raises(ContractError, match="descriptor mismatch"):
        await runtime.propose_turn(
            TurnContextV2(
                context.observation,
                context.legal_actions,
                context.graph,
                wrong,
            )
        )


def test_policy_descriptor_is_secret_free_and_seat_specific() -> None:
    first = policy_descriptor_v2(AgentProfile("a", 0, "turtler", 1))
    second = policy_descriptor_v2(AgentProfile("a", 1, "turtler", 1))
    assert first != second
    assert first.policy_kind is PolicyKindV2.SCRIPTED
    assert first.observation_only is True
    assert set(first.registered_action_kinds) == set(ActionKindV2)
    assert "key" not in str(first.to_doc()).lower()
