"""Strict, bounded strategy JSON. Entity ownership comes from projected observations."""
from __future__ import annotations

import copy
import math
import re
from typing import Any

UNIT_TYPES = ("SCOUT", "WARRIOR", "SLINGER", "ARCHER", "SPEARMAN")
WEIGHTS = {"unexplored": 4.0, "distance": 0.25, "threat": 5.0, "terrain": 1.0}
DEFAULT_DIRECTIVE = {
    "version": 1,
    "scouting": {"policy": "balanced", "selection": "seeded", "temperature": 0.5,
                 "unit_types": ["SCOUT", "WARRIOR"], "weights": WEIGHTS},
    "research_preferences": [], "production_preferences": [], "tactical_overrides": [],
}
_ID = {"type": "string", "minLength": 1, "maxLength": 96,
       "pattern": r"^[A-Za-z0-9_:.-]+$"}
_PREFERENCE = {"type": "array", "maxItems": 16, "uniqueItems": True,
               "items": {"type": "string", "pattern": "^[A-Z][A-Z0-9_]{0,63}$"}}
DIRECTIVE_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "properties": {
        "version": {"type": "integer", "enum": [1]},
        "scouting": {
            "type": "object", "additionalProperties": False,
            "properties": {
                "policy": {"type": "string", "enum": ["cautious", "balanced", "explore"]},
                "selection": {"type": "string", "enum": ["best", "seeded"]},
                "temperature": {"type": "number", "minimum": 0.05, "maximum": 2},
                "unit_types": {"type": "array", "maxItems": len(UNIT_TYPES),
                               "uniqueItems": True,
                               "items": {"type": "string", "enum": list(UNIT_TYPES)}},
                "weights": {"type": "object", "additionalProperties": False,
                            "properties": {key: {"type": "number", "minimum": 0,
                                                 "maximum": 10} for key in WEIGHTS}},
            },
        },
        "research_preferences": _PREFERENCE,
        "production_preferences": _PREFERENCE,
        "tactical_overrides": {
            "type": "array", "maxItems": 8,
            "items": {"type": "object", "additionalProperties": False,
                      "required": ["unit_id", "action"],
                      "properties": {"unit_id": _ID, "target_id": _ID,
                                     "dest": {"type": "string", "maxLength": 15},
                                     "action": {"type": "string", "enum": [
                                         "hold", "move", "attack", "found_city"]}}},
        },
    },
}


def _object(value: Any, keys: set[str], label: str) -> dict:
    if not isinstance(value, dict) or not set(value) <= keys:
        raise ValueError(f"{label}: expected object with only {sorted(keys)}")
    return value


def _strings(value: Any, *, limit: int, pattern: str, label: str) -> list[str]:
    if (not isinstance(value, list) or len(value) > limit
            or any(not isinstance(item, str) or not re.fullmatch(pattern, item) for item in value)
            or len(set(value)) != len(value)):
        raise ValueError(f"{label}: invalid, duplicate, or excessive entries")
    return list(value)


def coordinate(value: Any) -> tuple[int, int]:
    if not isinstance(value, str) or not re.fullmatch(r"-?\d{1,6},-?\d{1,6}", value):
        raise ValueError("coordinate must be canonical bounded axial q,r")
    q, r = map(int, value.split(","))
    if value != f"{q},{r}":
        raise ValueError("coordinate must be canonical bounded axial q,r")
    return q, r


def validate_directive(value: Any, *, player_id: int, owned_unit_ids: set[str]) -> dict:
    """Normalize defaults; reject extra keys, non-finite numbers and foreign overrides.

    Overrides are single-turn orders. The controller expires them after that turn;
    this validator never infers ownership by decoding an opaque entity identifier.
    """
    if type(player_id) is not int or player_id < 0:
        raise ValueError("invalid player identity")
    doc = _object(value, set(DEFAULT_DIRECTIVE), "directive")
    out = copy.deepcopy(DEFAULT_DIRECTIVE)
    version = doc.get("version", 1)
    if type(version) is not int or version != 1:
        raise ValueError("unsupported directive version")
    scout = _object(doc.get("scouting", {}), set(out["scouting"]), "scouting")
    for key, allowed in (("policy", {"cautious", "balanced", "explore"}),
                         ("selection", {"best", "seeded"})):
        if key in scout:
            if not isinstance(scout[key], str) or scout[key] not in allowed:
                raise ValueError(f"invalid scouting {key}")
            out["scouting"][key] = scout[key]
    temp = scout.get("temperature", 0.5)
    if type(temp) not in (int, float) or not math.isfinite(temp) or not 0.05 <= temp <= 2:
        raise ValueError("temperature must be finite and in [0.05, 2]")
    out["scouting"]["temperature"] = float(temp)
    if "unit_types" in scout:
        types = _strings(scout["unit_types"], limit=len(UNIT_TYPES),
                         pattern="|".join(UNIT_TYPES), label="unit_types")
        out["scouting"]["unit_types"] = sorted(types)
    weights = _object(scout.get("weights", {}), set(WEIGHTS), "weights")
    for key, weight in weights.items():
        if type(weight) not in (int, float) or not math.isfinite(weight) or not 0 <= weight <= 10:
            raise ValueError("weights must be finite and in [0, 10]")
        out["scouting"]["weights"][key] = float(weight)
    for field in ("research_preferences", "production_preferences"):
        out[field] = _strings(doc.get(field, []), limit=16, pattern=r"[A-Z][A-Z0-9_]{0,63}",
                              label=field)
    overrides = doc.get("tactical_overrides", [])
    if not isinstance(overrides, list) or len(overrides) > 8:
        raise ValueError("at most eight tactical overrides are allowed")
    seen = set()
    for override in overrides:
        override = _object(override, {"unit_id", "action", "dest", "target_id"}, "override")
        uid, action = override.get("unit_id"), override.get("action")
        if not isinstance(uid, str) or not re.fullmatch(r"[A-Za-z0-9_:.-]{1,96}", uid):
            raise ValueError("invalid override unit identity")
        if uid not in owned_unit_ids or uid in seen:
            raise ValueError("override must reference a distinct currently owned unit")
        if not isinstance(action, str) or action not in {"hold", "move", "attack", "found_city"}:
            raise ValueError("invalid tactical action")
        expected = {"unit_id", "action"} | ({"dest"} if action == "move" else
                                            {"target_id"} if action == "attack" else set())
        if set(override) != expected:
            raise ValueError("tactical arguments do not match action")
        if action == "move":
            coordinate(override["dest"])
        if action == "attack" and (not isinstance(override["target_id"], str)
                                  or not re.fullmatch(r"[A-Za-z0-9_:.-]{1,96}",
                                                      override["target_id"])):
            raise ValueError("invalid tactical target identity")
        seen.add(uid)
        out["tactical_overrides"].append(dict(override))
    out["tactical_overrides"].sort(key=lambda order: order["unit_id"])
    return out
