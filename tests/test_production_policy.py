"""Observed inventory production choices; no provider, tuner, or desktop input."""
import copy

import pytest

from civ_arena.agents.production_policy import choose_production
from civ_arena.agents.strategy_directive import validate_directive


def unit(item="SCOUT", uid="u0:1", owner=0, **extra):
    return {"unit_id": uid, "type": item, "owner": owner, "coord": "0,0", **extra}


def city(cid="c0:1", queue=None, owner=0, **extra):
    return {"city_id": cid, "owner": owner, "coord": "0,0",
            "production_queue": [] if queue is None else queue, **extra}


def choose(*, units=(), cities=None, options=("SCOUT", "MONUMENT"), prefs=("SCOUT",),
           targets=None, assigned=True, reservations=None):
    state = {"get_units": list(units), "get_cities": cities or [city()]}
    directive = validate_directive({"production_preferences": list(prefs),
                                   "unit_targets": targets or {},
                                   "scouting": {"unit_types": ["SCOUT"] if assigned else []}},
                                  player_id=0,
                                  owned_unit_ids={u["unit_id"] for u in units if u["owner"] == 0})
    catalog = [{"item_id": item, "kind": "building" if item in ("MONUMENT", "GRANARY")
               else "unit"} for item in options]
    original = copy.deepcopy((state, directive, catalog, reservations))
    result = choose_production(state, player_id=0, city_id="c0:1", options=catalog,
                               directive=directive, reservations=reservations)
    assert (state, directive, catalog, reservations) == original
    return result


def candidate(result, item):
    return next(row for row in result["candidates"] if row["item_id"] == item)


@pytest.mark.parametrize("owned,queued,expected", [(0, 0, "SCOUT"), (1, 0, "SCOUT"),
                                                   (2, 0, "MONUMENT"), (4, 0, "MONUMENT"),
                                                   (1, 1, "MONUMENT"), (0, 2, "MONUMENT")])
def test_scout_cap_counts_owned_and_every_owned_queue(owned, queued, expected):
    result = choose(units=[unit(uid=f"u0:{i}") for i in range(owned)],
                    cities=[city(), city("c0:2", ["SCOUT"] * queued)])
    assert result["item_id"] == expected
    row = candidate(result, "SCOUT")
    assert (row["owned"], row["queued"], row["target"]) == (owned, queued, 2)


def test_foreign_rosters_do_not_spend_owned_targets():
    result = choose(units=[unit(uid="u1:1", owner=1), unit(uid="u1:2", owner=1)],
                    cities=[city(), city("c1:1", ["SCOUT", "SCOUT"], owner=1)])
    assert result["item_id"] == "SCOUT"
    assert candidate(result, "SCOUT")["effective"] == 0


@pytest.mark.parametrize("owned,expected", [(1, "BUILDER"), (2, "MONUMENT")])
def test_builder_target_one_per_owned_city(owned, expected):
    result = choose(units=[unit("BUILDER", uid=f"u0:{i}") for i in range(owned)],
                    cities=[city(), city("c0:2")], options=("BUILDER", "MONUMENT"),
                    prefs=("BUILDER",))
    assert result["item_id"] == expected
    assert candidate(result, "BUILDER")["target"] == 2


def test_model_targets_zero_and_raised_exact_id():
    assert choose(targets={"SCOUT": 0})["item_id"] == "MONUMENT"
    assert choose(units=[unit(uid=f"u0:{i}") for i in range(4)],
                  targets={"SCOUT": 5})["item_id"] == "SCOUT"


def test_accepted_unobserved_reservation_prevents_second_city_overbuild():
    result = choose(units=[unit()], cities=[city(), city("c0:2")],
                    reservations={"c0:2": "SCOUT"})
    row = candidate(result, "SCOUT")
    assert (row["owned"], row["queued"], row["reserved"], row["effective"]) == (1, 0, 1, 2)
    assert result["item_id"] == "MONUMENT"


def test_observed_queue_replaces_reservation_without_double_counting():
    result = choose(cities=[city(), city("c0:2", "SCOUT")],
                    reservations={"c0:2": "SCOUT"})
    row = candidate(result, "SCOUT")
    assert (row["queued"], row["reserved"], row["effective"]) == (1, 0, 1)
    assert result["item_id"] == "SCOUT"


def test_lost_city_reservation_is_not_owned_inventory():
    assert choose(reservations={"c1:3": "SCOUT"})["item_id"] == "SCOUT"


def test_unavailable_alias_never_resolves_to_civilization_unique_unit():
    result = choose(options=("SUMERIAN_WAR_CART", "MONUMENT"), prefs=("WAR_CART",),
                    targets={"WAR_CART": 0})
    assert result["item_id"] == "MONUMENT"
    assert result["unavailable_preferences"] == ["WAR_CART"]
    assert result["unavailable_target_ids"] == ["WAR_CART"]
    assert choose(options=("SUMERIAN_WAR_CART", "MONUMENT"),
                  prefs=("SUMERIAN_WAR_CART",))["item_id"] == "SUMERIAN_WAR_CART"


@pytest.mark.parametrize("flag,coord,owner,expected", [(True, "1,0", 63, "ARCHER"),
                                                       (True, "5,0", 63, "ARCHER"),
                                                       (True, "6,0", 63, "SCOUT"),
                                                       (False, "1,0", 63, "SCOUT"),
                                                       (None, "1,0", 63, "SCOUT"),
                                                       (1, "1,0", 63, "SCOUT"),
                                                       ("true", "1,0", 63, "SCOUT"),
                                                       (True, "1,0", 0, "SCOUT")])
def test_defense_priority_requires_projected_boolean_near_owned_city(flag, coord, owner,
                                                                   expected):
    contact = unit("WARRIOR", "contact", owner=owner, coord=coord, is_barbarian=flag)
    result = choose(units=[contact], options=("SCOUT", "ARCHER", "MONUMENT"))
    assert result["item_id"] == expected
    assert bool(result["nearby_confirmed_barbarians"]) == (expected == "ARCHER")


def test_defense_total_includes_owned_and_queued_and_respects_model_zero():
    barb = unit("WARRIOR", "barb", owner=63, is_barbarian=True)
    owned = [unit("WARRIOR", "u0:1"), unit("SLINGER", "u0:2")]
    result = choose(units=[barb, *owned], options=("SCOUT", "ARCHER"))
    assert result["item_id"] == "SCOUT"
    assert result["defenders_owned_queued_reserved"] == result["defense_goal"] == 2
    result = choose(units=[barb], cities=[city(), city("c0:2", ["ARCHER"] * 4)],
                    options=("SCOUT", "ARCHER"))
    assert result["item_id"] == "SCOUT"
    result = choose(units=[barb], options=("SCOUT", "ARCHER"), targets={"ARCHER": 0})
    assert result["item_id"] == "SCOUT"
    assert candidate(result, "ARCHER")["reason"] == "unit_target_satisfied"


def test_explicit_scout_role_exclusion_prevents_repeated_unassigned_scouts():
    result = choose(assigned=False, targets={"SCOUT": 32})
    assert result["item_id"] == "MONUMENT"
    assert candidate(result, "SCOUT")["reason"] == "scouting_role_disabled"


def test_exhausted_targets_have_explicit_no_choice():
    result = choose(options=("SCOUT",), targets={"SCOUT": 0})
    assert result["item_id"] is None
    assert result["reason"] == "no_eligible_production"


@pytest.mark.parametrize("mutation", ["duplicate_unit", "duplicate_city", "queue_missing",
                                      "queue_dict", "option_missing_kind", "duplicate_option",
                                      "active_queue", "invalid_type"])
def test_malformed_observation_refuses_to_guess_inventory(mutation):
    state = {"get_units": [unit()], "get_cities": [city()]}
    options = [{"item_id": "SCOUT", "kind": "unit"}]
    if mutation == "duplicate_unit":
        state["get_units"] *= 2
    elif mutation == "duplicate_city":
        state["get_cities"] *= 2
    elif mutation == "queue_missing":
        state["get_cities"][0].pop("production_queue")
    elif mutation == "queue_dict":
        state["get_cities"][0]["production_queue"] = {}
    elif mutation == "option_missing_kind":
        options[0].pop("kind")
    elif mutation == "duplicate_option":
        options *= 2
    elif mutation == "active_queue":
        state["get_cities"][0]["production_queue"] = ["SCOUT"]
    else:
        state["get_units"][0]["type"] = "UNIT;INJECT"
    with pytest.raises(ValueError):
        choose_production(state, player_id=0, city_id="c0:1", options=options,
                          directive=validate_directive({}, player_id=0, owned_unit_ids={"u0:1"}))



def test_defense_dynamic_target_only_fills_remaining_empire_gap():
    barb = unit("WARRIOR", "barb", owner=63, is_barbarian=True)
    result = choose(units=[barb, unit("ARCHER", "u0:1")], options=("WARRIOR",),
                    prefs=("WARRIOR",))
    assert candidate(result, "WARRIOR")["target"] == 1
    assert result["item_id"] == "WARRIOR"
    result = choose(units=[barb, unit("ARCHER", "u0:1"), unit("WARRIOR", "u0:2")],
                    options=("WARRIOR", "MONUMENT"), prefs=("WARRIOR",))
    assert result["item_id"] == "MONUMENT"


def test_default_other_unit_target_does_not_repeat_forever():
    result = choose(units=[unit("SUMERIAN_WAR_CART")],
                    options=("SUMERIAN_WAR_CART", "MONUMENT"), prefs=("SUMERIAN_WAR_CART",))
    assert result["item_id"] == "MONUMENT"
    assert candidate(result, "SUMERIAN_WAR_CART")["target"] == 1
