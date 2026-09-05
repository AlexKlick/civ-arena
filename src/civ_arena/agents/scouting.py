"""Seeded frontier heuristics over projected observations, never a legality oracle.

Only the existing audited facade executes commands. Its adapter owns movement
restore/refreeze and engine legality. A submitted request with unchanged observed
position is not retried: the engine may still be applying it.
"""
from __future__ import annotations

import hashlib
import json
import math
import random
from collections.abc import Awaitable, Callable
from typing import Any

from civ_arena.agents.strategy_directive import coordinate, validate_directive
from civ_arena.game.sim.state import hex_dist, neighbors, tiles_within

MAX_UNITS = 16
MAX_ATTEMPTS = 2
_LAND_COST = {"PLAINS": 1, "GRASSLAND": 1, "DESERT": 1, "TUNDRA": 1, "SNOW": 1,
              "HILL": 2, "HILLS": 2, "FOREST": 2, "JUNGLE": 2, "MARSH": 2}
_SAFE_REJECTIONS = {"illegal_move", "ILLEGAL_MOVE"}


def _owner(entity: dict) -> int:
    owner = entity.get("owner_id", entity.get("owner"))
    if type(owner) is not int:
        raise ValueError("projected entity requires an integer owner")
    return owner


def _snapshot(snapshot: dict) -> tuple[list[dict], dict[str, dict]]:
    if not isinstance(snapshot, dict):
        raise ValueError("projected snapshot must be an object")
    units, visible_map = snapshot.get("get_units"), snapshot.get("get_visible_map")
    if not isinstance(units, list) or len(units) > 512:
        raise ValueError("projected unit list unavailable or oversized")
    if not isinstance(visible_map, dict) or not isinstance(visible_map.get("tiles"), dict):
        raise ValueError("projected map unavailable")
    tiles = visible_map["tiles"]
    if len(tiles) > 20000:
        raise ValueError("projected map exceeds bounded planner capacity")
    seen = set()
    for unit in units:
        if not isinstance(unit, dict) or not isinstance(unit.get("unit_id"), str):
            raise ValueError("malformed projected unit")
        if unit["unit_id"] in seen:
            raise ValueError("duplicate projected unit identity")
        seen.add(unit["unit_id"])
        _owner(unit)
        coordinate(unit.get("coord"))
    for key, tile in tiles.items():
        coordinate(key)
        if not isinstance(tile, dict) or not isinstance(tile.get("terrain"), str):
            raise ValueError("malformed projected terrain")
    return units, tiles


def _remaining(unit: dict) -> float:
    movement = unit.get("movement")
    if type(movement) not in (int, float) or not math.isfinite(movement) or movement < 0:
        raise ValueError("owned unit movement unavailable or invalid")
    return movement


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     allow_nan=False).encode()).hexdigest()


def _hold(unit: dict, reason: str) -> dict:
    return {"action": "fortify", "args": {"unit_id": unit["unit_id"]}, "reason": reason}


def _decision(unit, units, tiles, directive, identity, directive_hash, opening_frozen):
    uid, origin = unit["unit_id"], coordinate(unit["coord"])
    seed = _digest(["frontier-v1", identity, uid, directive_hash])
    decision = {"unit_id": uid, "origin": unit["coord"], "movement": _remaining(unit),
                "seed": seed, "candidates": [], "selected": None, "override": False,
                "movement_authority": "opening_frozen_allowance_unobserved" if opening_frozen
                else "observed_remaining"}
    if decision["movement"] <= 0 and not opening_frozen:
        return {**decision, "reason": "movement_spent"}
    override = next((row for row in directive["tactical_overrides"] if row["unit_id"] == uid),
                    None)
    if override is not None:
        decision["override"] = True
        action = override["action"]
        if action == "move":
            # No unseen destination or automatic long route. Tactical movement
            # is a single observed adjacent step, still subject to engine rules.
            dest = override["dest"]
            if dest not in tiles or hex_dist(origin, coordinate(dest)) != 1:
                return {**decision, "reason": "override_destination_not_known_adjacent"}
            selected = {"action": "move_unit", "args": {"unit_id": uid, "dest": dest},
                        "reason": "explicit_tactical_override"}
        elif action == "attack":
            target = next((other for other in units
                           if other["unit_id"] == override["target_id"]), None)
            if target is None or _owner(target) == _owner(unit):
                return {**decision, "reason": "override_target_not_observed_foreign_unit"}
            selected = {"action": "attack", "args": {"unit_id": uid,
                        "target_id": target["unit_id"]}, "reason": "explicit_tactical_override"}
        elif action == "found_city":
            if unit.get("type") != "SETTLER":
                return {**decision, "reason": "override_requires_settler"}
            selected = {"action": "found_city", "args": {"unit_id": uid},
                        "reason": "explicit_tactical_override_engine_checks_site"}
        else:
            selected = _hold(unit, "explicit_tactical_hold")
        return {**decision, "selected": selected, "reason": selected["reason"]}
    assigned = unit.get("type") in directive["scouting"]["unit_types"]
    if unit.get("fortified") and not assigned:
        return {**decision, "reason": "existing_standing_order"}
    if not assigned:
        selected = _hold(unit, "settler_safe_hold" if unit.get("type") == "SETTLER"
                         else "unit_not_assigned_to_scouting")
        return {**decision, "selected": selected, "reason": selected["reason"]}
    # Assignment is persistent standing intent. Completeness repair can fortify
    # an otherwise assigned scout at closure; that must not silently disable its
    # role on every later quiet turn. Current tactical overrides were handled
    # first, and the normal movement/frozen-allowance guards remain in force.
    if unit.get("fortified"):
        decision["standing_order_resolution"] = "persistent_scouting_assignment"

    known = {coordinate(key) for key in tiles}
    frontier = [pos for pos in sorted(known) if any(n not in known for n in neighbors(*pos))]
    threats = [other for other in units if _owner(other) != _owner(unit)]
    occupied = {other["coord"] for other in units if other["unit_id"] != uid}
    policy = directive["scouting"]["policy"]
    weights = directive["scouting"]["weights"]
    for pos in sorted(neighbors(*origin)):
        dest = f"{pos[0]},{pos[1]}"
        tile = tiles.get(dest)
        excluded = None
        if tile is None:
            excluded = "unobserved_terrain"
        elif tile["terrain"] not in _LAND_COST:
            excluded = "unsupported_or_nonland_terrain"
        elif dest in occupied:
            excluded = "observed_occupied_tile"
        elif isinstance(tile.get("owner_id"), int) and tile["owner_id"] >= 0 \
                and tile["owner_id"] != _owner(unit):
            excluded = "observed_foreign_territory"
        distances = [hex_dist(pos, coordinate(other["coord"])) for other in threats]
        nearest = min(distances, default=None)
        avoid_radius = {"cautious": 2, "balanced": 1, "explore": 0}[policy]
        if excluded is None and nearest is not None and nearest <= avoid_radius:
            excluded = "observed_threat_proximity"
        row = {"dest": dest, "excluded": excluded, "probability": 0.0}
        if excluded is None:
            gain = sum(p not in known for p in tiles_within(pos, 2))
            distance = min((hex_dist(pos, p) for p in frontier), default=0)
            threat = sum(max(0, 3 - d) for d in distances)
            terrain = _LAND_COST[tile["terrain"]] - 1
            score = (weights["unexplored"] * gain - weights["distance"] * distance
                     - weights["threat"] * threat - weights["terrain"] * terrain)
            row.update(score=score, components={"unexplored_gain": gain,
                       "frontier_distance": distance, "known_threat_exposure": threat,
                       "terrain_penalty": terrain})
        decision["candidates"].append(row)
    candidates = [row for row in decision["candidates"] if row["excluded"] is None]
    if not candidates:
        selected = _hold(unit, "no_conservative_scouting_candidate")
    else:
        best = max(row["score"] for row in candidates)
        if directive["scouting"]["selection"] == "best":
            picked = min(candidates, key=lambda row: (-row["score"], row["dest"]))
            picked["probability"] = 1.0
        else:
            temperature = directive["scouting"]["temperature"]
            masses = [math.exp((row["score"] - best) / temperature) for row in candidates]
            total = sum(masses)
            for row, mass in zip(candidates, masses, strict=True):
                row["probability"] = mass / total
            picked = random.Random(int(seed, 16)).choices(candidates, weights=masses, k=1)[0]
        selected = {"action": "move_unit", "args": {"unit_id": uid, "dest": picked["dest"]},
                    "reason": "seeded_frontier_heuristic" if directive["scouting"]["selection"]
                    == "seeded" else "maximum_frontier_heuristic"}
    return {**decision, "selected": selected, "reason": selected["reason"]}


def plan_scouting(snapshot: dict, *, directive: dict, player_id: int,
                  match_id: str, agent_id: str, turn: int, seed: int = 0,
                  frozen_unit_ids: frozenset[str] | set[str] = frozenset()) -> dict:
    """Produce JSON audit with selection probabilities, not success probabilities."""
    units, tiles = _snapshot(snapshot)
    owned = [unit for unit in units if _owner(unit) == player_id]
    normalized = validate_directive(directive, player_id=player_id,
                                    owned_unit_ids={unit["unit_id"] for unit in owned})
    if (not isinstance(frozen_unit_ids, (set, frozenset))
            or not frozen_unit_ids <= {unit["unit_id"] for unit in owned}):
        raise ValueError("frozen roster must contain only phase-opening owned untouched units")
    if (type(seed) is not int or type(turn) is not int or turn < 1
            or not isinstance(match_id, str) or not match_id
            or not isinstance(agent_id, str) or not agent_id):
        raise ValueError("stable match, agent, and positive turn identity required")
    identity = {"match_id": match_id, "agent_id": agent_id, "player_id": player_id,
                "turn": turn, "configured_seed": seed}
    digest = _digest(normalized)
    overrides = {row["unit_id"] for row in normalized["tactical_overrides"]}
    owned.sort(key=lambda unit: (unit["unit_id"] not in overrides, unit["unit_id"]))
    return {"version": 1, "kind": "heuristic_frontier_plan", "identity": identity,
            "directive_sha256": digest, "directive": normalized,
            "seed_spec": "sha256(canonical_json([frontier-v1,identity,unit_id,directive_sha256]))",
            "probability_meaning": "selection_weight_not_calibrated_success_or_engine_legality",
            "knowledge": "projected_visible_or_remembered_terrain_and_current_visible_entities",
            "limits": {"units": MAX_UNITS, "attempts_per_unit": MAX_ATTEMPTS},
            "opening_frozen_unit_ids": sorted(frozen_unit_ids),
            "deferred_units": max(0, len(owned) - MAX_UNITS),
            "decisions": [_decision(unit, units, tiles, normalized, identity, digest,
                                     unit["unit_id"] in frozen_unit_ids)
                          for unit in owned[:MAX_UNITS]]}


def _observed(snapshot, uid, player_id):
    units, _ = _snapshot(snapshot)
    unit = next((unit for unit in units if unit["unit_id"] == uid), None)
    if unit is None:
        return {"present": False}
    if _owner(unit) != player_id:
        return {"present": True, "owned": False}
    return {"present": True, "owned": True, "coord": unit["coord"],
            "movement": _remaining(unit), "fortified": unit.get("fortified", False)}


async def run_scouting(
    snapshot: dict, *, directive: dict, player_id: int, match_id: str, agent_id: str, turn: int,
    execute: Callable[[str, dict], Awaitable[dict]],
    refresh: Callable[[], Awaitable[dict]],
    seed: int = 0,
    frozen_unit_ids: frozenset[str] | set[str] = frozenset(),
) -> dict:
    """At most two distinct attempts per unit, using audited caller callbacks.

    Refresh must return facade-projected state, refreshing map before entities.
    No end_turn is issued here. The owning controller closes the turn and applies
    the existing bounded standing-order completeness repair, if necessary.

    ``frozen_unit_ids`` is explicit trusted live-coordinator knowledge: only the
    phase-opening owned roster untouched by any unit action. Observed zero alone
    never grants this exception. After an attempted action it is consumed.
    """
    kwargs = {"directive": directive, "player_id": player_id, "match_id": match_id,
              "agent_id": agent_id, "turn": turn, "seed": seed}
    plan = plan_scouting(snapshot, **kwargs, frozen_unit_ids=frozen_unit_ids)
    untouched_frozen = set(frozen_unit_ids)
    plan["execution"] = []
    # Recompute each unit's candidates from the preceding action's fresh view.
    # The initial roster bounds the whole pass; newly created units wait next turn.
    for initial in plan["decisions"]:
        uid = initial["unit_id"]
        before = _observed(snapshot, uid, player_id)
        if not before.get("owned") or (before["movement"] <= 0 and uid not in untouched_frozen):
            continue
        units, tiles = _snapshot(snapshot)
        current_unit = next(unit for unit in units if unit["unit_id"] == uid)
        # Keep the original validated directive hash for the whole pass even
        # when a prior override consumed its settler. Only this owned unit's
        # decision is recomputed; no repeated all-roster planning or wire reads.
        decision = _decision(current_unit, units, tiles, plan["directive"], plan["identity"],
                             plan["directive_sha256"], uid in untouched_frozen)
        if decision["selected"] is None:
            continue
        action = decision["selected"]
        tried = set()
        for attempt in range(MAX_ATTEMPTS):
            args = dict(action["args"])
            tried.add(args.get("dest"))
            args["idempotency_key"] = "scout-" + _digest([
                plan["identity"], plan["directive_sha256"], uid, action["action"], args])[:40]
            result = await execute(action["action"], args)
            untouched_frozen.discard(uid)
            # Never derive position from the request or raw live ACT x,y detail.
            snapshot = await refresh()
            after = _observed(snapshot, uid, player_id)
            if not isinstance(result, dict) or result.get("status") not in {"accepted", "rejected"}:
                raise ValueError("scouting action returned no canonical facade status")
            accepted = result["status"] == "accepted"
            record = {"unit_id": uid, "attempt": attempt + 1, "decision": decision,
                      "action": action["action"], "args": args,
                      "status": result["status"], "rejection": result.get("rejection"),
                      "before": before, "after": after,
                      "observation_changed": after != before,
                      "outcome": "submitted_observation_unchanged" if accepted and after == before
                      else "submitted_observation_changed" if accepted else "rejected"}
            plan["execution"].append(record)
            if (accepted or after != before or not after.get("owned")
                    or after.get("movement", 0) <= 0
                    or action["action"] != "move_unit" or decision["override"]
                    or result.get("rejection") not in _SAFE_REJECTIONS):
                break
            # A rejection is the only retry permit. Recompute from refreshed
            # observations, exclude attempted destinations, and try one alternative.
            units, tiles = _snapshot(snapshot)
            current_unit = next(unit for unit in units if unit["unit_id"] == uid)
            decision = _decision(current_unit, units, tiles, plan["directive"], plan["identity"],
                                 plan["directive_sha256"], False)
            alternatives = [row for row in decision["candidates"]
                            if row["excluded"] is None and row["dest"] not in tried]
            if not alternatives:
                break
            candidate = min(alternatives, key=lambda row: (-row["score"], row["dest"]))
            for row in decision["candidates"]:
                row["probability"] = float(row is candidate)
            action = {"action": "move_unit", "args": {"unit_id": uid, "dest": candidate["dest"]},
                      "reason": "bounded_alternative_after_explicit_rejection"}
            decision = {**decision, "selected": action}
    return plan
