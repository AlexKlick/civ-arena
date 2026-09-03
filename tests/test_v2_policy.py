from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from civ_arena.agents.llm.runtime import LLMAgentRuntime
from civ_arena.agents.runtime import AgentProfile
from civ_arena.arena.diary import DiaryStore
from civ_arena.arena.telemetry import TelemetryRegistry
from civ_arena.config import LLMSpec
from civ_arena.strategy.store import StrategyStore
from civ_arena.v2.contracts import (
    PLAYER_ACTION_KINDS_V2,
    ActionKindV2,
    ArtifactRefV2,
    ComputeConfigV2,
    EpisodeTerminationV2,
    EventTypeV2,
    ExecutionModeV2,
    PolicyDescriptorV2,
    PolicyKindV2,
    PolicyRuntimeKindV2,
    PolicySpendV2,
    PolicyStateV2,
    TurnContextV2,
    TurnProposalV2,
    TurnTerminationV2,
)
from civ_arena.v2.enumeration import ActionEnumeratorV2
from civ_arena.v2.environment import simulator_facets_v2
from civ_arena.v2.executor import TransactionalExecutorV2
from civ_arena.v2.graph import ActionGraphCompilerV2
from civ_arena.v2.ledger import EpisodeRecorderV2, ObjectStoreV2, load_events_v2
from civ_arena.v2.policy import (
    ALL_ACTION_KINDS_V2,
    LegacyPolicyRuntimeV2,
    PolicyServicesV2,
    ReplayPolicyRuntimeV2,
    build_policy_runtime_v2,
    policy_descriptor_v2,
    restore_policy_history_v2,
    snapshot_policy_state_v2,
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


def _bind_context_policy(
    context: TurnContextV2,
    descriptor: PolicyDescriptorV2,
) -> TurnContextV2:
    return TurnContextV2(
        context.observation,
        context.legal_actions,
        context.graph,
        descriptor,
    )


def test_policy_descriptor_binds_loaded_case_base_bytes() -> None:
    profile = AgentProfile(
        "planner-with-cases",
        0,
        "planner",
        13,
        case_base=SimpleNamespace(path="cases.json"),
    )
    left = policy_descriptor_v2(
        profile,
        runtime=SimpleNamespace(
            case_base=SimpleNamespace(artifact_sha256="a" * 64)
        ),
    )
    right = policy_descriptor_v2(
        profile,
        runtime=SimpleNamespace(
            case_base=SimpleNamespace(artifact_sha256="b" * 64)
        ),
    )
    assert left.artifact_digest != right.artifact_digest
    assert left.descriptor_id != right.descriptor_id


def test_policy_descriptor_binds_planner_execution_controls() -> None:
    profile = AgentProfile("planner-controls", 0, "planner", 13)
    base = SimpleNamespace(
        method="mcts",
        budget=16,
        weights=None,
        bandit=None,
        case_base=None,
    )
    weighted = SimpleNamespace(
        method="mcts",
        budget=16,
        weights={"cities": 111, "units": -2},
        bandit=None,
        case_base=None,
    )
    smaller = SimpleNamespace(
        method="mcts",
        budget=4,
        weights=None,
        bandit=None,
        case_base=None,
    )

    descriptors = {
        policy_descriptor_v2(profile, runtime=runtime).descriptor_id
        for runtime in (base, weighted, smaller)
    }
    assert len(descriptors) == 3


def test_resume_restores_durable_provider_attempts_beyond_last_state() -> None:
    llm = LLMSpec(
        base_url="http://fake.local/anthropic/v1",
        api_key_env="NEVER_READ",
        model_id="fake-model",
    )
    profile = AgentProfile("llm-resume", 0, "llm", 19, llm=llm)
    services = PolicyServicesV2(profile.agent_id, profile.player_id)
    legacy = SimpleNamespace(client=SimpleNamespace(posts_sent=0))
    runtime = LegacyPolicyRuntimeV2(
        legacy,
        policy_descriptor_v2(profile, runtime=legacy),
        services,
    )
    state = PolicyStateV2.create(
        policy_id=runtime.descriptor.descriptor_id,
        agent_id=profile.agent_id,
        player_id=profile.player_id,
        turn=2,
        proposal_id=None,
        runtime_kind=PolicyRuntimeKindV2.LLM,
        rng_state=None,
        runtime_state={"client_posts": 1},
        operations=[],
        telemetry={},
        model_posts=1,
    )

    restore_policy_history_v2(runtime, services, (state,), spend_attempts=3)

    assert legacy.client.posts_sent == 3
    assert services.model_posts == 3
    with pytest.raises(ContractError, match="attempts absent from the ledger"):
        restore_policy_history_v2(runtime, services, (state,), spend_attempts=0)


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
    context = _bind_context_policy(context, runtime.descriptor)
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
    context = _bind_context_policy(context, runtime.descriptor)
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
    )
    runtime = build_policy_runtime_v2(profile, services=services, runtime=legacy)
    context = _bind_context_policy(context, runtime.descriptor)
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


@pytest.mark.asyncio
async def test_provider_attempts_and_policy_state_enter_the_v2_chain(
    tmp_path: Path,
) -> None:
    llm = LLMSpec(
        base_url="http://fake.local/anthropic/v1",
        api_key_env="NEVER_READ",
        model_id="fake-model",
    )
    profile = AgentProfile("llm-custody", 0, "llm", 31, llm=llm)
    environment, _monitor, context, _descriptor = await _turn_context(profile)
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
    )
    runtime = build_policy_runtime_v2(profile, services=services, runtime=legacy)
    context = _bind_context_policy(context, runtime.descriptor)
    recorder = EpisodeRecorderV2(
        tmp_path,
        episode_id="llm-custody",
        environment=environment.descriptor,
        policies=[runtime.descriptor],
        seed=81,
        compute=ComputeConfigV2(ExecutionModeV2.DAG_TX, 1024, 2),
        scored=False,
    )
    services.bind_custody(runtime.descriptor.descriptor_id, recorder)
    proposal = await runtime.propose_turn(context)
    state = snapshot_policy_state_v2(
        runtime,
        services,
        turn=1,
        proposal_id=proposal.proposal_id,
    )
    recorder.record_reference(
        EventTypeV2.POLICY_STATE_RECORDED,
        state,
        turn_id=1,
        correlation_id="turn-1-p0",
    )
    services.mark_operations_durable()
    services.unbind_custody()
    recorder.terminate(EpisodeTerminationV2.FAILURE, turns_completed=0)
    recorder.close()

    events = load_events_v2(tmp_path / "events.jsonl")
    spend_refs = [
        event.payload_value
        for event in events
        if event.event_type is EventTypeV2.POLICY_SPEND_RECORDED
    ]
    assert len(spend_refs) == 2
    store = ObjectStoreV2(tmp_path)
    spends = [
        PolicySpendV2.from_doc(store.read_doc(ref))
        for ref in spend_refs
        if isinstance(ref, ArtifactRefV2)
    ]
    assert [item.attempt for item in spends] == [1, 2]
    state_ref = next(
        event.payload_value
        for event in events
        if event.event_type is EventTypeV2.POLICY_STATE_RECORDED
    )
    assert isinstance(state_ref, ArtifactRefV2)
    persisted = PolicyStateV2.from_doc(store.read_doc(state_ref))
    assert persisted.model_posts == 2
    assert persisted.runtime_state["client_posts"] == 2


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
        registered_action_kinds=PLAYER_ACTION_KINDS_V2,
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
        registered_action_kinds=PLAYER_ACTION_KINDS_V2,
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
    assert first.registered_action_kinds == PLAYER_ACTION_KINDS_V2
    assert "key" not in str(first.to_doc()).lower()
