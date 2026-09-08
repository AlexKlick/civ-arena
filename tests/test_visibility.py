"""Visibility isolation: projections vs ground truth, allowlists, leak meta-test.

The property test compares EVERY projected entity against independently
computed ground truth; the meta-test injects a deliberately leaky policy and
asserts the checker catches it — proving the property has teeth.
"""

from __future__ import annotations

import pytest

from civ_arena.arena.visibility import (
    BILATERAL_STUB,
    FOREIGN_CITY_FIELDS,
    Scope,
    VisibilityPolicy,
    find_city_leaks,
    find_leaks,
)
from civ_arena.game.adapter import ObserveKind
from civ_arena.game.sim.visibility import ground_truth
from conftest import observe, own_units, scenario_adapter, teleport


@pytest.mark.parametrize("seed", [1, 2, 3])
@pytest.mark.parametrize("pid", [0, 1])
async def test_observation_matches_ground_truth(seed: int, pid: int):
    adapter = await scenario_adapter(seed)
    try:
        vis = ground_truth(adapter.state, pid)
        policy = VisibilityPolicy()

        omni_units = await observe(adapter, ObserveKind.UNITS, pid)
        omni_cities = await observe(adapter, ObserveKind.CITIES, pid)

        proj_units = policy.project(
            omni_units, "units", pid, vis.observable, vis.remembered
        )
        assert find_leaks(proj_units, omni_units, pid, vis.observable, vis.remembered) == []

        proj_cities = policy.project(
            omni_cities, "cities", pid, vis.observable, vis.remembered
        )
        assert find_city_leaks(
            proj_cities, omni_cities, pid, vis.observable, vis.remembered
        ) == []

        omni_map = await observe(adapter, ObserveKind.VISIBLE_MAP, pid)
        proj_map = policy.project(
            omni_map, "visible_map", pid, vis.observable, vis.remembered
        )
        assert set(proj_map["tiles"]) <= (vis.remembered | vis.observable)
        # every tile the player can currently see shows ownership; remembered-only does not
        for key, tile in proj_map["tiles"].items():
            if key in vis.observable:
                assert "owner_id" in tile
    finally:
        await adapter.teardown()


async def test_hidden_enemy_absent_and_visible_enemy_allowlisted():
    adapter = await scenario_adapter(1)
    try:
        enemy_units = own_units(adapter.state, 1, "WARRIOR")
        # one enemy adjacent to player 0's city (observable), one far away (hidden)
        p0_city = sorted(
            (c for c in adapter.state.cities.values() if c["owner"] == 0),
            key=lambda c: c["city_id"],
        )[0]
        teleport(adapter.state, enemy_units[0]["unit_id"], p0_city["q"] + 1, p0_city["r"])
        teleport(adapter.state, enemy_units[1]["unit_id"], 5, -5)

        vis = ground_truth(adapter.state, 0)
        omni_units = await observe(adapter, ObserveKind.UNITS, 0)
        proj = VisibilityPolicy().project(
            omni_units, "units", 0, vis.observable, vis.remembered
        )
        ids = {u["unit_id"] for u in proj}
        assert enemy_units[0]["unit_id"] in ids, "enemy in sight must be projected"
        assert enemy_units[1]["unit_id"] not in ids, "hidden enemy must be ABSENT"
        seen = next(u for u in proj if u["unit_id"] == enemy_units[0]["unit_id"])
        assert "movement" not in seen and "fortified" not in seen, (
            "foreign units must not reveal movement/orders"
        )
    finally:
        await adapter.teardown()


async def test_foreign_city_hides_internals_even_when_seen():
    adapter = await scenario_adapter(2)
    try:
        p1_city = sorted(
            (c for c in adapter.state.cities.values() if c["owner"] == 1),
            key=lambda c: c["city_id"],
        )[0]
        assert p1_city["production_queue"], "scenario must leave a production queue set"
        scout = own_units(adapter.state, 0, "SCOUT")[0]
        teleport(adapter.state, scout["unit_id"], p1_city["q"] + 1, p1_city["r"])

        vis = ground_truth(adapter.state, 0)
        omni_cities = await observe(adapter, ObserveKind.CITIES, 0)
        proj = VisibilityPolicy().project(
            omni_cities, "cities", 0, vis.observable, vis.remembered
        )
        seen = next(c for c in proj if c["city_id"] == p1_city["city_id"])
        for leaky in ("production_queue", "food_bucket", "production_bucket", "buildings"):
            assert leaky not in seen, f"foreign city leaked {leaky}"
        own_city = next(c for c in proj if c.get("owner_id", c.get("owner")) == 0)
        assert "production_queue" in own_city, "own city keeps full fields"
    finally:
        await adapter.teardown()


async def test_leak_meta_catches_broken_policy():
    """If the property test lost discriminating power, this test fails."""

    class LeakyPolicy(VisibilityPolicy):
        foreign_city_fields = FOREIGN_CITY_FIELDS | {"production_queue"}

    adapter = await scenario_adapter(1)
    try:
        p1_city = sorted(
            (c for c in adapter.state.cities.values() if c["owner"] == 1),
            key=lambda c: c["city_id"],
        )[0]
        scout = own_units(adapter.state, 0, "SCOUT")[0]
        teleport(adapter.state, scout["unit_id"], p1_city["q"] + 1, p1_city["r"])

        vis = ground_truth(adapter.state, 0)
        omni_cities = await observe(adapter, ObserveKind.CITIES, 0)
        proj = LeakyPolicy().project(
            omni_cities, "cities", 0, vis.observable, vis.remembered
        )
        leaks = find_city_leaks(proj, omni_cities, 0, vis.observable, vis.remembered)
        assert any("production_queue" in leak for leak in leaks), (
            "the checker must catch a leaky policy"
        )
    finally:
        await adapter.teardown()


def test_foreign_city_never_leaks_extended_internals():
    """M4: the extended CITIES|2 keys are omniscient-doc material only. A
    visible foreign city projects exactly the closed foreign set — hp
    rides only when the engine answered, and buildings/districts/
    food*/max_hp/turns* never cross the boundary even when present."""
    city = {
        "city_id": "c12:90", "owner": 12, "name": "CITYSTATE",
        "q": 3, "r": 4, "population": 6, "production_queue": ["SCOUT"],
        "is_major": False, "is_capital": True, "hp": 180, "max_hp": 200,
        "food_bucket": 9, "food_threshold": 15, "food_surplus": 3,
        "turns_to_growth": 4, "turns_to_production": 2,
        "buildings": ["BUILDING_MONUMENT"], "districts": ["DISTRICT_CITY_CENTER"],
    }
    projected = VisibilityPolicy()._city(  # noqa: SLF001
        city, 0, frozenset({"3,4"}), frozenset())
    assert projected == {"city_id": "c12:90", "name": "CITYSTATE",
                         "coord": "3,4", "owner_id": 12, "hp": 180,
                         "population": 6}
    for leaky in ("buildings", "districts", "food_bucket", "food_threshold",
                  "food_surplus", "max_hp", "turns_to_growth",
                  "turns_to_production", "production_queue", "is_major",
                  "is_capital"):
        assert leaky not in projected
    # hp is CONDITIONAL: an unread hp (key absent) projects absent, never
    # a placeholder
    unread = {k: v for k, v in city.items() if k != "hp"}
    assert "hp" not in VisibilityPolicy()._city(  # noqa: SLF001
        unread, 0, frozenset({"3,4"}), frozenset())


def test_public_match_projects_only_three_fields_even_for_rich_rows():
    """M4: an OVX|2-rich players doc still projects the three public
    fields; economy/civics never reach public (they are `you`-scope)."""
    doc = {"turn": 7, "players": {
        "0": {"player_id": 0, "civ_name": "CIV_A", "gold": 120,
              "researched": [], "researching": None, "alive": True,
              "science": 6, "culture": 5, "faith": 4, "gold_per_turn": 2,
              "upkeep": 3, "era": 0, "civics": ["CIVIC_A"]},
        "1": {"player_id": 1, "civ_name": "CIV_B", "gold": 90,
              "researched": [], "researching": "MINING", "alive": True},
    }}
    public = VisibilityPolicy()._public_match(doc)  # noqa: SLF001
    assert public == {
        "turn": 7,
        "players": [{"player_id": 0, "civ_name": "CIV_A", "alive": True},
                    {"player_id": 1, "civ_name": "CIV_B", "alive": True}],
    }


def test_own_player_passes_economy_for_self_only():
    """M4: the OVX|2 economy keys pass through for the projecting player
    only, and a legacy doc projects the exact legacy five keys."""
    rich = {"turn": 7, "players": {
        "0": {"player_id": 0, "civ_name": "CIV_A", "gold": 120,
              "researched": ["POTTERY"], "researching": None, "alive": True,
              "science": 6, "culture": 5, "faith": 4, "gold_per_turn": 2,
              "upkeep": 3, "era": 0, "progressing_civic": "CIVIC_A",
              "civic_progress": 7, "civic_cost": 60, "civics": ["CIVIC_A"]},
        "1": {"player_id": 1, "civ_name": "CIV_B", "gold": 90,
              "researched": [], "researching": "MINING", "alive": True},
    }}
    policy = VisibilityPolicy()
    you = policy._own_player(rich, 0)  # noqa: SLF001
    assert you["science"] == 6 and you["civics"] == ["CIVIC_A"]
    assert you["gold_per_turn"] == 2 and you["era"] == 0
    other = policy._own_player(rich, 1)  # noqa: SLF001
    assert set(other) == {"player_id", "civ_name", "gold", "researched",
                          "researching"}
    legacy = {"turn": 7, "players": {
        "0": {"player_id": 0, "civ_name": "CIV_A", "gold": 120,
              "researched": [], "researching": None, "alive": True}}}
    assert policy._own_player(legacy, 0) == {  # noqa: SLF001
        "player_id": 0, "civ_name": "CIV_A", "gold": 120,
        "researched": [], "researching": None}


async def test_scopes_referee_public_bilateral():
    adapter = await scenario_adapter(1)
    try:
        omni = await observe(adapter, ObserveKind.OVERVIEW, 0)
        policy = VisibilityPolicy()
        vis = ground_truth(adapter.state, 0)

        referee = policy.project(omni, "overview", 0, vis.observable, vis.remembered,
                                 scope=Scope.REFEREE)
        assert referee is omni, "referee scope is the omniscient doc itself"

        public = policy.project(omni, "overview", 0, vis.observable, vis.remembered,
                                scope=Scope.PUBLIC_MATCH)
        assert set(public["players"][0]) == {"player_id", "civ_name", "alive"}

        bilateral = policy.project(omni, "overview", 0, vis.observable, vis.remembered,
                                   scope=Scope.BILATERAL)
        assert bilateral == BILATERAL_STUB
    finally:
        await adapter.teardown()
