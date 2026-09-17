"""Flash roster proposal: fixed JSON contract + fail-soft validator.

Every accepted entry maps 1:1 onto a ``DOCTRINES`` record
(src/civ_arena/agents/scripted.py), so implementing a proposed roster is
transcription, not design. Parse/validate failures degrade to an empty
roster with recorded drop reasons (the ``planner.proposer`` pattern lifted
to the research boundary) — a bad flash response costs a retry, never a
crash, and nothing undocumented ever reaches the game code.
"""

from __future__ import annotations

import re
from typing import Any

from civ_arena.game.sim.state import BUILDINGS, TECHS, UNIT_TYPES

DOCTRINE_ID_RE = re.compile(r"^[a-z][a-z0-9_]{2,23}$")
RESERVED_IDS = {"llm", "planner", "adaptive"}
PRODUCIBLE = set(UNIT_TYPES) | set(BUILDINGS)
TECH_ORDER_OK = "prereq-not-in-earlier-position"
EXPECTED_ROSTER_SIZE = 4

PROPOSER_CONTRACT = """{
  "roster": [
    {
      "doctrine_id": "siege_turtler",
      "research": ["MINING", "MASONRY"],
      "march": false,
      "fortify_idle": true,
      "build_order": ["WALLS", "ARCHER"],
      "purchase_pref": ["MONUMENT", "WARRIOR"],
      "max_cities": 3,
      "aggression": 3,
      "expand_ring": 2,
      "thesis": "one sentence: what game state this doctrine is trying to reach",
      "expected_signature": "its mid-game score trajectory (cities/pop/techs/units/gold)",
      "failure_mode": "the opponent behavior or game state that beats it"
    }
  ]
}"""

_RULES = """Rules:
- exactly 4 doctrines, distinct doctrine_id (lowercase snake_case)
- doctrine_id must not be llm, planner, or adaptive (reserved policies)
- research: an ordered list drawn from TECHS (prereqs must appear earlier
  in the list); the doctrine re-issues the FIRST entry still available
- build_order / purchase_pref: items drawn from units and buildings the
  sim can produce
- max_cities: 1..6; aggression: 0..6 attacks per turn (default 2);
  expand_ring: 1..5 tiles of settler push (default 2)
- march / fortify_idle: booleans
- thesis / expected_signature / failure_mode: non-empty strings
Known TECHS: {techs}
Known producible items: {items}"""


def proposer_rules() -> str:
    items = sorted(PRODUCIBLE - {"SLINGER", "BUILDER"})
    return _RULES.format(techs=sorted(TECHS), items=items)


def _research_ok(research: Any) -> str | None:
    if not isinstance(research, list) or not research:
        return "research must be a non-empty list"
    seen: list[str] = []
    for tech in research:
        if tech not in TECHS:
            return f"unknown tech {tech!r}"
        if tech in seen:
            return f"duplicate tech {tech!r}"
        missing = [p for p in TECHS[tech]["prereq"] if p not in seen]
        if missing:
            return f"{tech}: prereqs {missing} not earlier in research order"
        seen.append(tech)
    return None


def _item_list_ok(items: Any, field: str) -> str | None:
    if not isinstance(items, list) or not items:
        return f"{field} must be a non-empty list"
    for item in items:
        if item not in PRODUCIBLE:
            return f"{field}: unknown item {item!r}"
        if item in ("SLINGER", "BUILDER"):
            return f"{field}: {item} is not player-producible"
    return None


def _validate_entry(entry: Any) -> str | None:
    """None = accepted; a string = the drop reason."""
    if not isinstance(entry, dict):
        return "entry is not an object"
    doctrine_id = entry.get("doctrine_id")
    if not isinstance(doctrine_id, str) or not DOCTRINE_ID_RE.match(doctrine_id):
        return f"bad doctrine_id {doctrine_id!r}"
    if doctrine_id in RESERVED_IDS:
        return f"reserved doctrine_id {doctrine_id!r}"
    reason = _research_ok(entry.get("research"))
    if reason:
        return f"{doctrine_id}: {reason}"
    for field in ("march", "fortify_idle"):
        if not isinstance(entry.get(field), bool):
            return f"{doctrine_id}: {field} must be a boolean"
    reason = _item_list_ok(entry.get("build_order"), "build_order")
    if reason:
        return f"{doctrine_id}: {reason}"
    reason = _item_list_ok(entry.get("purchase_pref"), "purchase_pref")
    if reason:
        return f"{doctrine_id}: {reason}"
    for field, low, high in (("max_cities", 1, 6), ("aggression", 0, 6),
                             ("expand_ring", 1, 5)):
        value = entry.get(field, 2 if field != "max_cities" else None)
        if not isinstance(value, int) or isinstance(value, bool):
            return f"{doctrine_id}: {field} must be an integer"
        if field == "max_cities" and value is None:
            return f"{doctrine_id}: max_cities is required"
        if not low <= value <= high:
            return f"{doctrine_id}: {field}={value} outside {low}..{high}"
    for field in ("thesis", "expected_signature", "failure_mode"):
        if not isinstance(entry.get(field), str) or not entry[field].strip():
            return f"{doctrine_id}: {field} must be a non-empty string"
    return None


def validate_proposal(data: Any) -> tuple[list[dict[str, Any]], list[str]]:
    """(accepted, dropped-reasons). Fails soft to an empty roster unless
    EXACTLY the expected number of well-formed doctrines survive."""
    dropped: list[str] = []
    roster = data.get("roster") if isinstance(data, dict) else None
    if not isinstance(roster, list):
        return [], ["response carried no roster list"]
    accepted: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for entry in roster:
        reason = _validate_entry(entry)
        if reason:
            dropped.append(reason)
            continue
        doctrine_id = entry["doctrine_id"]
        if doctrine_id in seen_ids:
            dropped.append(f"duplicate doctrine_id {doctrine_id!r}")
            continue
        seen_ids.add(doctrine_id)
        accepted.append(entry)
    if len(accepted) != EXPECTED_ROSTER_SIZE:
        dropped.append(
            f"expected {EXPECTED_ROSTER_SIZE} valid doctrines, got "
            f"{len(accepted)} — roster rejected wholesale")
        return [], dropped
    return accepted, dropped
