"""Seeded start layouts: symmetric-ish maps, standard starting units.

Each player starts with 2 settlers + 2 warriors + 1 scout (bots must exercise
``found_city``; combat units exercise ``attack``/``fortify``). Terrain is a
seeded weighted roll, then sanitized so start areas and their immediate ring
are passable land and the starts are connected by walkable tiles.

Two seat counts are supported: the historical radius-5 duel (2 seats, body
kept verbatim for bit-identity — see tests/test_four_seat_sim.py) and a
radius-7 four-seat arena whose four starts are point-symmetric, so no seat is
geometrically advantaged.
"""

from __future__ import annotations

import random
from typing import Any

from civ_arena.canonical import rng_to_doc
from civ_arena.game.sim.state import (
    AXIAL_DIRS,
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

# Four-seat arena: starts at hex_dist 6 on the radius-7 map, point-symmetric
# under 180° rotation (ring angles 0°/120°/180°/240°). Every seat's ring-1
# neighbours stay on-map, and every seat has exactly one rival at hex_dist 6
# and two at 12 — identical per-seat exposure, no privileged corner.
MAP_RADIUS_4P: int = 7  # 3r²+3r+1 = 169 tiles
STARTS_4: dict[int, tuple[int, int]] = {
    0: (6, 0), 1: (0, -6), 2: (-6, 0), 3: (0, 6),
}
CIV_NAMES_4: dict[int, str] = {0: "ROME", 1: "KOREA", 2: "EGYPT", 3: "MONGOL"}

_START_LAYOUTS: dict[int, tuple[int, dict[int, tuple[int, int]]]] = {
    2: (MAP_RADIUS, STARTS),
    4: (MAP_RADIUS_4P, STARTS_4),
}


def start_layout(player_count: int) -> tuple[int, dict[int, tuple[int, int]]]:
    """Single source of truth for (map_radius, starts) per seat count."""
    if player_count not in _START_LAYOUTS:
        raise ValueError(f"no start layout for player_count={player_count}")
    return _START_LAYOUTS[player_count]


def hex_line_greedy(a: tuple[int, int], b: tuple[int, int]) -> list[tuple[int, int]]:
    """Exact hex line a→b: each step takes the AXIAL_DIR that strictly
    minimizes hex_dist to b (ties: lowest direction index). Emits exactly
    hex_dist(a, b) + 1 adjacent tiles — no float rounding, unlike a lerp
    (at radius-7 distances banker's rounding lands midpoints off-line)."""
    out: list[tuple[int, int]] = [a]
    q, r = a
    for _ in range(hex_dist(a, b)):
        best_dq, best_dr, best_dist = 0, 0, -1
        for dq, dr in AXIAL_DIRS:
            dist = hex_dist((q + dq, r + dr), b)
            if best_dist < 0 or dist < best_dist:
                best_dq, best_dr, best_dist = dq, dr, dist
        q, r = q + best_dq, r + best_dr
        out.append((q, r))
    return out


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
    if player_count == 2:
        return _duel_start_2(seed)
    if player_count == 4:
        return _duel_start_4(seed)
    raise ValueError(f"sim supports exactly 2 or 4 players, got {player_count}")


def _duel_start_2(seed: int) -> dict[str, Any]:
    """Historical 2-seat start. Body kept verbatim from a59cb44: the golden
    hashes in tests/test_four_seat_sim.py pin its exact terrain-rng draw
    order, so do not refactor it — dispatch, don't generalize."""
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


def _duel_start_4(seed: int) -> dict[str, Any]:
    """Four-seat arena start over the radius-7 point-symmetric layout.

    Corridors are carved along the start cycle 0→1→2→3→0 with
    ``hex_line_greedy`` (28 ring-dilated steps, same total as hub-and-spoke
    through the origin) so every seat reaches every other by land."""
    rng = random.Random(seed)
    starts = STARTS_4

    tiles: dict[str, dict[str, Any]] = {}
    for q, r in map_tiles(MAP_RADIUS_4P):
        tiles[tile_key(q, r)] = {
            "q": q,
            "r": r,
            "terrain": _weighted_terrain(rng),
            "owner": -1,
            "city": "",
        }

    # Sanitize: each start + its ring-1 are passable land.
    for start in starts.values():
        for q, r in tiles_within(start, 1):
            tile = tiles[tile_key(q, r)]
            tile["terrain"] = "GRASSLAND" if (q, r) == start else _passable_land(rng)

    # Carve guaranteed land corridors along the start cycle.
    for pid in range(4):
        for q, r in hex_line_greedy(starts[pid], starts[(pid + 1) % 4]):
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
        "revealed": {str(i): [] for i in range(4)},
        "rng": rng_to_doc(state_rng),
    }
    state = SimState(doc)

    for player_id in range(4):
        start = starts[player_id]
        roster = ["SETTLER", "SETTLER", "WARRIOR", "WARRIOR", "SCOUT"]
        placement = [start, *sorted(neighbors(*start))[:4]]
        for type_, spot in zip(roster, placement, strict=True):
            state.spawn_unit(player_id, type_, spot[0], spot[1])
        state.doc["players"][str(player_id)] = {
            "player_id": player_id,
            "civ_name": CIV_NAMES_4[player_id],
            "gold": 100,
            "science_bucket": 0,
            "researched": [],
            "researching": "",
            "alive": True,
        }
        state.extend_revealed(player_id, state.sight_tiles(player_id))

    return state.to_doc()
