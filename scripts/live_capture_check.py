"""Read-only M4 capture checker (OVX|2 / CITIES|2 / VMAP|4 / SPECW).

Modelled on scripts/live_seat_check.py: opens ONE GameConnection, runs the
new read surface against the live game, and prints row counts, per-field
'?'-fallback counts, and ms per read. NEVER acts: every command is one of
the capture reads (execute_read for the GameCore set, execute_write only
where the accessor context demands it — the CITIES|2 queue getter and the
palette are InGame-only).

AMENDMENT 1.5 (the parked-game trap): against a parked, never-attached
game `p:GetCities()` answers ZERO on both contexts — this script reports
`city surface empty` HONESTLY there. Its real target is the ATTACHED
3-round chain (recipe E.4): the city-accessor matrix (growth getters,
HasBuilding, districts, production progress) is confirmed there, never
against a parked game.

Usage:
    python scripts/live_capture_check.py [--host 127.0.0.1] [--port 4318]
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from civ_arena.game.civ6 import (  # noqa: E402
    lua_translator,
    response_parser,
    world_capture,  # noqa: E402
)
from civ_arena.game.civ6.vendor.connection import GameConnection  # noqa: E402


def _fallbacks(rows: list[dict], expected: frozenset[str] = frozenset()) -> dict[str, int]:
    """Per-key unread counts ('?' collapses to an ABSENT key in the
    parsers, so absence IS the honest '?'-fallback metric). Amendment 3
    item 11: counts include every field in the EXPECTED inventory (the
    CITIES|2 18-field set, the OVX|2 13-field set), so an empty city
    surface prints 18/18 unread rather than zero — distinguishing an
    empty inventory from all-fields-unread."""
    if not rows:
        # Amendment 3 item 11: an empty surface is distinct from "every
        # field unread" — return the expected inventory at N/N.
        return {key: 0 for key in sorted(expected)}
    keys = sorted({key for row in rows for key in row} | set(expected))
    return {key: sum(1 for row in rows if key not in row) for key in keys}


async def timed(label: str, call, parse):
    started = time.monotonic()
    lines = await call()
    ms = (time.monotonic() - started) * 1000.0
    doc = parse(lines)
    print(f"[{label}] {ms:.1f} ms")
    return doc, ms


async def main(host: str, port: int) -> None:
    conn = GameConnection(host, port)
    await conn.connect()
    total = 0.0
    try:
        # OVX|2 on the read transport (GameCore yields; civic progress '?')
        overview, ms = await timed(
            "OVX|2 read", lambda: conn.execute_read(
                lua_translator.overview_read(), timeout=25.0),
            response_parser.parse_overview)
        total += ms
        players = list(overview.get("players", {}).values())
        print(f"  players: {len(players)} rows; game_era="
              f"{overview.get('game_era', '<absent>')}")
        for row in players:
            print(f"    p{row['player_id']} keys={sorted(row)}")
        # CITIES|2 extended on the write transport (queue getter is InGame)
        extended, ms = await timed(
            "CITIES|2 extended (InGame)", lambda: conn.execute_write(
                lua_translator.cities_read(extended=True), timeout=25.0),
            lambda lines: response_parser.parse_cities(lines, qualified=True))
        total += ms
        print(f"  cities: {len(extended)} rows")
        if not extended:
            print("  city surface empty — parked-game trap (Amendment 1.5):"
                  " cities enumerate only on a driver-attached game")
        for key, missing in _fallbacks(extended,
                expected=frozenset(("name", "population", "is_capital", "is_major",
                                    "hp", "max_hp", "food", "thr", "surplus",
                                    "grow", "prodturns", "buildings", "districts",
                                    "production_queue"))).items():
            if missing:
                print(f"    {key}: unread(absent) on {missing}/{len(extended)}")
        # CITIES|1 lite for comparison
        lite, ms = await timed(
            "CITIES|1 lite (InGame)", lambda: conn.execute_write(
                lua_translator.cities_read(extended=False), timeout=25.0),
            lambda lines: response_parser.parse_cities(lines, qualified=True))
        total += ms
        print(f"  lite rows: {len(lite)} (majors only)")
        # VMAP|4 around player 0's capital, both accessor contexts
        capital = next((c for c in extended if c.get("owner") == 0), None)
        centre = (capital or {"q": 0, "r": 0})
        ring = [(centre["q"] + dq, centre["r"] + dr)
                for dq in range(-2, 3) for dr in range(-2, 3)]
        vmap_lua = lua_translator.visible_map_read(0, ring)
        for label, call in (("VMAP|4 gamecore (read)",
                             lambda: conn.execute_read(vmap_lua, timeout=25.0)),
                            ("VMAP|4 ingame (write)",
                             lambda: conn.execute_write(vmap_lua, timeout=25.0))):
            vmap, ms = await timed(label, call, response_parser.parse_visible_map)
            total += ms
            tiles = list(vmap["tiles"].values())
            print(f"  tiles: {len(tiles)}; unknown_terrain={vmap['unknown_terrain']}")
            for key in ("engine_visible", "resource", "improvement", "district",
                        "appeal", "feature", "river"):
                missing = sum(1 for t in tiles if key not in t)
                print(f"    {key}: unread(absent) on {missing}/{len(tiles)}")
        # the three SPECW reads (palette is InGame-only)
        roster, ms = await timed(
            "SPECW roster (read)", lambda: conn.execute_read(
                world_capture.roster_read(), timeout=25.0),
            world_capture.parse_roster)
        total += ms
        print(f"  roster: {len(roster)} rows; kinds="
              f"{sorted({r['kind'] for r in roster})}; levels="
              f"{sorted({r.get('level', '<absent>') for r in roster})}")
        tiles_doc, ms = await timed(
            "SPECW tiles (read)", lambda: conn.execute_read(
                world_capture.owned_tiles_read(), timeout=25.0),
            world_capture.parse_owned_tiles)
        total += ms
        owners = {row["owner"] for row in tiles_doc["rows"]}
        print(f"  owned tiles: {len(tiles_doc['rows'])} rows across "
              f"{len(owners)} owners; grid={tiles_doc['grid']}; "
              f"truncated={tiles_doc['truncated']}")
        palette, ms = await timed(
            "SPECW palette (InGame)", lambda: conn.execute_write(
                world_capture.palette_read(), timeout=25.0),
            world_capture.parse_palette)
        total += ms
        print(f"  palette: {len(palette)} rows (packing UNVERIFIED — "
              "raw ints only)")
        print(f"TOTAL read time: {total:.1f} ms")
    finally:
        await conn.disconnect()


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=4318)
    opts = ap.parse_args()
    asyncio.run(main(opts.host, opts.port))
