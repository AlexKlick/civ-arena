"""M18 ad-hoc: in-game seat census via the tuner (GameCore context).

Reads the live game's players: human/major/alive per seat + leader names.
GameCore_Tuner has no front-end tables (GameConfiguration et al are nil
there), so only Game/Player objects are used.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from civ_arena.game.civ6.vendor.connection import GameConnection  # noqa: E402

LUA = """
local out = {}
local ps = Game.GetPlayers()
for i, p in ipairs(ps) do
  local leader = "?"
  local cfg = PlayerConfigurations[p:GetID()]
  if cfg ~= nil then leader = tostring(cfg:GetLeaderTypeName()) end
  table.insert(out, "P" .. p:GetID()
    .. "|human=" .. tostring(p:IsHuman())
    .. "|major=" .. tostring(p:IsMajor())
    .. "|alive=" .. tostring(p:IsAlive())
    .. "|leader=" .. leader)
end
print(table.concat(out, "\\n"))
"""


async def main() -> None:
    conn = GameConnection("127.0.0.1", 4318)
    await conn.connect()
    try:
        lines = await conn.execute_read(LUA, timeout=8.0)
        for ln in lines:
            print(ln)
    finally:
        await conn.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
