"""Qualified live identities never enter numeric planner simulation or leak back."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from civ_arena.planner.entity_boundary import EntityBoundary, planner_id


def facade():
    return SimpleNamespace(
        get_units=AsyncMock(return_value=[
            {"unit_id": "u0:1", "owner_id": 0}, {"unit_id": "u1:1", "owner_id": 1},
            {"unit_id": "u0:9007199254740991", "owner_id": 0}]),
        get_cities=AsyncMock(return_value=[
            {"city_id": "c0:7", "owner": 0}, {"city_id": "c1:7", "owner_id": 1}]),
        get_visible_map=AsyncMock(return_value={"tiles": {
            "0,0": {"city_id": "c0:7", "owner_id": 0},
            "1,0": {"city_id": "c2:99", "owner_id": 2}}}),
        move_unit=AsyncMock(return_value={"status": "accepted"}),
        attack=AsyncMock(return_value={"status": "accepted"}),
        fortify=AsyncMock(return_value={"status": "accepted"}),
        purchase=AsyncMock(return_value={"status": "accepted", "unit_id": "u0:81"}),
        get_available_production=AsyncMock(return_value=[]),
        end_turn=AsyncMock(return_value={"status": "rejected", "rejection": "unmoved_units",
                                        "unmoved_units": ["u0:1"]}),
    )


def test_pair_encoding_is_exact_collision_free_and_kind_preserving():
    pairs = [(owner, raw) for owner in range(8) for raw in [0, 1, 2, 65536, 2**53 - 1]]
    encoded = [planner_id(f"u{owner}:{raw}", "u") for owner, raw in pairs]
    assert len(set(encoded)) == len(pairs)
    assert all(int(value[1:]) >= 1 and ":" not in value for value in encoded)
    assert planner_id("c1:1", "c")[1:] == planner_id("u1:1", "u")[1:]
    with pytest.raises(ValueError):
        planner_id("u0:9007199254740992", "u")


async def test_projection_ids_and_map_tags_translate_without_mutating_sources():
    raw = facade()
    boundary = EntityBoundary(raw)
    units = await boundary.get_units()
    cities = await boundary.get_cities()
    tiles = (await boundary.get_visible_map())["tiles"]
    assert [row["unit_id"] for row in units] == [planner_id(x, "u") for x in
                                               ["u0:1", "u1:1", "u0:9007199254740991"]]
    assert cities[0]["city_id"] == tiles["0,0"]["city_id"] == planner_id("c0:7", "c")
    assert raw.get_units.return_value[0]["unit_id"] == "u0:1"
    assert raw.get_visible_map.return_value["tiles"]["0,0"]["city_id"] == "c0:7"


async def test_actions_and_positional_production_reverse_only_observed_ids():
    raw, boundary = facade(), None
    boundary = EntityBoundary(raw)
    await boundary.get_units()
    await boundary.get_cities()
    await boundary.move_unit(unit_id=planner_id("u0:9007199254740991", "u"), dest="1,0")
    await boundary.attack(planner_id("u0:1", "u"), planner_id("u1:1", "u"))
    await boundary.get_available_production(planner_id("c0:7", "c"))
    raw.move_unit.assert_awaited_once_with(unit_id="u0:9007199254740991", dest="1,0")
    raw.attack.assert_awaited_once_with("u0:1", "u1:1")
    raw.get_available_production.assert_awaited_once_with("c0:7")
    result = await boundary.move_unit(unit_id="u999999", dest="1,0")
    assert result["status"] == "rejected"
    raw.move_unit.assert_awaited_once()


async def test_result_and_map_references_do_not_authorize_synthetic_future_entities():
    raw = facade()
    boundary = EntityBoundary(raw)
    await boundary.get_units()
    await boundary.get_cities()
    await boundary.get_visible_map()
    result = await boundary.purchase(city_id=planner_id("c0:7", "c"), item_id="SCOUT")
    assert result["unit_id"] == planner_id("u0:81", "u")
    rejected = await boundary.fortify(unit_id=result["unit_id"])
    assert rejected["status"] == "rejected"
    raw.fortify.assert_not_awaited()
    rejected = await boundary.get_available_production(planner_id("c2:99", "c"))
    assert rejected["status"] == "rejected"
    raw.get_available_production.assert_not_awaited()
    raw.get_units.return_value.append({"unit_id": "u0:81", "owner_id": 0})
    await boundary.get_units()
    await boundary.fortify(unit_id=result["unit_id"])
    raw.fortify.assert_awaited_once_with(unit_id="u0:81")


async def test_reconciliation_retires_entities_and_fresh_wrapper_rebuilds_stable_ids():
    raw = facade()
    first = EntityBoundary(raw)
    units = await first.get_units()
    target = planner_id("u1:1", "u")
    raw.get_units.return_value = [raw.get_units.return_value[0]]
    await first.get_units()
    result = await first.attack(unit_id=units[0]["unit_id"], target_id=target)
    assert result["status"] == "rejected"
    raw.attack.assert_not_awaited()
    resumed = EntityBoundary(raw)
    assert (await resumed.get_units())[0]["unit_id"] == units[0]["unit_id"]
    assert (await resumed.end_turn())["unmoved_units"] == [units[0]["unit_id"]]
    await resumed.fortify(unit_id=units[0]["unit_id"])
    raw.fortify.assert_awaited_once_with(unit_id="u0:1")


async def test_legacy_numeric_facade_behavior_is_unchanged():
    raw = facade()
    raw.get_units.return_value = [{"unit_id": "u1", "owner_id": 0}]
    raw.get_cities.return_value = [{"city_id": "c7", "owner": 0}]
    raw.get_visible_map.return_value = {"tiles": {"0,0": {"city_id": "c7"}}}
    raw.end_turn.return_value = {"status": "accepted"}
    boundary = EntityBoundary(raw)
    assert await boundary.get_units() is raw.get_units.return_value
    assert await boundary.get_cities() is raw.get_cities.return_value
    assert await boundary.get_visible_map() is raw.get_visible_map.return_value
    await boundary.move_unit(unit_id="u1", dest="1,0")
    raw.move_unit.assert_awaited_once_with(unit_id="u1", dest="1,0")
    assert await boundary.end_turn() is raw.end_turn.return_value


@pytest.mark.parametrize("rows", [
    [{"unit_id": "u0:1", "owner_id": 1}],
    [{"unit_id": "u0:1", "owner_id": 0}, {"unit_id": "u3", "owner_id": 0}],
    [{"unit_id": "u0:1", "owner_id": 0}, {"unit_id": "u0:1", "owner_id": 0}],
])
async def test_incoherent_projection_never_grants_dispatch_authority(rows):
    raw = facade()
    raw.get_units.return_value = rows
    with pytest.raises(ValueError):
        await EntityBoundary(raw).get_units()
    raw.move_unit.assert_not_awaited()


async def test_empty_observations_do_not_authorize_synthetic_dispatch():
    raw = facade()
    raw.get_units.return_value = []
    raw.get_cities.return_value = []
    raw.get_visible_map.return_value = {"tiles": {}}
    boundary = EntityBoundary(raw)
    await boundary.get_units()
    await boundary.get_cities()
    await boundary.get_visible_map()
    assert (await boundary.fortify(unit_id="u999"))["status"] == "rejected"
    raw.fortify.assert_not_awaited()
