"""Adaptive doctrine switcher: pure resolution, config validation, e2e switch."""

from __future__ import annotations

import json

import pytest

from civ_arena.agents.adaptive import (
    AdaptiveRuntime,
    AdaptiveSpec,
    AdaptiveTrigger,
    observations,
    resolve,
)
from civ_arena.agents.runtime import AgentProfile, build_runtime
from civ_arena.arena.coordinator import Arena
from civ_arena.config import ConfigError, parse_config

SPEC = AdaptiveSpec(
    initial="turtler",
    interval=5,
    triggers=(
        AdaptiveTrigger(when={"turn_gte": 3, "own_cities_gte": 1},
                        switch_to="expansionist"),
        AdaptiveTrigger(when={"min_foreign_units_seen": 2},
                        switch_to="turtler"),
    ),
)


def _overview(turn: int, gold: int = 100) -> dict:
    return {"turn": turn, "you": {"gold": gold, "researched": ["MINING"]}}


def _units(n_own: int, n_foreign: int) -> list[dict]:
    return ([{"unit_id": f"u{i}", "owner_id": 0, "type": "WARRIOR",
              "hp": 20, "max_hp": 20} for i in range(n_own)]
            + [{"unit_id": f"f{i}", "owner_id": 1, "type": "SCOUT"}
               for i in range(n_foreign)])


def _cities(n_own: int, n_foreign: int) -> list[dict]:
    # own projections keep the state ``owner`` key; foreign city
    # projections carry ``owner_id`` (visibility.py FOREIGN_CITY_FIELDS)
    return ([{"city_id": f"c{i}", "owner": 0, "population": 2}
             for i in range(n_own)]
            + [{"city_id": f"fc{i}", "owner_id": 1, "population": 1}
               for i in range(n_foreign)])


def test_resolve_first_match_wins_then_initial() -> None:
    # turn 1: nothing fires (turn_gte 3 unmet) -> initial
    assert resolve(SPEC, 1, _overview(1), _units(0, 0), _cities(0, 0), 0) == (
        "turtler", "initial")
    # turn 5 with a city: trigger 0 fires before trigger 1 is consulted
    doctrine, reason = resolve(SPEC, 5, _overview(5), _units(1, 3),
                               _cities(1, 0), 0)
    assert doctrine == "expansionist"
    assert "turn_gte=3" in reason and "own_cities_gte=1" in reason
    # trigger 1 alone: foreign pressure at turn 1 -> turtler via trigger
    doctrine, reason = resolve(
        AdaptiveSpec(initial="expansionist", interval=5,
                     triggers=(SPEC.triggers[1],)),
        1, _overview(1), _units(0, 2), _cities(0, 0), 0)
    assert doctrine == "turtler" and "min_foreign_units_seen=2" in reason


def test_observations_owner_field_duality_and_hp_frac() -> None:
    obs = observations(7, _overview(7, gold=55), _units(2, 3),
                       _cities(2, 1), 0)
    assert obs["own_units"] == 2 and obs["foreign_units_seen"] == 3
    assert obs["own_cities"] == 2 and obs["foreign_cities_seen"] == 1
    assert obs["own_gold"] == 55 and obs["own_techs"] == 1
    assert obs["own_population"] == 4
    wounded = [{"unit_id": "u0", "owner_id": 0, "type": "WARRIOR",
                "hp": 5, "max_hp": 20}]
    assert observations(1, _overview(1), wounded, [], 0)["own_units_hp_frac"] == 0.25
    # no max_hp reported -> 1.0 (can never fire the trigger)
    no_hp = [{"unit_id": "u0", "owner_id": 0, "type": "WARRIOR", "hp": 1}]
    assert observations(1, _overview(1), no_hp, [], 0)["own_units_hp_frac"] == 1.0


def test_config_adaptive_validation() -> None:
    def doc(agent: dict) -> dict:
        return {"match": {"match_id": "m", "seed": 1}, "agents": [agent]}

    ok = doc({"agent_id": "a", "player_id": 0, "policy": "adaptive",
              "adaptive": {
                  "initial": "turtler", "interval": 5,
                  "triggers": [{"when": {"turn_gte": 3},
                                "switch_to": "expansionist"}],
              }})
    spec = parse_config(ok)
    assert spec.agents[0].adaptive.initial == "turtler"
    assert spec.agents[0].adaptive.interval == 5

    no_block = doc({"agent_id": "a", "player_id": 0, "policy": "adaptive"})
    with pytest.raises(ConfigError):
        parse_config(no_block)
    block_elsewhere = doc({"agent_id": "a", "player_id": 0,
                           "policy": "turtler",
                           "adaptive": {"initial": "turtler", "triggers": [
                               {"when": {"turn_gte": 3},
                                "switch_to": "turtler"}]}})
    with pytest.raises(ConfigError):
        parse_config(block_elsewhere)
    bad_predicate = doc({"agent_id": "a", "player_id": 0,
                         "policy": "adaptive",
                         "adaptive": {"initial": "turtler", "triggers": [
                             {"when": {"won_last_war": 1},
                              "switch_to": "turtler"}]}})
    with pytest.raises(ConfigError):
        parse_config(bad_predicate)
    bad_doctrine = doc({"agent_id": "a", "player_id": 0,
                        "policy": "adaptive",
                        "adaptive": {"initial": "knight_rush", "triggers": [
                            {"when": {"turn_gte": 3},
                             "switch_to": "turtler"}]}})
    with pytest.raises(ConfigError):
        parse_config(bad_doctrine)


async def test_adaptive_switches_mid_match(tmp_path) -> None:
    doc = {
        "match": {"match_id": "adaptive-e2e", "seed": 424242, "max_turns": 8,
                  "adapter": "simulator", "checkpoint_every": 5,
                  "player_count": 2},
        "agents": [
            {"agent_id": "switcher", "player_id": 0, "policy": "adaptive",
             "seed": 11,
             "adaptive": {
                 "initial": "turtler", "interval": 5,
                 "triggers": [{"when": {"turn_gte": 3, "own_cities_gte": 1},
                               "switch_to": "expansionist"}],
             }},
            {"agent_id": "korea", "player_id": 1, "policy": "turtler",
             "seed": 22},
        ],
    }
    spec = parse_config(doc)
    arena = Arena(tmp_path / "run", spec)
    doctrine_by_turn: dict[int, str | None] = {}

    def on_turn_end(turn: int, scores) -> None:
        rt = arena.runtimes[0]
        doctrine_by_turn[turn] = getattr(rt, "current_doctrine", None)

    summary = await arena.run(on_turn_end=on_turn_end)
    assert summary["violations_total"] == 0 and summary["aborted"] is None
    # turtler through turn 4, expansionist from the turn-5 re-resolution
    assert doctrine_by_turn[4] == "turtler"
    assert doctrine_by_turn[5] == "expansionist"
    assert doctrine_by_turn[8] == "expansionist"
    # the switch is announced in the diary channel
    diary_calls = [
        rec for rec in arena.log.records()
        if rec["kind"] == "TOOL_CALL" and rec.get("tool") == "write_diary"
        and "ADAPTIVE:" in str(rec.get("args"))
    ]
    assert diary_calls, "the doctrine switch must be announced via write_diary"

    # the runtime builds from a profile and keeps the rng contract
    rt = build_runtime(AgentProfile(
        agent_id="s", player_id=0, policy="adaptive", seed=11,
        adaptive=parse_config(doc).agents[0].adaptive))
    assert isinstance(rt, AdaptiveRuntime) and rt.rng is not None


def test_adaptive_events_annotation_roundtrip(tmp_path) -> None:
    doc = {"match": {"match_id": "m", "seed": 1}, "agents": [
        {"agent_id": "a", "player_id": 0, "policy": "adaptive",
         "adaptive": {"initial": "turtler", "triggers": [
             {"when": {"turn_lte": 99}, "switch_to": "expansionist"}]}}]}
    spec = parse_config(doc)
    dumped = json.dumps(spec.agents[0].adaptive.triggers[0].when)
    assert json.loads(dumped) == {"turn_lte": 99}
