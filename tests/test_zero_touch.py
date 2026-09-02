"""M17b — zero-touch harness invariants.

The front-end hosting path lives or dies on the Civ VI type hashes
embedded in the generated Lua: a wrong hash sets a different map size or
speed and the game still launches (silently misconfigured). The hash
identity ``~crc32(s)`` was verified LIVE (2026-08-31 session) against
GameInfo.GameSpeeds rows and MapConfiguration.GetMapSize() read-back on
:4318 — these pins keep it from regressing.
"""

from __future__ import annotations

import importlib.util
import sys
import zlib
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def _load(name: str) -> object:
    spec = importlib.util.spec_from_file_location(
        name, REPO / "scripts" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


def test_civ_hash_identity():
    lng = _load("live_newgame")
    for s in ("GAMESPEED_STANDARD", "MAPSIZE_DUEL", "SINGLEPLAYER", ""):
        assert lng.civ_hash(s) == (~zlib.crc32(s.encode("utf-8"))) & 0xFFFFFFFF


def test_known_hash_values():
    """Values read back from the live engine (2026-08-31): GameSpeeds DB
    rows and the MapConfiguration read-back after SetMapSize(DUEL)."""
    lng = _load("live_newgame")
    assert lng.civ_hash("GAMESPEED_STANDARD") == 327976177
    assert lng.civ_hash("GAMESPEED_QUICK") == 2870794323
    assert lng.civ_hash("GAMESPEED_ONLINE") == 2645421392
    assert lng.civ_hash("MAPSIZE_DUEL") == 388991850
    assert lng.civ_hash("MAPSIZE_SMALL") == 2457744968
    assert lng.civ_hash("SINGLEPLAYER") == 3915931367


def test_lua_int_signedness():
    lng = _load("live_newgame")
    # hashes >= 2^31 must cross into negative Lua integers, not overflow
    assert lng.lua_int(3915931367) == "-379035929"
    assert lng.lua_int(388991850) == "388991850"


def test_config_lua_carries_verified_setup():
    lng = _load("live_newgame")
    lua = lng.CONFIG_LUA
    # TINY map (duel proved too cramped: the engine AI's settler
    # path-spins without settle spots), two majors, standard speed —
    # every setter is followed by a read-back print so the operator
    # gates the launch on the engine confirming the value
    assert "MapConfiguration.SetMapSize(-601637951)" in lua
    assert "MapConfiguration.SetMinMajorPlayers(2)" in lua
    assert "MapConfiguration.SetMaxMajorPlayers(2)" in lua
    assert "GameConfiguration.SetParticipatingPlayerCount(2)" in lua
    assert "GameConfiguration.SetGameSpeedType(327976177)" in lua
    for gate in ("MapSize|", "MinMajor|", "MaxMajor|", "Participating|",
                 "GameSpeed|", "SessionActive|"):
        assert f'print("{gate}' in lua
    assert 'print("' + lng.SENTINEL + '")' in lua


def test_static_probes_resolve_sandboxed_envs():
    """UI contexts expose neither _G nor _ENV nor getfenv; the static probe
    must therefore reference symbols as BARE names only."""
    fp = _load("frontend_probe")
    for name in ("Network", "GameConfiguration", "MapConfiguration"):
        assert f"if {name} ~= nil" in fp.STATIC_LUA
    assert "_G[" not in fp.STATIC_LUA
    # the env-resolve chain (for Main State, which does have _G) degrades
    # _G -> _ENV -> getfenv and reports UNRESOLVABLE rather than erroring
    assert "env = _G" in fp.ENV_RESOLVE
    assert "_ENV ~= nil" in fp.ENV_RESOLVE
    assert "getfenv ~= nil" in fp.ENV_RESOLVE
    assert "ENV|UNRESOLVABLE" in fp.ENV_RESOLVE


def test_skeys_uses_bare_symbol():
    fp = _load("frontend_probe")
    lua = fp.skeys_lua("GameModeTypes")
    assert "local t = GameModeTypes" in lua
    assert "PAIRS-FAILED" in lua  # pcall guard for userdata tables


if __name__ == "__main__":
    sys.exit(0)
