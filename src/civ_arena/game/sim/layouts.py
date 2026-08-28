"""Seeded duel start: symmetric-ish radius-5 map, standard starting units.

Each player starts with 2 settlers + 2 warriors + 1 scout (bots must exercise
``found_city``; combat units exercise ``attack``/``fortify``). Terrain is a
seeded weighted roll, then sanitized so start areas and their immediate ring
are passable land and the two starts are connected by walkable tiles.
"""

from __future__ import annotations

import random
from typing import Any

from civ_arena.canonical import rng_to_doc
from civ_arena.game.sim.state import (
    MAP_RADIUS,
    TERRAIN,
    SimState,
    hex_dist,
    map_tiles,
    neighbors,
    tile_key,
    tiles_within,
)

STARTS: dict[int, tuple[int, int]] = {0: (-3, 1), 1: (3, -1)}

_TERRAIN_WEIGHTS: list[tuple[str, int]] = [
    ("GRASSLAND", 30),
    ("PLAINS", 30),
    ("HILL", 14),
    ("DESERT", 10),
    ("COAST", 10),
    ("MOUNTAIN", 6),
]

CIV_NAMES: dict[int, str] = {0: "ROME", 1: "KOREA"}


def _weighted_terrain(rng: random.Random) -> str:
    total = sum(w for _, w in _TERRAIN_WEIGHTS)
    pick = rng.randint(1, total)
    acc = 0
    for name, w in _TERRAIN_WEIGHTS:
        acc += w
        if pick <= acc:
            return name
    return "PLAINS"  # unreachable


def _passable_land(rng: random.Random) -> str:
    while True:
        t = _weighted_terrain(rng)
        if t not in ("COAST", "MOUNTAIN"):
            return t


def duel_start(seed: int, player_count: int = 2) -> dict[str, Any]:
    if player_count != 2:
        raise ValueError("spike sim supports exactly 2 players")
    rng = random.Random(seed)

    tiles: dict[str, dict[str, Any]] = {}
    for q, r in map_tiles(MAP_RADIUS):
        tiles[tile_key(q, r)] = {
            "q": q,
            "r": r,
            "terrain": _weighted_terrain(rng),
            "owner": -1,
            "city": "",
        }

    # Sanitize: each start + its ring-1 are passable land.
    for start in STARTS.values():
        for q, r in tiles_within(start, 1):
            tile = tiles[tile_key(q, r)]
            tile["terrain"] = "GRASSLAND" if (q, r) == start else _passable_land(rng)

    # Carve a guaranteed land corridor between the starts.
    a, b = STARTS[0], STARTS[1]
    steps = hex_dist(a, b)
    for i in range(steps + 1):
        t = i / steps
        q = round(a[0] + (b[0] - a[0]) * t)
        r = round(a[1] + (b[1] - a[1]) * t)
        for nq, nr in [(q, r), *neighbors(q, r)]:
            key = tile_key(nq, nr)
            if key in tiles and TERRAIN[tiles[key]["terrain"]]["move"] == 0:
                tiles[key]["terrain"] = "PLAINS"

    # State rng is a distinct, explicitly threaded instance.
    state_rng = random.Random(seed * 7919 + 13)
    doc: dict[str, Any] = {
        "turn": 1,
        "phase_index": 0,
        "phase_player": -1,
        "next_unit_id": 1,
        "next_city_id": 1,
        "freeze_active": False,
        "tiles": tiles,
        "units": {},
        "cities": {},
        "players": {},
        "revealed": {"0": [], "1": []},
        "rng": rng_to_doc(state_rng),
    }
    state = SimState(doc)

    for player_id in range(2):
        start = STARTS[player_id]
        roster = ["SETTLER", "SETTLER", "WARRIOR", "WARRIOR", "SCOUT"]
        placement = [start, *sorted(neighbors(*start))[:4]]
        for type_, spot in zip(roster, placement, strict=True):
            state.spawn_unit(player_id, type_, spot[0], spot[1])
        state.doc["players"][str(player_id)] = {
            "player_id": player_id,
            "civ_name": CIV_NAMES[player_id],
            "gold": 100,
            "science_bucket": 0,
            "researched": [],
            "researching": "",
            "alive": True,
        }
        state.extend_revealed(player_id, state.sight_tiles(player_id))

    return state.to_doc()
