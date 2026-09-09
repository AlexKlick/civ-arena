"""One-shot production-blocker resolver (M14d live recovery).

WHY: when the arena's end-turn fires while a city's build queue is empty
(the build completed during the engine's end-of-turn processing, after the
agent's last command), the engine HALTS the whole turn cycle waiting for
the local player's production choice — the AI's turn never starts and the
trace ring shows the cycle stop at our deactivation (live-learned
2026-08-30, run 011: Tyre queue '-').

This is the recovery primitive: for every LOCAL city with an empty queue,
read its production options and issue ONE BUILD (the turtler doctrine's
own preference order) via the same InGame operation the
set_city_production tool uses. Every step prints; it touches nothing but
empty queues.

DISCIPLINE: used only outside a lease (it refuses while one is engaged);
each use is recorded dated in docs/live-validation.md §6.
"""

from __future__ import annotations

import argparse
import asyncio

from civ_arena.game.civ6 import lua_translator, response_parser
from civ_arena.game.civ6.firetuner import verify_production
from civ_arena.game.civ6.vendor.connection import GameConnection

# the turtler doctrine's own build preference (scripted.py)
PREFERENCE = ["MONUMENT", "WALLS", "WARRIOR", "GRANARY", "SETTLER",
              "SCOUT", "SLINGER", "BARRACKS"]


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=4318)
    ap.add_argument("--dry-run", action="store_true")
    opts = ap.parse_args()
    conn = GameConnection(opts.host, opts.port)
    try:
        await conn.connect()

        status = response_parser.parse_kv_lines(
            await conn.execute_read(lua_translator.mod_status()))
        if status.get("PUPPET_ACTIVE") is True:
            print(f"REFUSED: lease engaged ({dict(status)}) — resolve only "
                  "outside a lease")
            return 2

        local = 0
        pcall_status = await conn.execute_read(lua_translator.poll_turn_state())
        for line in pcall_status:
            if line.startswith("LOCAL|"):
                local = int(line.split("|", 1)[1])
        cities = response_parser.parse_cities(
            await conn.execute_write(lua_translator.cities_read()), qualified=True)
        mine = [c for c in cities if c["owner"] == local
                and not c["production_queue"]]
        if not mine:
            print("no empty-queue local cities — nothing to resolve")
            return 0
        print(f"empty-queue local cities: "
              f"{[(c['city_id'], c['name']) for c in mine]}")

        resolved = 0
        for city in mine:
            items = response_parser.parse_available_production(
                await conn.execute_write(
                    lua_translator.available_production_read(city["city_id"])))
            by_id = {i["item_id"]: i for i in items}
            pick = next((p for p in PREFERENCE if p in by_id), None)
            if pick is None:
                print(f"  {city['name']}: no preferred item among "
                      f"{sorted(by_id)[:6]}...")
                continue
            if opts.dry_run:
                print(f"  {city['name']}: would build {pick} (dry run)")
                continue
            submitted = await conn.execute_write(
                lua_translator.set_city_production(city["city_id"], pick))
            verdict = response_parser.parse_act(submitted)
            if verdict["status"] == "accepted":
                await verify_production(conn, city["city_id"], submitted)
            print(f"  {city['name']}: BUILD {pick} -> {verdict['status']}"
                  f"{'/' + verdict.get('detail', '') if verdict.get('detail') else ''}")
            if verdict["status"] == "accepted":
                resolved += 1
        print(f"resolved {resolved}/{len(mine)} blocked cities")
        return 0 if resolved == len(mine) else 1
    finally:
        await conn.disconnect()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
