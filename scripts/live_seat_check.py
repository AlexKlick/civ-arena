"""In-game seat census via the tuner (GameCore context).

Reads the live game's players: human/major/alive per seat + leader names,
the PlayerConfiguration slot status and hotseat pause flag, and IsHuman
read BOTH ways (the Player object and the PlayerConfiguration — the
LoadGame(SERVER_TYPE_NONE) demote may move one and not the other, which is
exactly what the A1 re-flag probe decides). GameCore_Tuner has no front-end
tables (GameConfiguration et al are nil there), so only Game/Player objects
and PlayerConfigurations are used.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from civ_arena.game.civ6.vendor.connection import GameConnection  # noqa: E402

LUA = """
local out = {}
local ps = Game.GetPlayers()
for i, p in ipairs(ps) do
  local pid = p:GetID()
  local leader = "?"
  local slot = "?"
  local pause = "?"
  local cfghuman = "?"
  local cfg = PlayerConfigurations[pid]
  if cfg ~= nil then
    leader = tostring(cfg:GetLeaderTypeName())
    if cfg.GetSlotStatus ~= nil then slot = tostring(cfg:GetSlotStatus()) end
    -- GetWantsPause is front-end-only: absent in the GameCore context.
    if cfg.GetWantsPause ~= nil then
      pause = tostring(cfg:GetWantsPause())
    end
    if cfg.IsHuman ~= nil then cfghuman = tostring(cfg:IsHuman()) end
  end
  table.insert(out, "P" .. pid
    .. "|human=" .. tostring(p:IsHuman())
    .. "|cfghuman=" .. cfghuman
    .. "|major=" .. tostring(p:IsMajor())
    .. "|alive=" .. tostring(p:IsAlive())
    .. "|leader=" .. leader
    .. "|slot=" .. slot
    .. "|pause=" .. pause)
end
print(table.concat(out, "\\n"))
print("CENSUS_END|" .. tostring(#out))
print("---END---")
"""


async def main(host: str, port: int) -> None:
    conn = GameConnection(host, port)
    await conn.connect()
    try:
        lines = await conn.execute_read(LUA, timeout=8.0)
        for ln in lines:
            print(ln)
    finally:
        await conn.disconnect()


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=4318)
    opts = ap.parse_args()
    asyncio.run(main(opts.host, opts.port))
