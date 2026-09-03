from __future__ import annotations

import copy
import hashlib
from pathlib import Path
from typing import Any

import pytest

from civ_arena.arena.referee import MatchAborted
from civ_arena.config import (
    AgentSpec,
    ChaosSpec,
    ConfigError,
    MatchSpec,
    load_config,
    parse_config,
)
from civ_arena.game.civ6.fake_tuner_server import FakeMod, FakeTunerServer
from civ_arena.game.civ6.firetuner import FireTunerAdapter
from civ_arena.recall import RecallCorpus
from civ_arena.v2.arena import ArenaV2
from civ_arena.v2.contracts import (
    ActionGraphV2,
    ActionKindV2,
    ArtifactRefV2,
    EpisodeConfigV2,
    EpisodeReceiptV2,
    EpisodeTerminationV2,
    EventTypeV2,
    PolicyStateV2,
    TurnReceiptV2,
    TurnTerminationV2,
)
from civ_arena.v2.environment import firetuner_facets_v2, simulator_facets_v2
from civ_arena.v2.ledger import ObjectStoreV2, load_events_v2, verify_ledger_v2
from civ_arena.v2.policy import SystemHousekeepingRuntimeV2
from civ_arena.v2.replay import replay_fake_episode_v2
from civ_arena.v2.schemas import ContractError


def _spec(
    match_id: str,
    *,
    policy: str = "turtler",
    max_turns: int = 1,
    seats: int = 1,
    chaos: list[Any] | None = None,
    scored: bool = False,
) -> MatchSpec:
    return MatchSpec(
        match_id=match_id,
        seed=90210,
        max_turns=max_turns,
        adapter="simulator",
        watchdog_mode="flag_and_continue",
        violation_limit=5,
        checkpoint_every=1,
        agents=[
            AgentSpec(f"seat-{player_id}", player_id, policy, 11 + player_id)
            for player_id in range(seats)
        ],
        chaos=chaos or [],
        schema=2,
        execution_mode="dag_tx",
        scored=scored,
        max_graph_actions=1024,
        max_replans_per_turn=2,
    )


class _OneTurnPolicy:
    async def take_turn(self, facade: Any) -> None:
        await facade.write_diary("custodied V2 memory")
        await facade.record_lesson("hold the observable river crossing")
        choices = await facade.get_available_research()
        if choices:
            await facade.set_research(choices[0]["tech_id"])
        await facade.end_turn()


@pytest.mark.asyncio
async def test_arena_v2_writes_only_v2_trust_root_and_exactly_replays(
    tmp_path: Path,
) -> None:
    run = tmp_path / "v2-one-turn"
    summary = await ArenaV2(
        run,
        _spec("v2-one-turn"),
        runtimes={0: _OneTurnPolicy()},
    ).run()

    assert summary["termination_reason"] == "success"
    assert summary["phases_completed"] == 1
    assert summary["violations_total"] == 0
    assert "final_state_hash" not in summary
    assert "scores" not in summary
    assert not (run / "summary.json").exists()
    assert not (run / "spend.jsonl").exists()
    assert not (run / "checkpoints").exists()
    raw = (run / "events.jsonl").read_text(encoding="utf-8")
    assert '"schema":2' in raw
    assert '"schema":1' not in raw
    assert "private_state" not in raw
    events = load_events_v2(run / "events.jsonl")
    assert events[0].event_type is EventTypeV2.EPISODE_STARTED
    assert events[1].event_type is EventTypeV2.EPISODE_CONFIG_RECORDED
    assert events[-1].event_type is EventTypeV2.EPISODE_TERMINATED
    config_ref = events[1].payload_value
    assert isinstance(config_ref, ArtifactRefV2)
    episode_config = EpisodeConfigV2.from_doc(ObjectStoreV2(run).read_doc(config_ref))
    assert episode_config.chaos == ()
    assert episode_config.violation_limit == 5
    state_events = [
        event for event in events if event.event_type is EventTypeV2.POLICY_STATE_RECORDED
    ]
    assert len(state_events) == 1
    state_ref = state_events[0].payload_value
    assert isinstance(state_ref, ArtifactRefV2)
    state = PolicyStateV2.from_doc(ObjectStoreV2(run).read_doc(state_ref))
    assert state.proposal_id is not None
    assert [operation.tool for operation in state.operations] == [
        "write_diary",
        "record_lesson",
    ]
    assert state.operations[0].args == {"text": "custodied V2 memory"}
    terminal = events[-1].payload_value
    assert isinstance(terminal, EpisodeReceiptV2)
    assert terminal.turns_completed == 1
    assert terminal.scored is False
    verified = verify_ledger_v2(run)
    assert verified.termination_reason == "success"
    replayed = await replay_fake_episode_v2(run)
    assert replayed.turn_receipt_count == 1
    assert replayed.action_count == 2


@pytest.mark.asyncio
async def test_prepared_firetuner_phase_receipts_system_housekeeping_before_policy(
    tmp_path: Path,
) -> None:
    mod = FakeMod()
    mod.pending_blockers = [
        "BLOCKING|ENDTURN_BLOCKING_FILL_CIVIC_SLOT",
        "BLOCKING|ENDTURN_BLOCKING_CIVIC",
    ]
    server = FakeTunerServer(mod=mod)
    port = await server.start()
    adapter = FireTunerAdapter(
        "127.0.0.1",
        port,
        simulate_hook=lambda event, player_id, turn=0: {
            "turn_start": f"Simulate.TurnStartAt({player_id}, {turn})",
            "turn_deactivated": f"Simulate.TurnDeactivated({player_id})",
            "advance_turn": "Simulate.AdvanceTurn()",
        }[event],
        poll_timeout_s=2.0,
    )
    environment, monitor = firetuner_facets_v2(
        adapter,
        adapter_version="prepared-firetuner-fixture-1",
        game_version="civ6-prepared-fixture-1",
        ruleset_digest="c" * 64,
        mod_digest="d" * 64,
    )
    spec = _spec("prepared-firetuner")
    spec.adapter = "firetuner"

    async def phases():
        adapter.expect_turn(1)
        yield 1, 0

    run = tmp_path / "prepared-firetuner"
    try:
        await adapter.setup({})
        summary = await ArenaV2(
            run,
            spec,
            environment=environment,
            private_monitor=monitor,
            system_runtime=SystemHousekeepingRuntimeV2(),
        ).run_prepared(phases(), expected_phases=1)
    finally:
        await adapter.teardown()
        await server.stop()

    assert summary["termination_reason"] == "success"
    events = load_events_v2(run / "events.jsonl")
    system_receipts = [
        event.payload_value
        for event in events
        if event.event_type is EventTypeV2.TURN_COMPLETED
        and isinstance(event.payload_value, TurnReceiptV2)
        and event.payload_value.termination is TurnTerminationV2.SYSTEM_HANDOFF
    ]
    assert len(system_receipts) == 2

    store = ObjectStoreV2(run)
    actions = {}
    for event in events:
        if event.event_type is not EventTypeV2.ACTION_GRAPH_COMPILED:
            continue
        assert isinstance(event.payload_value, ArtifactRefV2)
        graph = ActionGraphV2.from_doc(store.read_doc(event.payload_value))
        actions.update({node.action.action_id: node.action for node in graph.nodes})
    assert {
        actions[receipt.results[-1].action_id].action_kind
        for receipt in system_receipts
    } == {ActionKindV2.FILL_POLICY_SLOTS, ActionKindV2.RESOLVE_CIVIC}
    first_player_receipt = next(
        event.payload_value
        for event in events
        if event.event_type is EventTypeV2.TURN_COMPLETED
        and isinstance(event.payload_value, TurnReceiptV2)
        and event.payload_value.termination is not TurnTerminationV2.SYSTEM_HANDOFF
    )
    assert events.index(
        next(
            event
            for event in events
            if event.event_type is EventTypeV2.TURN_COMPLETED
            and event.payload_value == system_receipts[-1]
        )
    ) < events.index(
        next(
            event
            for event in events
            if event.event_type is EventTypeV2.TURN_COMPLETED
            and event.payload_value == first_player_receipt
        )
    )
    assert mod.pending_blockers == []
    assert verify_ledger_v2(run).termination_reason == "success"
    assert not (run / "summary.json").exists()


@pytest.mark.asyncio
async def test_arena_v2_custodies_chaos_for_exact_replay(tmp_path: Path) -> None:
    run = tmp_path / "v2-chaos"
    environment, monitor = simulator_facets_v2()
    summary = await ArenaV2(
        run,
        _spec(
            "v2-chaos",
            chaos=[ChaosSpec("steal_gold", hook="act", offset=0)],
        ),
        runtimes={0: _OneTurnPolicy()},
        environment=environment,
        private_monitor=monitor,
    ).run()

    assert summary["termination_reason"] == "success"
    assert summary["violations_total"] == 1
    replayed = await replay_fake_episode_v2(run)
    assert replayed.final_private_state_hash == monitor.state_hash()


@pytest.mark.asyncio
async def test_arena_v2_reproposes_for_newly_observable_mandatory_choice(
    tmp_path: Path,
) -> None:
    run = tmp_path / "planner-replan"
    summary = await ArenaV2(run, _spec("planner-replan", policy="planner")).run()

    assert summary["termination_reason"] == "success"
    # Initial founding reveals an idle city; the second proposal supplies its
    # production choice. Both attempts are receipted, one phase completes.
    assert len(summary["turn_receipts"]) >= 2
    events = load_events_v2(run / "events.jsonl")
    terminations = [
        event.payload_value.termination.value
        for event in events
        if event.event_type is EventTypeV2.TURN_COMPLETED
    ]
    assert "mandatory_unresolved" in terminations
    assert terminations[-1] == "completed"
    replayed = await replay_fake_episode_v2(run)
    assert replayed.turn_receipt_count == len(terminations)


class _CrashingPolicy:
    async def take_turn(self, facade: Any) -> None:
        _ = facade
        raise MatchAborted("authorization: SUPER-SECRET")


@pytest.mark.asyncio
async def test_policy_failure_gets_terminal_safe_receipt(tmp_path: Path) -> None:
    run = tmp_path / "failed"
    summary = await ArenaV2(
        run,
        _spec("failed"),
        runtimes={0: _CrashingPolicy()},
    ).run()
    assert summary["termination_reason"] == "failure"
    assert summary["aborted"] == "policy runtime aborted"
    raw = (run / "events.jsonl").read_text(encoding="utf-8")
    assert "SUPER-SECRET" not in raw
    assert verify_ledger_v2(run).termination_reason == "failure"


def test_file_configs_require_strict_v2_controls(tmp_path: Path) -> None:
    legacy = tmp_path / "legacy.yaml"
    legacy.write_text(
        "match:\n  match_id: old\n  seed: 1\nagents:\n"
        "  - {agent_id: a, player_id: 0, policy: turtler}\n",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="schema: 2"):
        load_config(legacy)

    base = {
        "schema": 2,
        "match": {
            "match_id": "strict",
            "seed": 1,
            "execution_mode": "dag_tx",
            "scored": False,
            "max_graph_actions": 1024,
            "max_replans_per_turn": 2,
        },
        "agents": [
            {"agent_id": "a", "player_id": 0, "policy": "turtler"}
        ],
    }
    assert parse_config(base, require_v2=True).schema == 2
    for field in (
        "execution_mode",
        "scored",
        "max_graph_actions",
        "max_replans_per_turn",
    ):
        broken = {
            **base,
            "match": {key: value for key, value in base["match"].items() if key != field},
        }
        with pytest.raises(ConfigError, match=field):
            parse_config(broken, require_v2=True)

    control = {**base, "match": {**base["match"], "execution_mode": "sequential"}}
    with pytest.raises(ConfigError, match="experiment-harness only"):
        parse_config(control, require_v2=True)

    hidden_match = copy.deepcopy(base)
    hidden_match["match"]["private_state"] = {"rival_rng": 7}
    with pytest.raises(ConfigError, match="match: unknown V2 keys"):
        parse_config(hidden_match, require_v2=True)

    hidden_agent = copy.deepcopy(base)
    hidden_agent["agents"][0]["referee"] = "borrowed"
    with pytest.raises(ConfigError, match=r"agents\[0\]: unknown V2 keys"):
        parse_config(hidden_agent, require_v2=True)

    for chaos, message in (
        ({"spec": "teleport_opponent"}, "registered mutation"),
        ({"spec": "steal_gold", "hook": "between_turns"}, "registered hook"),
        ({"spec": "steal_gold", "offset": True}, "non-negative integer"),
    ):
        broken_chaos = copy.deepcopy(base)
        broken_chaos["chaos"] = [chaos]
        with pytest.raises(ConfigError, match=message):
            parse_config(broken_chaos, require_v2=True)

    llm_agent = copy.deepcopy(base)
    llm_agent["agents"] = [
        {
            "agent_id": "llm-seat",
            "player_id": 0,
            "policy": "llm",
            "llm": {
                "base_url": "https://user:secret@example.test/v1?api_key=hidden",
                "api_key_env": "MODEL_KEY",
                "model_id": "model",
            },
        }
    ]
    with pytest.raises(ConfigError, match="credential-free"):
        parse_config(llm_agent, require_v2=True)

    for field, value, message in (
        ("match_id", "../outside", "bare V2 episode id"),
        ("seed", True, "seed must be an integer"),
        ("max_turns", "40", "max_turns must be an integer"),
        ("violation_limit", -1, "violation_limit must be >= 0"),
    ):
        bad_scalar = copy.deepcopy(base)
        bad_scalar["match"][field] = value
        with pytest.raises(ConfigError, match=message):
            parse_config(bad_scalar, require_v2=True)

    bad_player = copy.deepcopy(base)
    bad_player["agents"][0]["player_id"] = True
    with pytest.raises(ConfigError, match="non-negative integer"):
        parse_config(bad_player, require_v2=True)


def test_every_checked_in_yaml_config_is_v2_dag_tx() -> None:
    for path in sorted(Path("configs").glob("*.yaml")):
        spec = load_config(path)
        assert spec.schema == 2, path
        assert spec.execution_mode == "dag_tx", path
        assert spec.scored is False, path
        assert spec.max_graph_actions == 1024, path
        assert spec.max_replans_per_turn == 2, path


def test_scored_v2_resume_is_structurally_forbidden(tmp_path: Path) -> None:
    with pytest.raises(ContractError, match="cannot resume"):
        ArenaV2(
            tmp_path / "child",
            _spec("scored", scored=True),
            parent_episode_id="parent",
            parent_terminal_event_hash="a" * 64,
        )


@pytest.mark.asyncio
async def test_non_scored_resume_creates_child_without_rewriting_parent(
    tmp_path: Path,
) -> None:
    parent = tmp_path / "resume-v2"
    parent_env, parent_monitor = simulator_facets_v2()
    parent_arena = ArenaV2(
        parent,
        _spec("resume-v2", max_turns=1, seats=2),
        runtimes={0: _OneTurnPolicy(), 1: _OneTurnPolicy()},
        environment=parent_env,
        private_monitor=parent_monitor,
    )
    parent_summary = await parent_arena.run()
    parent_bytes = (parent / "events.jsonl").read_bytes()
    parent_digest = hashlib.sha256(parent_bytes).hexdigest()

    mismatch_env, mismatch_monitor = simulator_facets_v2()
    with pytest.raises(ContractError, match="episode config does not match"):
        ArenaV2(
            tmp_path / "resume-v2-mismatch",
            _spec(
                "resume-v2",
                max_turns=2,
                seats=2,
                chaos=[ChaosSpec("steal_gold")],
            ),
            runtimes={0: _OneTurnPolicy(), 1: _OneTurnPolicy()},
            environment=mismatch_env,
            private_monitor=mismatch_monitor,
            parent_episode_dir=parent,
        )

    child_env, child_monitor = simulator_facets_v2()
    child_id = f"resume-v2-child-{parent_summary['terminal_event_hash'][:12]}"
    child = tmp_path / child_id
    child_arena = ArenaV2(
        child,
        _spec("resume-v2", max_turns=2, seats=2),
        runtimes={0: _OneTurnPolicy(), 1: _OneTurnPolicy()},
        environment=child_env,
        private_monitor=child_monitor,
        parent_episode_dir=parent,
    )
    child_summary = await child_arena.run()

    clean_env, clean_monitor = simulator_facets_v2()
    clean_arena = ArenaV2(
        tmp_path / "clean-v2",
        _spec("clean-v2", max_turns=2, seats=2),
        runtimes={0: _OneTurnPolicy(), 1: _OneTurnPolicy()},
        environment=clean_env,
        private_monitor=clean_monitor,
    )
    clean_summary = await clean_arena.run()

    assert parent_summary["termination_reason"] == "success"
    assert child_summary["termination_reason"] == "success"
    assert clean_summary["termination_reason"] == "success"
    assert child_summary["phases_completed"] == 2
    assert child_summary["final_turn"] == 2
    assert child_monitor.state_hash() == clean_monitor.state_hash()
    assert child_arena.diary.get(0) == clean_arena.diary.get(0) == "custodied V2 memory"
    assert child_arena.services[0].operations == clean_arena.services[0].operations
    assert hashlib.sha256((parent / "events.jsonl").read_bytes()).hexdigest() == parent_digest
    terminal = load_events_v2(child / "events.jsonl")[-1].payload_value
    assert isinstance(terminal, EpisodeReceiptV2)
    assert terminal.parent_episode_id == parent_summary["episode_id"]
    assert terminal.parent_terminal_event_hash == parent_summary["terminal_event_hash"]
    assert terminal.turns_completed == 2
    replayed = await replay_fake_episode_v2(child)
    assert replayed.final_private_state_hash == child_monitor.state_hash()
    recall = RecallCorpus.from_runs(tmp_path, "future-match", [child_id])
    assert recall.size("seat-0") == 2
    assert len(recall.query("seat-0", "river observable")) == 2


@pytest.mark.asyncio
async def test_recovered_crash_receipt_seeds_immutable_child(
    tmp_path: Path,
) -> None:
    parent = tmp_path / "recovered-parent"
    parent_env, parent_monitor = simulator_facets_v2()
    parent_summary = await ArenaV2(
        parent,
        _spec("recovered-parent", max_turns=2, seats=2),
        runtimes={0: _OneTurnPolicy(), 1: _OneTurnPolicy()},
        environment=parent_env,
        private_monitor=parent_monitor,
    ).run(recover_after_turn=1)

    assert parent_summary["termination_reason"] == "recovered_crash"
    assert parent_summary["final_turn"] == 1
    assert parent_summary["phases_completed"] == 2
    parent_bytes = (parent / "events.jsonl").read_bytes()
    parent_terminal = load_events_v2(parent / "events.jsonl")[-1].payload_value
    assert isinstance(parent_terminal, EpisodeReceiptV2)
    assert (
        parent_terminal.termination_reason
        is EpisodeTerminationV2.RECOVERED_CRASH
    )
    assert parent_terminal.turns_completed == 2
    verified = verify_ledger_v2(parent)
    assert verified.termination_reason == "recovered_crash"

    child_env, child_monitor = simulator_facets_v2()
    child = tmp_path / "recovered-child"
    child_summary = await ArenaV2(
        child,
        _spec("recovered-parent", max_turns=2, seats=2),
        runtimes={0: _OneTurnPolicy(), 1: _OneTurnPolicy()},
        environment=child_env,
        private_monitor=child_monitor,
        parent_episode_dir=parent,
        episode_id="recovered-child",
    ).run()

    assert child_summary["termination_reason"] == "success"
    assert child_summary["phases_completed"] == 2
    assert child_summary["parent_episode_id"] == "recovered-parent"
    assert (
        child_summary["parent_terminal_event_hash"]
        == verified.terminal_event_hash
    )
    assert (parent / "events.jsonl").read_bytes() == parent_bytes


@pytest.mark.asyncio
async def test_recovered_crash_injection_refuses_unsafe_scopes(tmp_path: Path) -> None:
    for invalid in (0, 3, True):
        with pytest.raises(ContractError, match="configured horizon"):
            await ArenaV2(
                tmp_path / f"invalid-{invalid}",
                _spec(f"invalid-{invalid}", max_turns=2),
            ).run(recover_after_turn=invalid)

    with pytest.raises(ContractError, match="scored"):
        await ArenaV2(
            tmp_path / "scored-crash",
            _spec("scored-crash", max_turns=2, scored=True),
        ).run(recover_after_turn=1)

    parent = tmp_path / "nested-parent"
    await ArenaV2(
        parent,
        _spec("nested-parent", max_turns=1),
    ).run()
    child_env, child_monitor = simulator_facets_v2()
    child_arena = ArenaV2(
        tmp_path / "nested-child",
        _spec("nested-parent", max_turns=2),
        environment=child_env,
        private_monitor=child_monitor,
        parent_episode_dir=parent,
    )
    with pytest.raises(ContractError, match="child cannot inject"):
        await child_arena.run(recover_after_turn=1)
