"""Strict, bounded strategy JSON. Entity ownership comes from projected observations."""
from __future__ import annotations

import copy
import math
import re
from typing import Any

UNIT_TYPES = ("SCOUT", "WARRIOR", "SLINGER", "ARCHER", "SPEARMAN")
MAX_UNIT_TARGETS = 16
MAX_UNIT_TARGET = 32
WEIGHTS = {"unexplored": 4.0, "distance": 0.25, "threat": 5.0, "terrain": 1.0}
DEFAULT_DIRECTIVE = {
    "version": 1,
    "scouting": {"policy": "balanced", "selection": "seeded", "temperature": 0.5,
                 "unit_types": ["SCOUT", "WARRIOR"], "weights": WEIGHTS},
    "research_preferences": [], "production_preferences": [],
    "tactical_overrides": [],
}
_ID = {"type": "string", "minLength": 1, "maxLength": 96,
       "pattern": r"^[A-Za-z0-9_:.-]+$"}
_PREFERENCE = {"type": "array", "maxItems": 16, "uniqueItems": True,
               "items": {"type": "string", "pattern": "^[A-Z][A-Z0-9_]{0,63}$"}}
_DEST = {"type": "string", "maxLength": 15,
         "pattern": r"^(?:0|-?[1-9][0-9]{0,5}),(?:0|-?[1-9][0-9]{0,5})$"}
_TACTICAL_CASES = []
for _action in ("hold", "move", "attack", "found_city"):
    _properties = {"unit_id": _ID, "action": {"type": "string", "enum": [_action]}}
    if _action == "move":
        _properties["dest"] = _DEST
    elif _action == "attack":
        _properties["target_id"] = _ID
    _TACTICAL_CASES.append({"type": "object", "additionalProperties": False,
                           "required": list(_properties), "properties": _properties})
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
                               "description": "Persistent scouting roles; include SCOUT to deploy "
                                   "built scouts. Explicit omission holds them.",
                               "uniqueItems": True,
                               "items": {"type": "string", "enum": list(UNIT_TYPES)}},
                "weights": {"type": "object", "additionalProperties": False,
                            "properties": {key: {"type": "number", "minimum": 0,
                                                 "maximum": 10} for key in WEIGHTS}},
            },
        },
        "research_preferences": _PREFERENCE,
        "production_preferences": {**_PREFERENCE, "description":
            "Recurring preference ranking for each empty city queue, subject to unit targets."},
        "unit_targets": {
            "type": "object", "maxProperties": MAX_UNIT_TARGETS,
            "propertyNames": {"pattern": "^[A-Z][A-Z0-9_]{0,63}$"},
            "additionalProperties": {"type": "integer", "minimum": 0,
                                     "maximum": MAX_UNIT_TARGET},
            "description": "Optional empire-wide desired owned plus queued counts by exact unit "
                "item_id. Zero disables new production; omitted IDs use conservative defaults.",
        },
        "tactical_overrides": {
            "type": "array", "maxItems": 8,
            "items": {"type": "object", "oneOf": _TACTICAL_CASES},
        },
    },
}


# These tokens are the complete public diagnostic vocabulary. Never include
# a model-supplied property name, entity ID, value or exception message.
_VALIDATION_CODES = frozenset({
    "expected_object", "unknown_property", "invalid_list", "invalid_item", "duplicate_item",
    "invalid_player", "unsupported_version", "invalid_enum", "invalid_temperature",
    "invalid_weight", "invalid_unit_targets", "invalid_unit_target", "invalid_overrides",
    "invalid_unit_id", "unit_not_owned", "duplicate_unit_override", "invalid_action",
    "action_arguments", "invalid_destination", "invalid_target_id",
})
_VALIDATION_FIELDS = frozenset({
    "version", "scouting", "policy", "selection", "temperature", "unit_types", "weights",
    "unexplored", "distance", "threat", "terrain", "research_preferences",
    "production_preferences", "unit_targets", "tactical_overrides", "unit_id", "action",
    "dest", "target_id",
})


class DirectiveValidationError(ValueError):
    """Existing ValueError contract plus allowlisted, value-free diagnostics."""

    def __init__(self, message: str, *, code: str, path: tuple = ()):
        super().__init__(message)
        self.code = code
        self.path = path


def validation_diagnostic(error: BaseException) -> dict:
    """Only fixed schema tokens and bounded array indexes may reach audit/model."""
    try:
        if isinstance(error, DirectiveValidationError):
            code, path = error.code, error.path
            if (type(code) is str and code in _VALIDATION_CODES and type(path) is tuple
                    and len(path) <= 4 and all(
                        type(part) is str and part in _VALIDATION_FIELDS
                        or type(part) is int and 0 <= part < 16 for part in path)):
                return {"code": code, "path": list(path)}
    except Exception:
        # Broken metadata is still an unknown validation failure. Cancellation
        # and other BaseException controls must propagate unchanged.
        pass
    return {"code": "unknown", "path": []}


def _object(value: Any, keys: set[str], label: str, *, path: tuple = ()) -> dict:
    if not isinstance(value, dict) or not set(value) <= keys:
        raise DirectiveValidationError(f"{label}: expected object with only {sorted(keys)}",
            code="expected_object" if not isinstance(value, dict) else "unknown_property",
            path=path)
    return value


def _strings(value: Any, *, limit: int, pattern: str, label: str, path: tuple) -> list[str]:
    message = f"{label}: invalid, duplicate, or excessive entries"
    if not isinstance(value, list) or len(value) > limit:
        raise DirectiveValidationError(message, code="invalid_list", path=path)
    for index, item in enumerate(value):
        if not isinstance(item, str) or not re.fullmatch(pattern, item):
            raise DirectiveValidationError(message, code="invalid_item", path=(*path, index))
    if len(set(value)) != len(value):
        raise DirectiveValidationError(message, code="duplicate_item", path=path)
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
        raise DirectiveValidationError("invalid player identity", code="invalid_player")
    doc = _object(value, set(DEFAULT_DIRECTIVE) | {"unit_targets"}, "directive")
    out = copy.deepcopy(DEFAULT_DIRECTIVE)
    version = doc.get("version", 1)
    # JSON Schema integer accepts mathematically integral JSON numbers such as
    # 1.0. Normalize that equivalent wire spelling; booleans remain forbidden.
    if type(version) not in (int, float) or version != 1:
        raise DirectiveValidationError("unsupported directive version",
                                       code="unsupported_version", path=("version",))
    scout = _object(doc.get("scouting", {}), set(out["scouting"]), "scouting",
                    path=("scouting",))
    for key, allowed in (("policy", {"cautious", "balanced", "explore"}),
                         ("selection", {"best", "seeded"})):
        if key in scout:
            if not isinstance(scout[key], str) or scout[key] not in allowed:
                raise DirectiveValidationError(f"invalid scouting {key}",
                                               code="invalid_enum", path=("scouting", key))
            out["scouting"][key] = scout[key]
    temp = scout.get("temperature", 0.5)
    if type(temp) not in (int, float) or not math.isfinite(temp) or not 0.05 <= temp <= 2:
        raise DirectiveValidationError("temperature must be finite and in [0.05, 2]",
                                       code="invalid_temperature", path=("scouting", "temperature"))
    out["scouting"]["temperature"] = float(temp)
    if "unit_types" in scout:
        types = _strings(scout["unit_types"], limit=len(UNIT_TYPES),
                         pattern="|".join(UNIT_TYPES), label="unit_types",
                         path=("scouting", "unit_types"))
        out["scouting"]["unit_types"] = sorted(types)
    weights = _object(scout.get("weights", {}), set(WEIGHTS), "weights",
                      path=("scouting", "weights"))
    for key, weight in weights.items():
        if type(weight) not in (int, float) or not math.isfinite(weight) or not 0 <= weight <= 10:
            raise DirectiveValidationError("weights must be finite and in [0, 10]",
                                       code="invalid_weight", path=("scouting", "weights", key))
        out["scouting"]["weights"][key] = float(weight)
    for field in ("research_preferences", "production_preferences"):
        out[field] = _strings(doc.get(field, []), limit=16, pattern=r"[A-Z][A-Z0-9_]{0,63}",
                              label=field, path=(field,))
    targets = doc.get("unit_targets", {})
    if (not isinstance(targets, dict) or len(targets) > MAX_UNIT_TARGETS
            or any(not isinstance(key, str) or not re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", key)
                   for key in targets)):
        raise DirectiveValidationError("unit_targets requires at most sixteen exact unit item IDs",
                                       code="invalid_unit_targets", path=("unit_targets",))
    # Empty/omitted extensions preserve the historical normalized directive and
    # therefore its existing scouting seed hash. Nonempty targets are explicit.
    if targets:
        out["unit_targets"] = {}
    for key, target in sorted(targets.items()):
        if (type(target) not in (int, float) or not 0 <= target <= MAX_UNIT_TARGET
                or not math.isfinite(target) or target != int(target)):
            raise DirectiveValidationError("unit_targets values must be integers in [0, 32]",
                                           code="invalid_unit_target", path=("unit_targets",))
        out["unit_targets"][key] = int(target)
    overrides = doc.get("tactical_overrides", [])
    if not isinstance(overrides, list) or len(overrides) > 8:
        raise DirectiveValidationError("at most eight tactical overrides are allowed",
                                       code="invalid_overrides", path=("tactical_overrides",))
    seen = set()
    for index, override in enumerate(overrides):
        path = ("tactical_overrides", index)
        override = _object(override, {"unit_id", "action", "dest", "target_id"}, "override",
                           path=path)
        uid, action = override.get("unit_id"), override.get("action")
        if not isinstance(uid, str) or not re.fullmatch(r"[A-Za-z0-9_:.-]{1,96}", uid):
            raise DirectiveValidationError("invalid override unit identity",
                                           code="invalid_unit_id", path=(*path, "unit_id"))
        if uid not in owned_unit_ids or uid in seen:
            raise DirectiveValidationError(
                "override must reference a distinct currently owned unit",
                code="unit_not_owned" if uid not in owned_unit_ids else "duplicate_unit_override",
                path=(*path, "unit_id"))
        if not isinstance(action, str) or action not in {"hold", "move", "attack", "found_city"}:
            raise DirectiveValidationError("invalid tactical action",
                                           code="invalid_action", path=(*path, "action"))
        expected = {"unit_id", "action"} | ({"dest"} if action == "move" else
                                            {"target_id"} if action == "attack" else set())
        if set(override) != expected:
            raise DirectiveValidationError("tactical arguments do not match action",
                                           code="action_arguments", path=path)
        if action == "move":
            try:
                coordinate(override["dest"])
            except ValueError:
                raise DirectiveValidationError("coordinate must be canonical bounded axial q,r",
                    code="invalid_destination", path=(*path, "dest")) from None
        if action == "attack" and (not isinstance(override["target_id"], str)
                                  or not re.fullmatch(r"[A-Za-z0-9_:.-]{1,96}",
                                                      override["target_id"])):
            raise DirectiveValidationError("invalid tactical target identity",
                                           code="invalid_target_id", path=(*path, "target_id"))
        seen.add(uid)
        out["tactical_overrides"].append(dict(override))
    out["tactical_overrides"].sort(key=lambda order: order["unit_id"])
    return out
