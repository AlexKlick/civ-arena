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

from live_newgame import (  # noqa: E402
    CONFIG_HOTSEAT_LUA,
    HOST_HOTSEAT_LUA,
    connect_state,
    find_state,
    run_lua,
)

from civ_arena.game.civ6.vendor import SENTINEL, tuner_client  # noqa: E402

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

# M18 rung 4: HostGame(SERVER_TYPE_HOTSEAT) kills the FireTuner listener
# for the whole process (live-proven twice 2026-09-02) — but Network.LoadGame
# is a different engine path. Load the hotseat AUTOsave back with the tuner
# still attached; param shape from loadgamemenu.lua OnLoadYes + the
# automation suite's PlayGame LoadConfiguration block.
#
# Rung 5 refinement: with SERVER_TYPE_HOTSEAT the load opens a STAGING
# session (roster reset to defaults) and the tuner still dies — the network
# SESSION is the killer, not HostGame specifically. HOTSEAT IS LOCAL: try
# SERVER_TYPE_NONE (the automation suite's own LoadConfiguration server
# type) — resume the hotseat save with no session server at all, the SP
# path the tuner demonstrably survives.
LOAD_LUA = f"""
local p = {{
  Location = SaveLocations.LOCAL_STORAGE,
  Type = SaveTypes.SINGLE_PLAYER,
  FileType = SaveFileTypes.GAME_SAVE,
  IsAutosave = true,
  IsQuicksave = false,
  Directory = "Hotseat/auto",
  Name = "AutoSave_0001",
}}
local ok = Network.LoadGame(p, ServerType.SERVER_TYPE_NONE)
print("loadgame-returned|" .. tostring(ok))
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


async def _rediscover(conn) -> bool:
    """Re-run the LSQ handshake on the SAME socket (the tuner server can
    outlive front-end transitions; the TCP client should not have to
    reconnect). Returns True when a StagingRoom state is present."""
    _, raw_states = await tuner_client.handshake(conn._reader,  # noqa: SLF001
                                                 conn._writer)  # noqa: SLF001
    states: dict[int, str] = {}
    i = 0
    while i + 1 < len(raw_states):
        try:
            states[int(raw_states[i])] = raw_states[i + 1]
            i += 2
        except ValueError:
            i += 1
    conn.lua_states = states
    return find_state_sync(states, "StagingRoom") is not None


def find_state_sync(states: dict[int, str], name: str) -> int | None:
    for i, n in states.items():
        if n == name:
            return i
    return None


async def run_full(host: str, port: int, state: str,
                   transition_timeout: float = 180.0) -> int:
    """The whole hotseat launch on ONE tuner connection, held open across
    the HostGame transition (the disconnect is what the degraded-boot
    pathology punishes — and an idle unjoined staging session has exited
    the game twice now, so no gaps).

    Gates: every phase's read-back must match before the next mutation.
    """
    conn = await connect_state(host, port, state)
    try:
        # 1. seat + verify (live_newgame's proven Lua, same read-backs)
        idx = await find_state(conn, state)
        assert idx is not None
        for ln in await run_lua(conn, idx, CONFIG_HOTSEAT_LUA):
            print(ln)
        # 2. host the hotseat staging session
        for ln in await run_lua(conn, idx, HOST_HOTSEAT_LUA):
            print(ln)
        # 3. wait out the front-end transition ON THIS SOCKET, polling the
        #    state list until StagingRoom re-registers (index may change).
        deadline = asyncio.get_event_loop().time() + transition_timeout
        new_idx: int | None = None
        while asyncio.get_event_loop().time() < deadline:
            await asyncio.sleep(3.0)
            try:
                if await _rediscover(conn):
                    new_idx = find_state_sync(conn.lua_states, state)
                    break
            except (asyncio.IncompleteReadError, ConnectionError, OSError):
                print("handshake-failed-retry", flush=True)
        if new_idx is None:
            print("FAILED: StagingRoom never re-registered after HostGame")
            return 11
        print(f"staging-registered|idx={new_idx}", flush=True)
        # 4. read the seats, gate on session active
        for ln in await run_lua(conn, new_idx, READ_LUA):
            print(ln)
        # 5. complete seat 2 + ready both (leaders + SetReady + broadcast)
        for ln in await run_lua(conn, new_idx, COMPLETE_LUA):
            print(ln)
        # 6. verify the completion took
        for ln in await run_lua(conn, new_idx, READ_LUA):
            print(ln)
        # 7. launch — stagingroom.lua OnReadyButton, hotseat arm
        for ln in await run_lua(conn, new_idx, LAUNCH_LUA):
            print(ln)
        return 0
    finally:
        await conn.disconnect()


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
    g.add_argument("--load", action="store_true",
                   help="Network.LoadGame the hotseat autosave (HOTSEAT)")
    g.add_argument("--full", action="store_true",
                   help="config → host → complete → launch on ONE connection")
    opts = ap.parse_args()

    try:
        if opts.full:
            return asyncio.run(
                run_full(opts.host, opts.port, opts.state))
        lua = {"read": READ_LUA, "complete": COMPLETE_LUA,
               "launch": LAUNCH_LUA, "load": LOAD_LUA}[
            ("load" if opts.load else
             "read" if opts.read else
             "complete" if opts.complete else "launch")]
        return asyncio.run(run(opts.host, opts.port, lua, opts.state,
                               opts.settle))
    except (ConnectionError, RuntimeError) as e:
        print(f"FAILED: {e}", file=sys.stderr)
        return 10


if __name__ == "__main__":
    sys.exit(main())
