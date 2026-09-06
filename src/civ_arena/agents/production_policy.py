"""Deterministic desired-inventory production from current projected observations.

Preferences recur for every empty city queue. Units are eligible only while
owned + queued + unobserved accepted reservations are below their desired count.
No name aliases, hostile-player guesses, or wall clocks enter these decisions.
"""
from __future__ import annotations

import re
from collections import Counter

from civ_arena.agents.strategy_directive import MAX_UNIT_TARGET, coordinate

# Exact base-ruleset IDs, considered only when the current city offers them.
# This opening defense list is intentionally not a universal combat-unit catalog.
DEFENDERS = ("ARCHER", "SUMERIAN_WAR_CART", "SPEARMAN", "WARRIOR", "SLINGER")
_ID = re.compile(r"[A-Z][A-Z0-9_]{0,63}")
THREAT_RADIUS = 5
_OPAQUE_QUEUE = re.compile(r"UNKNOWN_PRODUCTION_(-?[1-9][0-9]{0,15})")


def _owner(row):
    return row.get("owner", row.get("owner_id"))


def _item(value):
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise ValueError("production observation has invalid exact item ID")
    return value


def _owned(state, field, player_id, identity):
    rows = state.get(field)
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise ValueError("production observation has invalid entity rows")
    owned = [row for row in rows if _owner(row) == player_id]
    ids = [row.get(identity) for row in owned]
    if any(not isinstance(uid, str) or not uid for uid in ids) or len(set(ids)) != len(ids):
        raise ValueError("production observation has duplicate or missing owned identity")
    return owned


def _opaque_queue_item(value):
    """Native unresolved production hash, not a unit/building type identity."""
    match = _OPAQUE_QUEUE.fullmatch(value) if isinstance(value, str) else None
    return bool(match and abs(int(match[1])) < 2**53)


def _queue(city):
    value = city.get("production_queue")
    # Retain the existing facade's legacy single-string queue representation.
    if isinstance(value, str):
        value = [value] if value else []
    if not isinstance(value, list) or len(value) > 32:
        raise ValueError("production queue unavailable or exceeds bounded queue size")
    for item in value:
        if isinstance(item, str) and item.startswith("UNKNOWN_PRODUCTION_"):
            if not _opaque_queue_item(item):
                raise ValueError("production queue has invalid opaque native hash")
        else:
            _item(item)
    return list(value)


def _near_city(unit, cities):
    try:
        q, r = coordinate(unit.get("coord"))
    except ValueError:
        return False
    for city in cities:
        try:
            cq, cr = coordinate(city.get("coord"))
        except ValueError:
            continue
        if max(abs(q - cq), abs(r - cr), abs(q + r - cq - cr)) <= THREAT_RADIUS:
            return True
    return False


def choose_production(state: dict, *, player_id: int, city_id: str, options: list,
                      directive: dict, reservations: dict[str, str] | None = None,
                      growth_policy=None) -> dict:
    """Choose one exact available item and explain inventory/eligibility decisions.

    Reservations last only for the caller's current economy pass. An accepted
    queue request whose item is not yet in that city's observation counts once
    conservatively; it is never reported as an observed queue or completed unit.
    """
    units = _owned(state, "get_units", player_id, "unit_id")
    cities = _owned(state, "get_cities", player_id, "city_id")
    if len(cities) > 32:
        raise ValueError("production policy exceeds 32-city bound")
    cities_by_id = {city["city_id"]: city for city in cities}
    if city_id not in cities_by_id:
        raise ValueError("production requires an observed owned city")
    queues = {cid: _queue(city) for cid, city in cities_by_id.items()}
    if queues[city_id]:
        raise ValueError("production policy never replaces an active queue")
    if not isinstance(options, list) or len(options) > 256:
        raise ValueError("production catalog unavailable or exceeds bounded size")
    catalog = {}
    for row in options:
        if not isinstance(row, dict) or row.get("kind") not in ("unit", "building"):
            raise ValueError("production catalog requires observed unit/building kind")
        item = _item(row.get("item_id"))
        if item in catalog:
            raise ValueError("production catalog has duplicate exact item ID")
        catalog[item] = row["kind"]
    owned = Counter(_item(unit.get("type")) for unit in units)
    # Preserve opaque tokens as occupied queues, but never guess their item kind
    # or count an unresolved native hash toward any desired unit inventory.
    queued = Counter(item for queue in queues.values() for item in queue
                     if not _opaque_queue_item(item))
    reserved = Counter()
    for cid, item in sorted((reservations or {}).items()):
        if cid in queues and _item(item) not in queues[cid]:
            reserved[item] += 1
    effective = owned + queued + reserved
    threats = sorted(unit["unit_id"] for unit in state["get_units"]
                     if _owner(unit) != player_id and unit.get("is_barbarian") is True
                     and isinstance(unit.get("unit_id"), str) and _near_city(unit, cities))
    defense_goal = min(MAX_UNIT_TARGET, 2 * len(cities))
    defenders = sum(effective[item] for item in DEFENDERS)
    targets = directive.get("unit_targets", {})
    assigned = "SCOUT" in directive["scouting"]["unit_types"]
    candidates = []
    for item, kind in sorted(catalog.items()):
        target = None
        reason = "building_available"
        if kind == "unit":
            default = 1
            if item == "SCOUT":
                default = 2
            elif item == "BUILDER":
                default = len(cities)
            elif threats and item in DEFENDERS:
                shortfall = max(0, defense_goal - defenders)
                default = min(MAX_UNIT_TARGET, max(1, effective[item] + shortfall))
            target = targets.get(item, default)
            reason = ("scouting_role_disabled" if item == "SCOUT" and not assigned else
                      "unit_target_satisfied" if effective[item] >= target else
                      "below_unit_target")
        candidates.append({"item_id": item, "kind": kind, "target": target,
                           "owned": owned[item], "queued": queued[item],
                           "reserved": reserved[item], "effective": effective[item],
                           "eligible": reason in ("building_available", "below_unit_target"),
                           "reason": reason})
    eligible = {row["item_id"] for row in candidates if row["eligible"]}
    prefs = directive["production_preferences"]
    selected, reason = None, "no_eligible_production"
    if threats and defenders < defense_goal:
        defense_order = [item for item in prefs if item in DEFENDERS] + list(DEFENDERS)
        selected = next((item for item in defense_order if item in eligible), None)
        if selected is not None:
            reason = "confirmed_nearby_barbarian_defense"
    if selected is None:
        selected = next((item for item in prefs if item in eligible), None)
        if selected is not None:
            reason = "recurring_preference_below_target_or_building"
    if selected is None:
        selected = next((item for item in sorted(eligible) if catalog[item] == "building"), None)
        if selected is not None:
            reason = "available_building_fallback"
    if selected is None:
        unit_order = ["BUILDER", *DEFENDERS, "SCOUT", "SETTLER", *sorted(eligible)]
        selected = next((item for item in unit_order if item in eligible), None)
        if selected is not None:
            reason = "available_unit_below_target_fallback"
    result = {"version": 1, "city_id": city_id, "item_id": selected, "reason": reason,
            "count_basis": "owned_plus_observed_queues_plus_unobserved_accepted_reservations",
            "candidates": candidates, "nearby_confirmed_barbarians": threats,
            "opaque_active_queues": [{"city_id": cid, "token": item}
                                     for cid, queue in sorted(queues.items()) for item in queue
                                     if _opaque_queue_item(item)],
            "defenders_owned_queued_reserved": defenders, "defense_goal": defense_goal,
            "scout_role_assigned": assigned,
            "unavailable_preferences": [item for item in prefs if item not in catalog],
            "unavailable_target_ids": sorted(item for item in targets
                                             if catalog.get(item) != "unit")}

    if growth_policy is not None:
        return growth_policy.adjust_production(result, state=state, player_id=player_id,
                                               city_id=city_id, options=options,
                                               directive=directive,
                                               reservations=reservations)
    return result
