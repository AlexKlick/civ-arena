"""Simulator state: plain JSON-able dicts with typed helpers.

Everything mutable lives in ``SimState.doc`` as plain dicts/lists so that
``to_doc``/``from_doc`` are trivial and canonicalization cannot fail.
Sentinel conventions (None is never stored in state):
- unowned tile: ``owner == -1``; tile without city: ``city == ""``
- no active phase: ``phase_player == -1``; no research: ``researching == ""``
"""

from __future__ import annotations

import copy
import random
from collections.abc import Iterator
from typing import Any

from civ_arena.canonical import rng_from_doc, rng_to_doc

MAP_RADIUS = 5

# move == 0 means impassable
TERRAIN: dict[str, dict[str, int]] = {
    "PLAINS": {"move": 1, "food": 1, "prod": 0},
    "GRASSLAND": {"move": 1, "food": 2, "prod": 0},
    "HILL": {"move": 2, "food": 0, "prod": 1},
    "DESERT": {"move": 1, "food": 0, "prod": 0},
    "MOUNTAIN": {"move": 0, "food": 0, "prod": 0},
    "COAST": {"move": 2, "food": 1, "prod": 0},
    "OCEAN": {"move": 0, "food": 0, "prod": 0},
}

UNIT_TYPES: dict[str, dict[str, int]] = {
    "SETTLER": {"strength": 0, "ranged": 0, "mv": 2, "cost": 80},
    "WARRIOR": {"strength": 20, "ranged": 0, "mv": 2, "cost": 40},
    "SPEARMAN": {"strength": 25, "ranged": 0, "mv": 2, "cost": 50},
    "ARCHER": {"strength": 15, "ranged": 25, "mv": 2, "cost": 50},
    "SCOUT": {"strength": 5, "ranged": 0, "mv": 3, "cost": 25},
}
UNIT_TECH_REQ: dict[str, str] = {"SPEARMAN": "BRONZE_WORKING", "ARCHER": "ARCHERY"}

BUILDINGS: dict[str, dict[str, int]] = {
    "MONUMENT": {"cost": 50, "gold": 2},
    "GRANARY": {"cost": 60, "gold": 1},
    "WALLS": {"cost": 70, "defhp": 50},
}

TECHS: dict[str, dict[str, Any]] = {
    "POTTERY": {"cost": 20, "prereq": []},
    "MINING": {"cost": 20, "prereq": []},
    "ANIMAL_HUSBANDRY": {"cost": 20, "prereq": []},
    "ARCHERY": {"cost": 35, "prereq": ["POTTERY"]},
    "WRITING": {"cost": 35, "prereq": ["POTTERY"]},
    "MASONRY": {"cost": 35, "prereq": ["MINING"]},
    "BRONZE_WORKING": {"cost": 40, "prereq": ["MINING"]},
    "IRRIGATION": {"cost": 40, "prereq": ["ANIMAL_HUSBANDRY"]},
}

AXIAL_DIRS = [(1, 0), (1, -1), (0, -1), (-1, 0), (-1, 1), (0, 1)]

UNIT_SIGHT = 2
FORT_BONUS = 4
HEAL_PER_TURN = 10
GROWTH_BASE = 10
GROWTH_PER_POP = 4


def tile_key(q: int, r: int) -> str:
    return f"{q},{r}"


def parse_key(key: str) -> tuple[int, int]:
    q, r = key.split(",")
    return int(q), int(r)


def hex_dist(a: tuple[int, int], b: tuple[int, int]) -> int:
    dq, dr = a[0] - b[0], a[1] - b[1]
    return (abs(dq) + abs(dr) + abs(dq + dr)) // 2


def neighbors(q: int, r: int) -> Iterator[tuple[int, int]]:
    for dq, dr in AXIAL_DIRS:
        yield q + dq, r + dr


def tiles_within(center: tuple[int, int], radius: int) -> list[tuple[int, int]]:
    out: list[tuple[int, int]] = []
    for q in range(center[0] - radius, center[0] + radius + 1):
        for r in range(center[1] - radius, center[1] + radius + 1):
            if hex_dist(center, (q, r)) <= radius:
                out.append((q, r))
    return out


def map_tiles(radius: int = MAP_RADIUS) -> list[tuple[int, int]]:
    return tiles_within((0, 0), radius)


class SimState:
    """Wrapper over the plain state document."""

    def __init__(self, doc: dict[str, Any]) -> None:
        self.doc = doc
        self.rng: random.Random = rng_from_doc(doc["rng"])

    # -- serialization --------------------------------------------------
    def to_doc(self) -> dict[str, Any]:
        """DEEP copy: a snapshot must never alias live nested state."""
        out = copy.deepcopy(self.doc)
        out["rng"] = rng_to_doc(self.rng)
        return out

    @classmethod
    def from_doc(cls, doc: dict[str, Any]) -> SimState:
        """DEEP copy: restoring N times from one snapshot must yield
        independent states — a shallow copy aliases every nested unit/city/
        tile dict, so mutating the restored state corrupts the snapshot."""
        return cls(copy.deepcopy(doc))

    # -- scalars ---------------------------------------------------------
    @property
    def turn(self) -> int:
        return self.doc["turn"]

    @turn.setter
    def turn(self, value: int) -> None:
        self.doc["turn"] = value

    @property
    def phase_player(self) -> int:
        return self.doc["phase_player"]

    @phase_player.setter
    def phase_player(self, value: int) -> None:
        self.doc["phase_player"] = value

    @property
    def phase_index(self) -> int:
        return self.doc["phase_index"]

    @phase_index.setter
    def phase_index(self, value: int) -> None:
        self.doc["phase_index"] = value

    # -- collections -------------------------------------------------------
    @property
    def tiles(self) -> dict[str, dict[str, Any]]:
        return self.doc["tiles"]

    @property
    def units(self) -> dict[str, dict[str, Any]]:
        return self.doc["units"]

    @property
    def cities(self) -> dict[str, dict[str, Any]]:
        return self.doc["cities"]

    @property
    def players(self) -> dict[str, dict[str, Any]]:
        return self.doc["players"]

    # -- helpers -----------------------------------------------------------
    def tile(self, q: int, r: int) -> dict[str, Any] | None:
        return self.tiles.get(tile_key(q, r))

    def unit(self, unit_id: str) -> dict[str, Any] | None:
        return self.units.get(unit_id)

    def city(self, city_id: str) -> dict[str, Any] | None:
        return self.cities.get(city_id)

    def player(self, player_id: int) -> dict[str, Any]:
        return self.players[str(player_id)]

    def units_at(self, q: int, r: int) -> list[dict[str, Any]]:
        key = tile_key(q, r)
        return [u for u in self.units.values() if tile_key(u["q"], u["r"]) == key]

    def enemy_units_at(self, q: int, r: int, owner: int) -> list[dict[str, Any]]:
        return [u for u in self.units_at(q, r) if u["owner"] != owner]

    def revealed_keys(self, player_id: int) -> set[str]:
        return set(self.doc["revealed"].get(str(player_id), []))

    def extend_revealed(self, player_id: int, keys: set[str]) -> list[str]:
        """Merge new tile keys into a player's remembered set; returns the sorted full list."""
        merged = self.revealed_keys(player_id) | keys
        lst = sorted(merged)
        self.doc["revealed"][str(player_id)] = lst
        return lst

    def spawn_unit(
        self, owner: int, type_: str, q: int, r: int
    ) -> tuple[str, dict[str, Any]]:
        spec = UNIT_TYPES[type_]
        unit_id = f"u{self.doc['next_unit_id']}"
        self.doc["next_unit_id"] += 1
        unit = {
            "unit_id": unit_id,
            "owner": owner,
            "type": type_,
            "q": q,
            "r": r,
            "movement": spec["mv"],
            "max_movement": spec["mv"],
            "hp": 100,
            "strength": spec["strength"],
            "ranged_strength": spec["ranged"],
            "fortified": False,
        }
        self.units[unit_id] = unit
        return unit_id, unit

    def remove_unit(self, unit_id: str) -> dict[str, Any]:
        return self.units.pop(unit_id)

    def sight_tiles(self, player_id: int) -> set[str]:
        """Currently observable tiles: unit sight UNION city territory."""
        out: set[str] = set()
        for unit in self.units.values():
            if unit["owner"] == player_id:
                for q, r in tiles_within((unit["q"], unit["r"]), UNIT_SIGHT):
                    key = tile_key(q, r)
                    if key in self.tiles:
                        out.add(key)
        for city in self.cities.values():
            if city["owner"] == player_id:
                for q, r in tiles_within((city["q"], city["r"]), city["border_radius"]):
                    key = tile_key(q, r)
                    if key in self.tiles:
                        out.add(key)
        return out
