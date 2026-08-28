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
