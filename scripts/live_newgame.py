"""M17b/c — zero-touch game creation through the front-end tuner states.

The missing rung of the live ladder: with the game sitting at the MAIN
MENU, the tuner exposes the front-end Lua contexts (Main State, StagingRoom,
...) which carry the full hosting API — ``Network.HostGame`` /
``GameConfiguration`` / ``MapConfiguration`` — so a game can be created
WITHOUT a human click. The game states (GameCore_Tuner / InGame) only
materialize once a map loads, which is why the live smoke cannot pass at
the menu.

Enum values are Civ VI type hashes, verified live this session:
``hash(s) == ~crc32(s)`` (32-bit, signed when transmitted) — checked
against GameInfo.GameSpeeds rows AND the live MapConfiguration value
(MAPSIZE_SMALL). Never hand-type these numbers; compute them here.

Two phases, deliberately split so the map load (the point of no return)
happens only after the read-back verification passes:

    uv run python scripts/live_newgame.py --host     # host + configure + verify
    uv run python scripts/live_newgame.py --launch   # Network.LaunchGame()

Single-connection discipline: the tuner serves ONE client and refuses
rapid reconnects — every phase uses one connection, and a forced
reconnect waits for the game to notice the disconnect first.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import zlib
from pathlib import Path

from civ_arena.game.civ6.vendor import SENTINEL, tuner_client
from civ_arena.game.civ6.vendor.connection import GameConnection, _parse_output

# -- Civ VI type hashes -------------------------------------------------


def civ_hash(name: str) -> int:
    """Civ VI enum/type hash: bitwise NOT of CRC32 (verified against live
    GameInfo.GameSpeeds rows and MapConfiguration.GetMapSize())."""
    return (~zlib.crc32(name.encode("utf-8"))) & 0xFFFFFFFF


def lua_int(v: int) -> str:
    """Render a hash as the Lua integer literal with the same value."""
    return str(v if v < 2**31 else v - 2**32)


MAPSIZE_DUEL = civ_hash("MAPSIZE_DUEL")
MAPSIZE_TINY = civ_hash("MAPSIZE_TINY")
GAMESPEED_STANDARD = civ_hash("GAMESPEED_STANDARD")

# -- shared plumbing -----------------------------------------------------


async def find_state(conn: GameConnection, name: str) -> int | None:
    for i, n in conn.lua_states.items():
        if n == name:
            return i
    return None


async def run_lua(conn: GameConnection, idx: int, lua: str,
                  timeout: float = 8.0) -> list[str]:
    """Collect parsed O-lines until the sentinel (single connection)."""
    await tuner_client.drain_messages(conn._reader, timeout=0.2)  # noqa: SLF001
    await tuner_client.send_message(
        conn._writer, tuner_client.TAG_COMMAND, f"CMD:{idx}:{lua}")  # noqa: SLF001
    lines: list[str] = []
    for _ in range(500):
        msg = await tuner_client.recv_message_timeout(conn._reader,
                                                      timeout=timeout / 10)
        if msg is None:
            break
        if msg.payload.startswith("ERR:"):
            raise RuntimeError(f"Lua error: {msg.payload}")
        text = _parse_output(msg.payload)
        if text is None:
            continue
        if text.strip() == SENTINEL:
            break
        lines.append(text)
    return lines


async def connect_state(host: str, port: int, state: str) -> GameConnection:
    """Connect (retrying politely — the tuner refuses rapid reconnects) and
    resolve the named state, re-handshaking once if the list was stale."""
    conn = GameConnection(host, port)
    last_err: Exception | None = None
    for attempt in range(4):
        try:
            await conn.connect()
            last_err = None
            break
        except ConnectionError as e:
            last_err = e
            await asyncio.sleep(2.0 + attempt)
    if last_err is not None:
        raise last_err
    for _ in range(3):
        idx = await find_state(conn, state)
        if idx is not None:
            return conn
        # states can register late as the front-end finishes booting
        await conn.disconnect()
        await asyncio.sleep(3.0)
        await conn.connect()
    await conn.disconnect()
    raise RuntimeError(f"state {state!r} not found: {conn.lua_states}")


# -- phase: host + configure + verify ------------------------------------

HOST_LUA = f"""
print("mode-before|" .. tostring(GameConfiguration.GetGameMode()))
Network.SetLocalNetworkMode(GameModeTypes.SINGLEPLAYER)
Network.HostGame()
print("hostgame-called")
print("{SENTINEL}")
"""

# M18: the hotseat HOST — Network.HostGame takes a SERVER TYPE (the engine's
# own automation suite passes it; mainmenu.lua derives it from the lobby
# mode via ServerTypeForMPLobbyType). The ARGUMENTLESS HostGame used in SP
# takes the local fast path: it auto-launches and the launch resolution
# DEMOTES every non-local seated human to AI (four live attempts). With
# SERVER_TYPE_HOTSEAT the host opens the HOTSEAT STAGING SESSION instead —
# the seated players survive, and Network.LaunchGame() starts the game.
HOST_HOTSEAT_LUA = f"""
print("mode-before|" .. tostring(GameConfiguration.GetGameMode()))
Network.HostGame(ServerType.SERVER_TYPE_HOTSEAT)
print("hostgame-hotseat-called")
print("InSession|" .. tostring(Network.IsSessionActive()))
print("Humans|" .. tostring(GameConfiguration.GetHumanPlayerCount()))
print("{SENTINEL}")
"""

# Read-back verification gates the launch: every value set here must read
# back exactly, or the operator (not the script) decides what to do.
# Map size is TINY, not DUEL: on duel-with-2-majors the engine AI's
# settler runs out of settle spots mid-game and path-spins forever
# (AStar log: UNIT_SETTLER -> -9999,-9999 at turn ~15, 2026-09-01) —
# one size class of headroom keeps the opponent's expansion alive.
CONFIG_LUA = f"""
MapConfiguration.SetMapSize({lua_int(MAPSIZE_TINY)})
MapConfiguration.SetMinMajorPlayers(2)
MapConfiguration.SetMaxMajorPlayers(2)
GameConfiguration.SetParticipatingPlayerCount(2)
GameConfiguration.SetGameSpeedType({lua_int(GAMESPEED_STANDARD)})
print("RuleSet|" .. tostring(GameConfiguration.GetRuleSet()))
print("GameMode|" .. tostring(GameConfiguration.GetGameMode()))
print("GameSpeed|" .. tostring(GameConfiguration.GetGameSpeedType()))
print("MapSize|" .. tostring(MapConfiguration.GetMapSize()))
print("MapScript|" .. tostring(MapConfiguration.GetScript()))
print("MinMajor|" .. tostring(MapConfiguration.GetMinMajorPlayers()))
print("MaxMajor|" .. tostring(MapConfiguration.GetMaxMajorPlayers()))
print("Participating|" .. tostring(GameConfiguration.GetParticipatingPlayerCount()))
print("InUse|" .. tostring(GameConfiguration.GetInUsePlayerCount()))
print("Human|" .. tostring(GameConfiguration.GetHumanPlayerCount()))
print("AI|" .. tostring(GameConfiguration.GetAIPlayerCount()))
print("SessionActive|" .. tostring(Network.IsSessionActive()))
print("IsGameHost|" .. tostring(Network.IsGameHost()))
print("{SENTINEL}")
"""

LAUNCH_LUA = f"""
print("launch-pre|participating=" .. tostring(GameConfiguration.GetParticipatingPlayerCount()) .. "|host=" .. tostring(Network.IsGameHost()))
Network.LaunchGame()
print("launchgame-called")
print("{SENTINEL}")
"""

# M18 hotseat: two HUMAN seats, no engine AI (the class of engine-AI hangs
# that ends long games dies with it). Seating recipe from the ENGINE'S OWN
# automation suite (steamassets/.../automation_standardtests.lua): a slot
# becomes human via SetSlotStatus(SlotStatus.SS_TAKEN) — SS_OPEN (1) is
# UNOCCUPIED and auto-fills with AI at launch (live-learned). Hotseat names
# seat the players per the game's own EditHotseatPlayer flow. Then HostGame
# auto-launches as in SP.
CONFIG_HOTSEAT_LUA = f"""
Network.SetLocalNetworkMode(GameModeTypes.HOTSEAT)
GameConfiguration.SetGameMode(GameModeTypes.HOTSEAT)
MapConfiguration.SetMapSize({lua_int(MAPSIZE_TINY)})
MapConfiguration.SetMinMajorPlayers(2)
MapConfiguration.SetMaxMajorPlayers(2)
GameConfiguration.SetParticipatingPlayerCount(2)
GameConfiguration.SetGameSpeedType({lua_int(GAMESPEED_STANDARD)})
PlayerConfigurations[0]:SetSlotStatus(SlotStatus.SS_TAKEN)
PlayerConfigurations[0]:SetMajorCiv()
PlayerConfigurations[0]:SetHotseatName("Arena Seat 1")
pcall(function() PlayerConfigurations[0]:SetHotseatPassword("arena") end)
Network.BroadcastPlayerInfo(0)
PlayerConfigurations[1]:SetSlotStatus(SlotStatus.SS_TAKEN)
PlayerConfigurations[1]:SetMajorCiv()
PlayerConfigurations[1]:SetHotseatName("Arena Seat 2")
pcall(function() PlayerConfigurations[1]:SetHotseatPassword("arena") end)
Network.BroadcastPlayerInfo(1)
print("GameMode|" .. tostring(GameConfiguration.GetGameMode()))
print("IsHotseat|" .. tostring(GameConfiguration.IsHotseat()))
print("Humans|" .. tostring(GameConfiguration.GetHumanPlayerCount()))
print("AI|" .. tostring(GameConfiguration.GetAIPlayerCount()))
print("MapSize|" .. tostring(MapConfiguration.GetMapSize()))
print("Participating|" .. tostring(GameConfiguration.GetParticipatingPlayerCount()))
print("P0Human|" .. tostring(PlayerConfigurations[0]:IsHuman()))
print("P1Human|" .. tostring(PlayerConfigurations[1]:IsHuman()))
print("P0Slot|" .. tostring(PlayerConfigurations[0]:GetSlotStatus()))
print("P1Slot|" .. tostring(PlayerConfigurations[1]:GetSlotStatus()))
print("{SENTINEL}")
"""


async def phase(host: str, port: int, lua: str, state: str,
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
    ap.add_argument("phase_arg", choices=["host", "config", "hotseat", "hostseat",
                                          "launch"],
                    help="host: create the local session; config: apply + "
                         "verify the duel setup; hotseat: the two-human-seat "
                         "setup; launch: start the map load")
    ap.add_argument("--settle", type=float, default=0.0,
                    help="seconds to wait after the phase (state churn)")
    ap.add_argument("--state", default="StagingRoom")
    opts = ap.parse_args()

    lua = {"host": HOST_LUA, "config": CONFIG_LUA,
           "hotseat": CONFIG_HOTSEAT_LUA, "hostseat": HOST_HOTSEAT_LUA,
           "launch": LAUNCH_LUA}[opts.phase_arg]
    try:
        return asyncio.run(phase(opts.host, opts.port, lua, opts.state,
                                 opts.settle))
    except (ConnectionError, RuntimeError) as e:
        print(f"FAILED: {e}", file=sys.stderr)
        return 10


if __name__ == "__main__":
    sys.exit(main())
