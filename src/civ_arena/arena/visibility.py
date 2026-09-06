"""Visibility scopes + per-player projection of omniscient observations.

Adapter-agnostic: this operates on the omniscient doc any GameAdapter returns
from ``observe`` and projects it into a scope. The agent-facing boundary is
``private_player``; ``referee`` is reachable only through
``Referee.referee_snapshot()`` (a session requesting it is itself logged as
an unauthorized call).

Foreign-entity field allowlists are closed sets. Hidden entities are ABSENT,
never null-masked (an absent key leaks nothing, not even existence).
"""

from __future__ import annotations

import copy
import enum
from typing import Any

from civ_arena.game.terrain_metadata import terrain_fields

BILATERAL_STUB: dict[str, Any] = {"status": "stub", "participants": []}


class Scope(enum.StrEnum):
    REFEREE = "referee"
    PUBLIC_MATCH = "public_match"
    BILATERAL = "bilateral"
    PRIVATE_PLAYER = "private_player"


# Field allowlists for foreign (non-owned) entities.
FOREIGN_UNIT_FIELDS = frozenset(
    {"unit_id", "type", "coord", "owner_id", "hp_bucket", "strength", "ranged_strength"}
)
FOREIGN_CITY_FIELDS = frozenset({"city_id", "name", "coord", "owner_id", "hp", "population"})

# Hardcoded expectations for the no-leak checker — deliberately NOT read off
# the policy, so a leaky policy subclass cannot make the checker vacuous.
CHECKER_FOREIGN_UNIT_FIELDS = set(FOREIGN_UNIT_FIELDS)
CHECKER_FOREIGN_CITY_FIELDS = set(FOREIGN_CITY_FIELDS)


class VisibilityPolicy:
    """Projects omniscient observation docs into player-scoped observations."""

    foreign_unit_fields: frozenset[str] = FOREIGN_UNIT_FIELDS
    foreign_city_fields: frozenset[str] = FOREIGN_CITY_FIELDS

    def project(
        self,
        doc: Any,
        kind: str,
        player_id: int,
        observable: frozenset[str],
        remembered: frozenset[str],
        scope: Scope = Scope.PRIVATE_PLAYER,
    ) -> Any:
        if scope is Scope.REFEREE:
            return doc
        if scope is Scope.PUBLIC_MATCH:
            return self._public_match(doc)
        if scope is Scope.BILATERAL:
            return dict(BILATERAL_STUB)
        return self._private_player(doc, kind, player_id, observable, remembered)

    # -- scopes ---------------------------------------------------------------
    def _public_match(self, doc: Any) -> Any:
        players = doc.get("players", {}) if isinstance(doc, dict) else {}
        public_players = []
        for pid in sorted(players):
            p = players[pid]
            public_players.append({
                "player_id": p["player_id"],
                "civ_name": p["civ_name"],
                "alive": p["alive"],
            })
        return {
            "turn": doc.get("turn") if isinstance(doc, dict) else None,
            "players": public_players,
        }

    def _private_player(
        self,
        doc: Any,
        kind: str,
        player_id: int,
        observable: frozenset[str],
        remembered: frozenset[str],
    ) -> Any:
        if kind == "units":
            out = []
            for u in doc:
                projected = self._unit(u, player_id, observable)
                if projected is not None:
                    out.append(projected)
            return out
        if kind == "cities":
            out = []
            for c in doc:
                projected = self._city(c, player_id, observable, remembered)
                if projected is not None:
                    out.append(projected)
            return out
        if kind == "visible_map":
            tiles = doc.get("tiles", {})
            out = {}
            for key, tile in tiles.items():
                sees = key in observable
                if not (sees or key in remembered):
                    continue
                entry: dict[str, Any] = {"coord": key, **terrain_fields(tile)}
                if sees:
                    entry["owner_id"] = tile["owner"]
                    entry["city_id"] = tile["city"]
                out[key] = entry
            return {"turn": doc.get("turn"), "tiles": out}
        if kind == "overview":
            return {
                "turn": doc.get("turn"),
                "you": self._own_player(doc, player_id),
                "public": self._public_match(doc),
            }
        # available_research / available_production are already player-derived
        return doc

    # -- entities ----------------------------------------------------------------
    def _unit(self, u: dict[str, Any], player_id: int,
              observable: frozenset[str]) -> dict[str, Any] | None:
        key = f"{u['q']},{u['r']}"
        if u["owner"] == player_id:
            return {
                "unit_id": u["unit_id"],
                "owner_id": u["owner"],
                "type": u["type"],
                "coord": key,
                "hp": u["hp"],
                "hp_bucket": u["hp"] // 25,
                "movement": u["movement"],
                "max_movement": u["max_movement"],
                "strength": u["strength"],
                "ranged_strength": u["ranged_strength"],
                "fortified": u["fortified"],
            }
        if key not in observable:
            return None  # hidden: absent, not masked
        full = {
            "unit_id": u["unit_id"],
            "owner_id": u["owner"],
            "type": u["type"],
            "coord": key,
            "hp": u["hp"],
            "hp_bucket": u["hp"] // 25,
            "movement": u["movement"],
            "max_movement": u["max_movement"],
            "strength": u["strength"],
            "ranged_strength": u["ranged_strength"],
            "fortified": u["fortified"],
        }
        return {k: v for k, v in full.items() if k in self.foreign_unit_fields}

    def _city(self, c: dict[str, Any], player_id: int, observable: frozenset[str],
              remembered: frozenset[str]) -> dict[str, Any] | None:
        _ = remembered
        key = f"{c['q']},{c['r']}"
        if c["owner"] == player_id:
            own = copy.deepcopy(c)
            own["coord"] = key
            return own
        # Foreign cities are visible ONLY while currently observed: a
        # remembered tile must not reveal live hidden-city state (a city
        # founded after you explored the tile, or live population/HP changes
        # to a city you are not watching). Last-known snapshots are a
        # post-spike refinement.
        if key not in observable:
            return None
        full = {
            "city_id": c["city_id"],
            "name": c["name"],
            "coord": key,
            "owner_id": c["owner"],
            "hp": c["hp"],
            "population": c["population"],
            "production_queue": c["production_queue"],
            "food_bucket": c["food_bucket"],
            "production_bucket": c["production_bucket"],
            "buildings": c["buildings"],
        }
        return {k: v for k, v in full.items() if k in self.foreign_city_fields}

    def _own_player(self, doc: dict[str, Any], player_id: int) -> dict[str, Any]:
        p = doc["players"][str(player_id)]
        return {
            "player_id": p["player_id"],
            "civ_name": p["civ_name"],
            "gold": p["gold"],
            "researched": list(p["researched"]),
            "researching": p["researching"],
        }

    # NOTE: own-entity projections deep-copy nested lists so an agent
    # mutating a returned observation cannot reach live game state.


# --------------------------------------------------------------------------
# No-leak checker (independent of any policy instance)


def find_leaks(
    projected: Any,
    omniscient_units: list[dict[str, Any]],
    player_id: int,
    observable: frozenset[str],
    remembered: frozenset[str],
) -> list[str]:
    """Returns a list of human-readable leak descriptions (empty = clean).

    Used by the visibility property test and by the leak meta-test: the
    meta-test injects a deliberately leaky policy and asserts this checker
    catches it, proving the property has teeth.
    """
    leaks: list[str] = []
    units = projected if isinstance(projected, list) else []
    projected_ids = {u.get("unit_id") for u in units if isinstance(u, dict)}
    for u in omniscient_units:
        key = f"{u['q']},{u['r']}"
        visible = u["owner"] == player_id or key in observable
        if visible and u["unit_id"] not in projected_ids:
            leaks.append(f"visible unit {u['unit_id']} missing from projection")
        if not visible and u["unit_id"] in projected_ids:
            leaks.append(f"HIDDEN unit {u['unit_id']} leaked into projection")
    for entry in units:
        if not isinstance(entry, dict):
            continue
        src = next((u for u in omniscient_units if u["unit_id"] == entry.get("unit_id")), None)
        if src is None:
            leaks.append(f"projected unit {entry.get('unit_id')} does not exist")
            continue
        if src["owner"] != player_id:
            extra = set(entry) - CHECKER_FOREIGN_UNIT_FIELDS
            if extra:
                leaks.append(f"foreign unit {src['unit_id']} leaked fields: {sorted(extra)}")
    return leaks


def find_city_leaks(
    projected: Any,
    omniscient_cities: list[dict[str, Any]],
    player_id: int,
    observable: frozenset[str],
    remembered: frozenset[str],
) -> list[str]:
    _ = remembered
    leaks: list[str] = []
    cities = projected if isinstance(projected, list) else []
    projected_ids = {c.get("city_id") for c in cities if isinstance(c, dict)}
    for c in omniscient_cities:
        key = f"{c['q']},{c['r']}"
        visible = c["owner"] == player_id or key in observable
        if visible and c["city_id"] not in projected_ids:
            leaks.append(f"visible city {c['city_id']} missing from projection")
        if not visible and c["city_id"] in projected_ids:
            leaks.append(f"HIDDEN city {c['city_id']} leaked into projection")
    for entry in cities:
        if not isinstance(entry, dict):
            continue
        src = next((c for c in omniscient_cities if c["city_id"] == entry.get("city_id")), None)
        if src is None:
            leaks.append(f"projected city {entry.get('city_id')} does not exist")
            continue
        if src["owner"] != player_id:
            extra = set(entry) - CHECKER_FOREIGN_CITY_FIELDS
            if extra:
                leaks.append(f"foreign city {src['city_id']} leaked fields: {sorted(extra)}")
    return leaks
