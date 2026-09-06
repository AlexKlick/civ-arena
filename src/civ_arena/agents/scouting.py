"""Seeded frontier heuristics over projected observations, never a legality oracle.

Only the existing audited facade executes commands. Its adapter owns movement
restore/refreeze and engine legality. A submitted request with unchanged observed
position is not retried: the engine may still be applying it.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
import random
from collections.abc import Awaitable, Callable
from typing import Any

from civ_arena.agents.recovery import RecoveryPolicy, observed_health
from civ_arena.agents.strategy_directive import coordinate, validate_directive
from civ_arena.game.sim.state import hex_dist, neighbors, tiles_within

MAX_UNITS = 16
MAX_ATTEMPTS = 2
NONPROGRESS_TTL = 3
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


class ScoutingFeedback:
    """Small fresh-only memory of observed non-progress, never engine illegality.

    Only remember attempts after the controller has closed their issuing turn.
    A subsequent consecutive own turn must still observe the original coordinate
    before a candidate is suppressed. No wall clocks, model calls or extra reads.
    """

    def __init__(self):
        self._pending: dict[str, dict] = {}
        self._cooldowns: dict[str, dict] = {}

    def begin_turn(self, snapshot: dict, *, player_id: int, turn: int,
                   completed_turn: int) -> dict:
        units, _ = _snapshot(snapshot)
        owned = {u["unit_id"]: u["coord"] for u in units if _owner(u) == player_id}
        audit = {"confirmed": [], "forgotten": [], "expired": [], "suppressed": {},
                 "ttl_own_turns": NONPROGRESS_TTL, "unit_limit": MAX_UNITS,
                 "destinations_per_unit": 6}
        for uid in sorted(set(self._pending) | set(self._cooldowns)):
            pending = self._pending.pop(uid, None)
            record = self._cooldowns.get(uid)
            origin = record["origin"] if record else pending["origin"]
            if owned.get(uid) != origin:
                self._cooldowns.pop(uid, None)
                audit["forgotten"].append({"unit_id": uid, "reason": "lost_or_changed_origin"})
                continue
            if pending:
                if completed_turn == turn - 1 and pending["turn"] == completed_turn:
                    record = self._cooldowns.setdefault(uid, {"origin": origin,
                                                              "destinations": {}})
                    receipt = {"confirmed_turn": turn, "expires_turn": turn + NONPROGRESS_TTL,
                               "issued_turn": pending["turn"]}
                    record["destinations"][pending["dest"]] = receipt
                    audit["confirmed"].append({"unit_id": uid, "origin": origin,
                                               "dest": pending["dest"], **receipt})
                else:
                    audit["forgotten"].append({"unit_id": uid, "reason": "unconfirmed_turn_gap"})
            if record:
                for dest, receipt in list(record["destinations"].items()):
                    if turn >= receipt["expires_turn"]:
                        del record["destinations"][dest]
                        audit["expired"].append({"unit_id": uid, "dest": dest})
                if not record["destinations"]:
                    self._cooldowns.pop(uid, None)
        audit["suppressed"] = copy.deepcopy(self._cooldowns)
        return audit

    def remember_completed(self, graph: dict, *, turn: int) -> list[dict]:
        """Stage accepted unchanged-position attempts only after successful closure."""
        pending = {}
        for row in graph.get("execution", []):
            before, after = row["before"], row["after"]
            if (row["action"] != "move_unit" or row["status"] != "accepted"
                    or not before.get("owned") or not after.get("owned")
                    or before.get("coord") != after.get("coord")):
                continue
            dest, origin = row["args"]["dest"], before["coord"]
            if hex_dist(coordinate(origin), coordinate(dest)) != 1:
                continue
            pending[row["unit_id"]] = {"origin": origin, "dest": dest, "turn": turn}
        # Prefer this turn's bounded action roster, then retain older cooldowns
        # deterministically. At most sixteen units and six adjacent targets each.
        retain = sorted(pending)[:MAX_UNITS]
        retain += sorted(set(self._cooldowns) - set(retain))[:MAX_UNITS - len(retain)]
        self._pending = {uid: pending[uid] for uid in retain if uid in pending}
        self._cooldowns = {uid: self._cooldowns[uid] for uid in retain if uid in self._cooldowns}
        return [{"unit_id": uid, **row} for uid, row in sorted(self._pending.items())]


def _decision(unit, units, tiles, directive, identity, directive_hash, opening_frozen,
              nonprogress, recovery=None, recovery_policy=None):
    uid, origin = unit["unit_id"], coordinate(unit["coord"])
    seed = _digest(["frontier-v1", identity, uid, directive_hash])
    decision = {"unit_id": uid, "origin": unit["coord"], "movement": _remaining(unit),
                "seed": seed, "candidates": [], "selected": None, "override": False,
                "movement_authority": "opening_frozen_allowance_unobserved" if opening_frozen
                else "observed_remaining"}
    if decision["movement"] <= 0 and not opening_frozen:
        return {**decision, "reason": "movement_spent"}
    active_recovery = recovery.get(uid) if recovery else None
    health = observed_health(unit)
    # A later refreshed read may be unavailable or newly damaged even when the
    # opening observation was healthy. Never allow that to bypass the hold.
    if active_recovery is None and recovery_policy is not None and (
            not health["health_valid"] or health["hp"] * 100
            < health["max_hp"] * recovery_policy.enter_below_percent):
        active_recovery = {"unit_id": uid, **health, "observed_turn": identity["turn"],
            "started_turn": identity["turn"], "recovering": health["health_valid"],
            "state": "recovering" if health["health_valid"] else "health_unavailable",
            "no_gain_turns": 0, "no_gain_reported": False}
    override = next((row for row in directive["tactical_overrides"] if row["unit_id"] == uid),
                    None)
    if override is not None:
        decision["override"] = True
        if active_recovery:
            decision["recovery_resolution"] = "one_turn_tactical_interruption"
            decision["recovery"] = copy.deepcopy(active_recovery)
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
    if active_recovery:
        decision["recovery"] = copy.deepcopy(active_recovery)
        decision["health_observation"] = observed_health(unit)
        if unit.get("fortified"):
            return {**decision, "reason": "recovery_existing_standing_order"}
        selected = _hold(unit, "persistent_recovery_hold")
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
    feedback = nonprogress.get(uid, {})
    suppressed = (feedback.get("destinations", {})
                  if feedback.get("origin") == unit["coord"] else {})
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
        if excluded is None and dest in suppressed:
            excluded = "observed_nonprogress_cooldown"
        row = {"dest": dest, "excluded": excluded, "probability": 0.0}
        if dest in suppressed:
            row["nonprogress"] = copy.deepcopy(suppressed[dest])
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
                  frozen_unit_ids: frozenset[str] | set[str] = frozenset(),
                  nonprogress: dict | None = None, recovery: dict | None = None,
                  recovery_policy: RecoveryPolicy | None = None) -> dict:
    """Produce JSON audit with selection probabilities, not success probabilities."""
    units, tiles = _snapshot(snapshot)
    nonprogress = {} if nonprogress is None else nonprogress
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
    recovery = {} if recovery is None else recovery
    if (not isinstance(recovery, dict)
            or not set(recovery) <= {unit["unit_id"] for unit in owned}):
        raise ValueError("recovery overlay must reference currently owned units")
    # Explicit actions first, then recovery needing a standing order. Already
    # held units do not displace actionable tasks within the existing unit cap.
    owned.sort(key=lambda unit: (unit["unit_id"] not in overrides,
        not (unit["unit_id"] in recovery and not unit.get("fortified")),
        unit["unit_id"] in recovery and bool(unit.get("fortified")), unit["unit_id"]))
    return {"version": 1, "kind": "heuristic_frontier_plan", "identity": identity,
            "directive_sha256": digest, "directive": normalized,
            "seed_spec": "sha256(canonical_json([frontier-v1,identity,unit_id,directive_sha256]))",
            "probability_meaning": "selection_weight_not_calibrated_success_or_engine_legality",
            "knowledge": "projected_visible_or_remembered_terrain_and_current_visible_entities",
            "limits": {"units": MAX_UNITS, "attempts_per_unit": MAX_ATTEMPTS},
            "opening_frozen_unit_ids": sorted(frozen_unit_ids),
            "nonprogress_suppression": copy.deepcopy(nonprogress),
            "recovery_overlay": copy.deepcopy(recovery),
            "deferred_units": max(0, len(owned) - MAX_UNITS),
            "decisions": [_decision(unit, units, tiles, normalized, identity, digest,
                                     unit["unit_id"] in frozen_unit_ids, nonprogress, recovery,
                                     recovery_policy)
                          for unit in owned[:MAX_UNITS]]}


def _observed(snapshot, uid, player_id):
    units, _ = _snapshot(snapshot)
    unit = next((unit for unit in units if unit["unit_id"] == uid), None)
    if unit is None:
        return {"present": False}
    if _owner(unit) != player_id:
        return {"present": True, "owned": False}
    return {"present": True, "owned": True, "coord": unit["coord"],
            "movement": _remaining(unit), "fortified": unit.get("fortified", False),
            "health": observed_health(unit)}


async def run_scouting(
    snapshot: dict, *, directive: dict, player_id: int, match_id: str, agent_id: str, turn: int,
    execute: Callable[[str, dict], Awaitable[dict]],
    refresh: Callable[[], Awaitable[dict]],
    seed: int = 0,
    frozen_unit_ids: frozenset[str] | set[str] = frozenset(),
    nonprogress: dict | None = None,
    recovery: dict | None = None,
    recovery_policy: RecoveryPolicy | None = None,
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
    plan = plan_scouting(snapshot, **kwargs, frozen_unit_ids=frozen_unit_ids,
                         nonprogress=nonprogress, recovery=recovery,
                         recovery_policy=recovery_policy)
    nonprogress = plan["nonprogress_suppression"]
    recovery = plan["recovery_overlay"]
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
                             plan["directive_sha256"], uid in untouched_frozen, nonprogress,
                             recovery, recovery_policy)
        if "recovery" in decision:
            recovery.setdefault(uid, copy.deepcopy(decision["recovery"]))
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
            if action["action"] == "move_unit":
                positions_observed = before.get("owned") and after.get("owned")
                record.update(
                    position_changed=(before["coord"] != after["coord"])
                    if positions_observed else None,
                    destination_observed=(after["coord"] == args["dest"])
                    if after.get("owned") else None,
                    movement_allowance_changed=(before["movement"] != after["movement"])
                    if positions_observed else None,
                )
                if accepted:
                    record["movement_outcome"] = ("submitted_displacement_observed"
                                         if record["position_changed"] else
                                         "submitted_displacement_unconfirmed")
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
                                 plan["directive_sha256"], False, nonprogress, recovery,
                                 recovery_policy)
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
