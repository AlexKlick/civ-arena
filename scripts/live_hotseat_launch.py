"""M18 rung 3 — launch the game FROM the live hotseat staging session.

The unsolved step of the hotseat lane: the staging session is open
(InSession true, Humans 2) and the argumentless-SP demote trap is behind
us, but no game has launched from the staging room yet — the START GAME
click attempt exited the game (2026-09-01). This script does on the wire
exactly what the game's own staging-room Lua does for hotseat
(stagingroom.lua OnReadyButton → SetLocalReady + Network.LaunchGame;
CheckGameAutoStart gates on every SS_TAKEN seat being Ready AND ModReady):

    uv run python scripts/live_hotseat_launch.py --read     # state only
    uv run python scripts/live_hotseat_launch.py --complete # leaders+ready
    uv run python scripts/live_hotseat_launch.py --launch   # LaunchGame()

Phases split so the read-back gates each mutation (read-back verification
is the operator's contract from live_newgame). Leaders come from the
engine's own tutorialsetup.lua (guaranteed base-ruleset strings). One
connection per invocation — the tuner serves a single client.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent / "src"))
sys.path.insert(0, str(_HERE))

from live_newgame import connect_state, find_state, run_lua  # noqa: E402

from civ_arena.game.civ6.vendor import SENTINEL  # noqa: E402

# Base-ruleset leaders the engine itself uses in tutorialsetup.lua —
# never hand-pick an unverified string.
LEADER_SEAT_0 = "LEADER_CLEOPATRA"
LEADER_SEAT_1 = "LEADER_GILGAMESH"

READ_LUA = f"""
local lp = Network.GetLocalPlayerID()
print("LocalPlayer|" .. tostring(lp))
print("InSession|" .. tostring(Network.IsSessionActive()))
print("IsHost|" .. tostring(Network.IsGameHost()))
print("IsHotseat|" .. tostring(GameConfiguration.IsHotseat()))
print("GameState|" .. tostring(GameConfiguration.GetGameState()))
print("Humans|" .. tostring(GameConfiguration.GetHumanPlayerCount()))
print("AI|" .. tostring(GameConfiguration.GetAIPlayerCount()))
for i = 0, 1 do
  local pc = PlayerConfigurations[i]
  local parts = {{tostring(i)}}
  table.insert(parts, "slot=" .. tostring(pc:GetSlotStatus()))
  table.insert(parts, "leader=" .. tostring(pc:GetLeaderTypeName()))
  table.insert(parts, "civ=" .. tostring(pc:GetCivilizationTypeName()))
  table.insert(parts, "civlevel=" .. tostring(pc:GetCivilizationLevelTypeID()))
  table.insert(parts, "ready=" .. tostring(pc:GetReady()))
  table.insert(parts, "modready=" .. tostring(pc:GetModReady()))
  table.insert(parts, "nick=" .. tostring(pc:GetNickName()))
  table.insert(parts, "human=" .. tostring(pc:IsHuman()))
  print("SEAT|" .. table.concat(parts, "|"))
end
print("{SENTINEL}")
"""

# Set a distinct base leader on each seat, then ready BOTH seats (the
# hotseat UI readies only the local player, but CheckGameAutoStart gates on
# every SS_TAKEN seat, and the wire can set them all) + broadcast.
COMPLETE_LUA = f"""
PlayerConfigurations[0]:SetLeaderTypeName("{LEADER_SEAT_0}")
PlayerConfigurations[1]:SetLeaderTypeName("{LEADER_SEAT_1}")
PlayerConfigurations[0]:SetReady(true)
PlayerConfigurations[1]:SetReady(true)
Network.BroadcastPlayerInfo(0)
Network.BroadcastPlayerInfo(1)
print("{SENTINEL}")
"""

# stagingroom.lua OnReadyButton, hotseat arm: the host launches directly.
LAUNCH_LUA = f"""
print("launch-pre|hotseat=" .. tostring(GameConfiguration.IsHotseat())
  .. "|host=" .. tostring(Network.IsGameHost())
  .. "|state=" .. tostring(GameConfiguration.GetGameState())
  .. "|humans=" .. tostring(GameConfiguration.GetHumanPlayerCount()))
Network.LaunchGame()
print("launchgame-called")
print("{SENTINEL}")
"""


async def run(host: str, port: int, lua: str, state: str,
              settle: float) -> int:
    conn = await connect_state(host, port, state)
    idx = await find_state(conn, state)
    assert idx is not None
    try:
        lines = await run_lua(conn, idx, lua)
    finally:
        await conn.disconnect()
    for ln in lines:
        print(ln)
    if settle > 0:
        await asyncio.sleep(settle)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=4318)
    ap.add_argument("--state", default="StagingRoom")
    ap.add_argument("--settle", type=float, default=0.0)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--read", action="store_true")
    g.add_argument("--complete", action="store_true")
    g.add_argument("--launch", action="store_true")
    opts = ap.parse_args()

    lua = {"read": READ_LUA, "complete": COMPLETE_LUA,
           "launch": LAUNCH_LUA}[("read" if opts.read else
                                  "complete" if opts.complete else "launch")]
    try:
        return asyncio.run(run(opts.host, opts.port, lua, opts.state,
                               opts.settle))
    except (ConnectionError, RuntimeError) as e:
        print(f"FAILED: {e}", file=sys.stderr)
        return 10


if __name__ == "__main__":
    sys.exit(main())
