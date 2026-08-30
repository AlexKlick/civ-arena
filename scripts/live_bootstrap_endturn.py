"""One-shot bootstrap end-turn for a parked pre-lease turn (M14d).

WHY THIS EXISTS: attach-while-parked — the mod is injected mid-way through
the LOCAL player's parked turn, whose PlayerTurnStartComplete hook already
fired, so no lease can engage until the NEXT turn starts. In M14b the
operator clicked end-turn by hand for every parked turn (that click is
itself an unrefereed end-turn by design); this script is the same single
action, driver-side, so a live dispatch run no longer depends on human
click timing.

DISCIPLINE: this issues exactly ONE end-turn for the local player and only
when NO lease is engaged (a fresh injection's parked turn). It books no
observations, no acts, no hashes — the referee's lease window starts at the
NEXT hook. Every use is recorded dated in docs/live-validation.md §6.
"""

from __future__ import annotations

import argparse
import asyncio
import time

from civ_arena.game.civ6 import lua_translator, response_parser
from civ_arena.game.civ6.vendor.connection import GameConnection


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=4318)
    ap.add_argument("--timeout", type=float, default=300.0,
                    help="seconds to wait for the engine to advance")
    opts = ap.parse_args()
    conn = GameConnection(opts.host, opts.port)
    await conn.connect()

    parsed = response_parser.parse_kv_lines(
        await conn.execute_read(lua_translator.mod_status()))
    if parsed.get("PUPPET_ACTIVE") is True:
        print(f"REFUSED: a lease is engaged ({dict(parsed)}) — "
              "bootstrap end-turn would cut a live lease short")
        await conn.disconnect()
        return 2
    turn_before = int(parsed.get("TURN", -1))
    print(f"parked turn {turn_before}, no lease — issuing ONE end-turn")

    await conn.execute_write(lua_translator.request_end_turn(0))
    deadline = time.monotonic() + opts.timeout
    while time.monotonic() < deadline:
        await asyncio.sleep(2.0)
        parsed = response_parser.parse_kv_lines(
            await conn.execute_read(lua_translator.mod_status()))
        turn = int(parsed.get("TURN", -1))
        if turn > turn_before or parsed.get("PUPPET_ACTIVE") is True:
            print(f"advanced: TURN {turn_before} -> {turn}, "
                  f"PUPPET_ACTIVE={parsed.get('PUPPET_ACTIVE')}, "
                  f"LEASE_TURN={parsed.get('LEASE_TURN')}")
            await conn.disconnect()
            return 0
    print(f"TIMEOUT: engine still at turn {turn_before}")
    await conn.disconnect()
    return 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
