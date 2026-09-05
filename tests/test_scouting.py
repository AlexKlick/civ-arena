"""Executable heuristic/controller boundaries; no provider or live engine needed."""
from __future__ import annotations

import copy
import json
from unittest.mock import AsyncMock

import pytest

from civ_arena.agents.scouting import MAX_UNITS, plan_scouting, run_scouting
from civ_arena.game.adapter import ActionCommand
from civ_arena.game.civ6.firetuner import FireTunerAdapter
from civ_arena.game.sim.state import hex_dist, tiles_within

IDENTITY = {"player_id": 0, "match_id": "fixture", "agent_id": "seat0", "turn": 1, "seed": 7}


def unit(uid="u0:131073", *, coord="0,0", owner=0, kind="SCOUT", movement=2):
    return {"unit_id": uid, "owner_id": owner, "coord": coord, "type": kind,
            "movement": movement, "fortified": False, "strength": 10}


def snapshot(units=None, radius=2):
    return {"get_units": [unit()] if units is None else units,
            "get_cities": [], "get_visible_map": {"turn": 1, "tiles": {
                f"{q},{r}": {"terrain": "PLAINS", "owner_id": -1, "city_id": None}
                for q, r in tiles_within((0, 0), radius)}}}


def plan(state=None, directive=None, **kwargs):
    return plan_scouting(snapshot() if state is None else state,
                         directive={} if directive is None else directive,
                         **{**IDENTITY, **kwargs})


def test_seeded_plan_is_order_independent_json_and_records_exact_seed():
    state = snapshot([unit("u0:8", coord="1,0"), unit()])
    first = plan(state)
    reordered = copy.deepcopy(state)
    reordered["get_units"].reverse()
    reordered["get_visible_map"]["tiles"] = dict(reversed(list(
        reordered["get_visible_map"]["tiles"].items())))
    assert plan(reordered) == first
    assert json.loads(json.dumps(first, allow_nan=False)) == first
    assert first["identity"]["configured_seed"] == 7
    assert first["decisions"][0]["seed"] != plan(state, seed=8)["decisions"][0]["seed"]
    assert first["decisions"][0]["seed"] != plan(state, turn=2)["decisions"][0]["seed"]
    for decision in first["decisions"]:
        assert sum(c["probability"] for c in decision["candidates"]) == pytest.approx(1)
    assert "not_calibrated_success_or_engine_legality" in first["probability_meaning"]


def test_frontier_gain_and_distance_use_only_known_tiles():
    result = plan(snapshot(radius=1), {"scouting": {"selection": "best"}})
    candidates = result["decisions"][0]["candidates"]
    assert len(candidates) == 6
    assert all(c["components"]["unexplored_gain"] > 0 for c in candidates)
    assert sum(c["probability"] for c in candidates) == 1
    dest = result["decisions"][0]["selected"]["args"]["dest"]
    assert dest in snapshot(radius=1)["get_visible_map"]["tiles"]


def test_cautious_avoids_visible_foreign_units_and_does_not_attack():
    state = snapshot([unit(), unit("u1:131073", coord="2,0", owner=1)])
    result = plan(state, {"scouting": {"policy": "cautious", "selection": "best"}})
    decision = result["decisions"][0]
    for candidate in decision["candidates"]:
        q, r = map(int, candidate["dest"].split(","))
        if hex_dist((q, r), (2, 0)) <= 2:
            assert candidate["excluded"] == "observed_threat_proximity"
            assert candidate["probability"] == 0
    assert decision["selected"]["action"] == "move_unit"
    assert len(result["decisions"]) == 1


@pytest.mark.parametrize("terrain", ["OCEAN", "COAST", "MOUNTAIN", "UNKNOWN"])
def test_unsupported_or_nonland_tiles_are_not_assumed_walkable(terrain):
    state = snapshot()
    for tile in state["get_visible_map"]["tiles"].values():
        tile["terrain"] = terrain
    decision = plan(state)["decisions"][0]
    assert decision["selected"]["action"] == "fortify"
    assert all(row["probability"] == 0 for row in decision["candidates"])


def test_terrain_penalty_changes_score_without_claiming_engine_cost():
    state = snapshot(radius=1)
    state["get_visible_map"]["tiles"]["1,0"]["terrain"] = "HILL"
    rows = {row["dest"]: row for row in plan(state)["decisions"][0]["candidates"]}
    assert rows["1,0"]["score"] < rows["-1,0"]["score"]
    assert rows["1,0"]["components"]["terrain_penalty"] == 1


def test_settler_default_safe_hold_and_explicit_owned_found_override():
    state = snapshot([unit(kind="SETTLER")])
    decision = plan(state)["decisions"][0]
    assert decision["selected"]["action"] == "fortify"
    override = {"tactical_overrides": [{"unit_id": "u0:131073", "action": "found_city"}]}
    decision = plan(state, override)["decisions"][0]
    assert decision["selected"]["action"] == "found_city"
    assert decision["override"]


def test_hidden_target_and_unknown_or_distant_override_destination_refused():
    for order, reason in [
        ({"action": "attack", "target_id": "u1:131073"}, "override_target_not_observed"),
        ({"action": "move", "dest": "4,0"}, "override_destination_not_known_adjacent"),
    ]:
        decision = plan(directive={"tactical_overrides": [{"unit_id": "u0:131073", **order}]})[
            "decisions"][0]
        assert decision["selected"] is None
        assert decision["reason"].startswith(reason)


def test_tactical_attack_uses_only_owned_actor_and_exact_visible_foreign_identity():
    state = snapshot([unit(), unit("u1:131073", coord="1,0", owner=1)])
    override = {"tactical_overrides": [{"unit_id": "u0:131073", "action": "attack",
                                       "target_id": "u1:131073"}]}
    decision = plan(state, override)["decisions"][0]
    assert decision["selected"]["args"] == {"unit_id": "u0:131073", "target_id": "u1:131073"}
    assert decision["selected"]["action"] == "attack"
    with pytest.raises(ValueError):
        plan(state, {"tactical_overrides": [{"unit_id": "u1:131073", "action": "hold"}]})


@pytest.mark.parametrize("bad", [
    {"get_units": {"error": "no_lease"}}, {"get_visible_map": {"error": "no_lease"}},
    {"get_units": [unit(movement=float("nan"))]}, {"get_units": [unit(), unit()]},
])
def test_invalid_observations_are_not_empty_success(bad):
    with pytest.raises(ValueError):
        plan({**snapshot(), **bad})


class Harness:
    def __init__(self, state, effect="move", rejection="illegal_move"):
        self.state = state
        self.effect, self.rejection = effect, rejection
        self.calls, self.trace = [], []

    async def execute(self, name, args):
        self.calls.append((name, dict(args)))
        self.trace.append("execute")
        own = next(u for u in self.state["get_units"] if u["unit_id"] == args["unit_id"])
        if self.effect == "move" and name == "move_unit":
            own.update(coord=args["dest"], movement=0)
        if self.effect == "consumed":
            self.state["get_units"].remove(own)
        if self.effect in {"reject_refrozen", "reject_moved"}:
            own["movement"] = 0
        if self.effect == "reject_moved":
            own["coord"] = args["dest"]
        return {"status": "rejected", "rejection": self.rejection} if self.effect.startswith(
            "reject") else {"status": "accepted", "result": {"detail": "999,999"}}

    async def refresh(self):
        self.trace.append("refresh")
        return copy.deepcopy(self.state)

    async def run(self, **kwargs):
        return await run_scouting(copy.deepcopy(self.state), directive={}, **IDENTITY,
                                  execute=self.execute, refresh=self.refresh, **kwargs)


@pytest.mark.parametrize("effect", ["move", "noop"])
async def test_single_accepted_request_never_blind_retried_and_actual_coord_wins(effect):
    harness = Harness(snapshot(), effect=effect)
    result = await harness.run()
    assert harness.trace == ["execute", "refresh"]
    row = result["execution"][0]
    assert row["after"]["coord"] != "999,999"
    assert row["outcome"] == ("submitted_observation_unchanged" if effect == "noop"
                              else "submitted_observation_changed")
    assert len(harness.calls[0][1]["idempotency_key"]) < 96


async def test_rejections_try_at_most_two_distinct_candidates_with_fresh_observations():
    harness = Harness(snapshot(), effect="reject")
    result = await harness.run()
    assert harness.trace == ["execute", "refresh", "execute", "refresh"]
    assert len({args["dest"] for _, args in harness.calls}) == 2
    assert len(result["execution"]) == 2
    fallback = result["execution"][1]["decision"]
    assert fallback["selected"]["reason"] == "bounded_alternative_after_explicit_rejection"
    selected = fallback["selected"]["args"]["dest"]
    assert next(row for row in fallback["candidates"] if row["dest"] == selected)[
        "probability"] == 1


@pytest.mark.parametrize("effect,rejection", [
    ("reject_refrozen", "illegal_move"), ("reject_moved", "illegal_move"),
    ("reject", "no_lease"), ("reject", "unknown_entity"),
])
async def test_no_retry_after_refreeze_changed_state_or_nonlegality_failure(effect, rejection):
    harness = Harness(snapshot(), effect=effect, rejection=rejection)
    await harness.run()
    assert len(harness.calls) == 1


async def test_spent_units_do_not_execute_but_explicit_opening_frozen_roster_attempts_once():
    state = snapshot([unit(movement=0)])
    harness = Harness(state, effect="reject_refrozen")
    result = await harness.run()
    assert not harness.calls and result["decisions"][0]["reason"] == "movement_spent"
    result = await harness.run(frozen_unit_ids={"u0:131073"})
    assert len(harness.calls) == 1
    assert result["decisions"][0]["movement"] == 0
    assert result["decisions"][0]["movement_authority"] == "opening_frozen_allowance_unobserved"
    with pytest.raises(ValueError):
        plan(state, frozen_unit_ids={"u1:131073"})


async def test_total_roster_is_bounded():
    state = snapshot([unit(f"u0:{i}", coord=f"{i},0") for i in range(MAX_UNITS + 5)])
    harness = Harness(state, effect="noop")
    result = await harness.run()
    assert len(harness.calls) == MAX_UNITS
    assert result["deferred_units"] == 5


async def test_consumed_override_preserves_other_units_seed_and_never_reuses_stale_roster():
    state = snapshot([unit("u0:1", kind="SETTLER"), unit("u0:2", coord="1,0")])
    directive = {"tactical_overrides": [{"unit_id": "u0:1", "action": "found_city"}]}
    original = plan(state, directive)
    calls = []
    async def execute(name, args):
        calls.append((name, args))
        if name == "found_city":
            state["get_units"] = [u for u in state["get_units"] if u["unit_id"] != "u0:1"]
        return {"status": "accepted"}
    async def refresh():
        return copy.deepcopy(state)
    result = await run_scouting(copy.deepcopy(state), directive=directive, **IDENTITY,
                                execute=execute, refresh=refresh)
    assert [args["unit_id"] for _, args in calls] == ["u0:1", "u0:2"]
    assert result["execution"][0]["after"] == {"present": False}
    assert result["execution"][1]["decision"]["seed"] == original["decisions"][1]["seed"]


async def test_next_unit_decision_uses_new_threat_observation_after_first_action():
    state = snapshot([unit("u0:1"), unit("u0:2", coord="1,0")])
    calls = []
    async def execute(name, args):
        calls.append((name, args))
        if len(calls) == 1:
            state["get_units"].append(unit("u1:1", coord="2,0", owner=1))
        return {"status": "accepted"}
    async def refresh():
        return copy.deepcopy(state)
    result = await run_scouting(copy.deepcopy(state), directive={}, **IDENTITY,
                                execute=execute, refresh=refresh)
    second = result["execution"][1]["decision"]
    for candidate in second["candidates"]:
        pos = tuple(map(int, candidate["dest"].split(",")))
        if hex_dist(pos, (2, 0)) <= 1:
            assert candidate["excluded"] is not None
    assert len(calls) == 2


async def test_execute_transport_error_stops_without_retries():
    execute = AsyncMock(side_effect=TimeoutError("transport"))
    refresh = AsyncMock()
    with pytest.raises(TimeoutError):
        await run_scouting(snapshot(), directive={}, **IDENTITY, execute=execute, refresh=refresh)
    execute.assert_awaited_once()
    refresh.assert_not_called()


async def test_real_adapter_rejection_refreezes_before_executor_observes_and_stops():
    """Use the real adapter's restore/rejection path, with wire methods mocked."""
    state = snapshot([unit(movement=0)])
    adapter = FireTunerAdapter()
    adapter._phase_open = 0
    adapter._conn = AsyncMock()
    trace = []
    async def read(lua):
        if "RestoreUnit" in lua:
            trace.append("restore")
            state["get_units"][0]["movement"] = 2
            return ["RESTORE_UNIT|0|131073|restored", "---END---"]
        assert "FreezeUnit" in lua
        trace.append("refreeze")
        state["get_units"][0]["movement"] = 0
        return ["FROZEN|131073", "---END---"]
    adapter._conn.execute_read.side_effect = read
    adapter._conn.execute_write.return_value = ["ACT|move_unit|ERR|ILLEGAL_MOVE|blocked"]
    async def execute(name, args):
        trace.append("execute")
        args = dict(args)
        key = args.pop("idempotency_key")
        result = await adapter.act(ActionCommand(tool=name, args=args, player_id=0,
                                                 idempotency_key=key, lease_id="fixture"))
        return {"status": result.status, "rejection": result.rejection}
    async def refresh():
        trace.append("refresh")
        return copy.deepcopy(state)
    result = await run_scouting(copy.deepcopy(state), directive={}, **IDENTITY,
                                execute=execute, refresh=refresh,
                                frozen_unit_ids={"u0:131073"})
    assert trace == ["execute", "restore", "refreeze", "refresh"]
    assert result["execution"][0]["after"]["movement"] == 0
    assert adapter._conn.execute_write.await_count == 1
