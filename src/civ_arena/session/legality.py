"""Policy-level pre-commit legality: schema, whitelist, ownership.

These checks run in the referee BEFORE the adapter is reached, so hostile
probes (foreign unit/city ids, malformed args, unknown tools) never touch the
game engine. Domain legality (movement, prereqs, gold) stays in the adapter's
rules layer — defense in depth: the adapter re-checks ownership and phase.
"""

from __future__ import annotations

from typing import Any

from civ_arena.game.adapter import RejectionReason

# The complete agent-visible action surface.
KNOWN_ACTION_TOOLS = frozenset({
    "move_unit", "attack", "fortify", "found_city", "set_research",
    "set_city_production", "purchase", "end_turn",
})

_ARG_SPEC: dict[str, dict[str, type]] = {
    "move_unit": {"unit_id": str, "dest": str},
    "attack": {"unit_id": str, "target_id": str},
    "fortify": {"unit_id": str},
    "found_city": {"unit_id": str},
    "set_research": {"tech_id": str},
    "set_city_production": {"city_id": str, "item_id": str},
    "purchase": {"city_id": str, "item_id": str},
    "end_turn": {},
}

_OPTIONAL_STR_ARGS = frozenset({("found_city", "name"), ("set_city_production", "dest")})


def validate_args(tool: str, args: dict[str, Any]) -> RejectionReason | None:
    if tool not in KNOWN_ACTION_TOOLS:
        return RejectionReason.TOOL_UNKNOWN
    spec = _ARG_SPEC[tool]
    for key, type_ in spec.items():
        value = args.get(key)
        if not isinstance(value, type_) or (type_ is str and not value.strip()):
            return RejectionReason.ARGS_INVALID
    allowed = set(spec) | {
        k for t, k in _OPTIONAL_STR_ARGS if t == tool
    }
    for key in args:
        if key not in allowed:
            return RejectionReason.ARGS_INVALID
    if tool == "set_city_production":
        from civ_arena.game.civ6.productive_native import validate
        item, dest = args["item_id"], args.get("dest")
        if item.startswith(("DISTRICT_", "PROJECT_")):
            try:
                validate(item, dest)
            except ValueError:
                return RejectionReason.ARGS_INVALID
        elif "dest" in args:
            return RejectionReason.ARGS_INVALID
    name = args.get("name")
    if name is not None and not (1 <= len(name.strip()) <= 23):
        return RejectionReason.ARGS_INVALID
    return None


def ownership_reason(
    omni_units: Any,
    omni_cities: Any,
    player_id: int,
    tool: str,
    args: dict[str, Any],
) -> RejectionReason | None:
    """Ownership/existence on an omniscient peek (referee scope, never projected)."""
    units = omni_units if isinstance(omni_units, list) else list(omni_units.values())
    cities = omni_cities if isinstance(omni_cities, list) else list(omni_cities.values())

    unit_id = args.get("unit_id")
    if isinstance(unit_id, str):
        unit = next((u for u in units if u.get("unit_id") == unit_id), None)
        if unit is None:
            return RejectionReason.UNKNOWN_ENTITY
        if unit["owner"] != player_id:
            return RejectionReason.NOT_YOUR_UNIT

    target_id = args.get("target_id")
    if isinstance(target_id, str):
        target = next((u for u in units if u.get("unit_id") == target_id), None)
        if target is None:
            return RejectionReason.UNKNOWN_ENTITY

    city_id = args.get("city_id")
    if isinstance(city_id, str):
        city = next((c for c in cities if c.get("city_id") == city_id), None)
        if city is None:
            return RejectionReason.UNKNOWN_ENTITY
        if city["owner"] != player_id:
            return RejectionReason.NOT_YOUR_CITY
    return None
