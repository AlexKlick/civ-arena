from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from civ_arena.arena.referee import MatchAborted
from civ_arena.config import AgentSpec, ConfigError, MatchSpec, load_config, parse_config
from civ_arena.v2.arena import ArenaV2
from civ_arena.v2.contracts import EpisodeReceiptV2, EventTypeV2
from civ_arena.v2.ledger import load_events_v2, verify_ledger_v2
from civ_arena.v2.replay import replay_fake_episode_v2
from civ_arena.v2.schemas import ContractError


def _spec(
    match_id: str,
    *,
    policy: str = "turtler",
    max_turns: int = 1,
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
        agents=[AgentSpec("seat-zero", 0, policy, 11)],
        chaos=chaos or [],
        schema=2,
        execution_mode="dag_tx",
        scored=scored,
        max_graph_actions=1024,
        max_replans_per_turn=2,
    )


class _OneTurnPolicy:
    async def take_turn(self, facade: Any) -> None:
        choices = await facade.get_available_research()
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
    assert events[-1].event_type is EventTypeV2.EPISODE_TERMINATED
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
