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

# The A1 wire-save name (Saves/ root-routed by the engine's save type).
SAVE_NAME_DEFAULT = "civ-arena-a1"

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

# A1 (2026-09-03): the Architecture-1 session runs with EMPTY hotseat
# passwords — the shipped playerchange.lua auto-OKs the hand-off panel on
# Return ONLY when GetHotseatPassword() == "" (nil does NOT count), so the
# read-back must print the empty string. Derived from live_newgame's block
# (single source of truth) with loud assertions against silent drift.
CONFIG_HOTSEAT_EMPTY_LUA = CONFIG_HOTSEAT_LUA.replace(
    'SetHotseatPassword("arena")', 'SetHotseatPassword("")'
).replace(
    f'print("{SENTINEL}")',
    'print("P0PW|" .. tostring(PlayerConfigurations[0]:GetHotseatPassword()))\n'
    'print("P1PW|" .. tostring(PlayerConfigurations[1]:GetHotseatPassword()))\n'
    f'print("{SENTINEL}")',
)
assert 'SetHotseatPassword("")' in CONFIG_HOTSEAT_EMPTY_LUA, (
    "live_newgame's password literal drifted — update the empty variant")
assert "P0PW|" in CONFIG_HOTSEAT_EMPTY_LUA, (
    "live_newgame's sentinel drifted — password read-backs not injected")

# Wire-side save (the engine's own quicksave/automation recipe: ingame.lua
# OnInputActionTriggered + automation_standardtests SharedGame_OnSaveComplete).
# InGame context: Network/SaveLocations/SaveFileTypes live there. The save
# TYPE the engine wants for THIS game is printed so the manual load can use
# the same value.
def save_lua(name: str) -> str:
    return f"""
local t = Network.GetGameConfigurationSaveType()
print("SAVETYPE|" .. tostring(t))
local p = {{
  Name = "{name}",
  Location = SaveLocations.LOCAL_STORAGE,
  Type = t,
  IsAutosave = false,
  IsQuicksave = false,
}}
Network.SaveGame(p)
print("savegame-called")
print("{SENTINEL}")
"""

# A1 rung 7: the decisive probe — re-flag a demoted seat human IN-GAME.
# No shipped Lua does this mid-game; SetSlotStatus + broadcast is the
# candidate. Read-backs print BOTH IsHuman paths so the census and the
# probe agree on what actually flipped.
REFLAG_LUA_TMPL = """
local pid = {seat}
PlayerConfigurations[pid]:SetSlotStatus(SlotStatus.SS_TAKEN)
pcall(function() PlayerConfigurations[pid]:SetWantsPause(false) end)
pcall(function() Network.BroadcastPlayerInfo(pid) end)
print("REFLAG_SLOT|" .. tostring(pid) .. "|" .. tostring(PlayerConfigurations[pid]:GetSlotStatus()))
print("REFLAG_CFGHUMAN|" .. tostring(pid) .. "|" .. tostring(PlayerConfigurations[pid]:IsHuman()))
local p = Players[pid]
if p ~= nil then
  print("REFLAG_PHUMAN|" .. tostring(pid) .. "|" .. tostring(p:IsHuman()))
else
  print("REFLAG_PHUMAN|" .. tostring(pid) .. "|nil")
end
print("{sent}")
"""


def reflag_lua(seat: int) -> str:
    return REFLAG_LUA_TMPL.format(seat=seat, sent=SENTINEL)


# Manual load of the wire-saved game (parameterized LOAD_LUA): the type is
# whatever SAVETYPE| printed at save time (raw number), defaulting to the
# engine's SINGLE_PLAYER (0 is the shipped enum's first value; pass the
# printed number explicitly if the load falls through to defaults).
def load_manual_lua(name: str, save_type: int | None) -> str:
    type_expr = (str(save_type) if save_type is not None
                 else "SaveTypes.SINGLE_PLAYER")
    return f"""
local p = {{
  Location = SaveLocations.LOCAL_STORAGE,
  Type = {type_expr},
  IsAutosave = false,
  IsQuicksave = false,
  Name = "{name}",
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
                   empty_passwords: bool = False,
                   transition_timeout: float = 180.0) -> int:
    """The whole hotseat launch on ONE tuner connection, held open across
    the HostGame transition (the disconnect is what the degraded-boot
    pathology punishes — and an idle unjoined staging session has exited
    the game twice now, so no gaps).

    Gates: every phase's read-back must match before the next mutation.
    """
    config_lua = (CONFIG_HOTSEAT_EMPTY_LUA if empty_passwords
                  else CONFIG_HOTSEAT_LUA)
    conn = await connect_state(host, port, state)
    try:
        # 1. seat + verify (live_newgame's proven Lua, same read-backs)
        idx = await find_state(conn, state)
        assert idx is not None
        for ln in await run_lua(conn, idx, config_lua):
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
    ap.add_argument("--state", default=None,
                    help="tuner state (default: per-phase, see below)")
    ap.add_argument("--settle", type=float, default=0.0)
    ap.add_argument("--empty", action="store_true",
                    help="with --full/--complete: empty hotseat passwords")
    ap.add_argument("--reflag-seat", type=int, default=1,
                    help="seat to re-flag human for --reflag")
    ap.add_argument("--save-name", default=SAVE_NAME_DEFAULT)
    ap.add_argument("--load-type", type=int, default=None,
                    help="raw SaveTypes value printed by --save's SAVETYPE|")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--read", action="store_true")
    g.add_argument("--complete", action="store_true")
    g.add_argument("--launch", action="store_true")
    g.add_argument("--load", action="store_true",
                   help="Network.LoadGame the hotseat autosave (NONE)")
    g.add_argument("--load-manual", action="store_true",
                   help="Network.LoadGame the --save'd game (NONE)")
    g.add_argument("--save", action="store_true",
                   help="Network.SaveGame now (InGame context)")
    g.add_argument("--reflag", action="store_true",
                   help="re-flag a demoted seat human (InGame context)")
    g.add_argument("--full", action="store_true",
                   help="config → host → complete → launch on ONE connection")
    opts = ap.parse_args()

    # Per-phase default states: the front-end tables (Network, Save* enums)
    # live in different VMs — LoadGameMenu proved sufficient for loads, the
    # game's InGame context carries the save/reflag surface.
    state = opts.state
    if state is None:
        if opts.save or opts.reflag:
            state = "InGame"
        elif opts.load_manual:
            state = "LoadGameMenu"
        else:
            state = "StagingRoom"

    try:
        if opts.full:
            return asyncio.run(
                run_full(opts.host, opts.port, opts.state or "StagingRoom",
                         empty_passwords=opts.empty))
        if opts.save:
            lua = save_lua(opts.save_name)
        elif opts.reflag:
            lua = reflag_lua(opts.reflag_seat)
        elif opts.load_manual:
            lua = load_manual_lua(opts.save_name, opts.load_type)
        elif opts.load:
            lua = LOAD_LUA
        elif opts.read:
            lua = READ_LUA
        elif opts.complete:
            lua = COMPLETE_LUA
        else:
            lua = LAUNCH_LUA
        return asyncio.run(run(opts.host, opts.port, lua, state,
                               opts.settle))
    except (ConnectionError, RuntimeError) as e:
        print(f"FAILED: {e}", file=sys.stderr)
        return 10


if __name__ == "__main__":
    sys.exit(main())
