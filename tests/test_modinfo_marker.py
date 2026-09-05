"""The Lua mod is an UNVALIDATED draft; the marker must not silently vanish."""

from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

MODS = Path(__file__).resolve().parents[1] / "mods" / "PuppeteerMod"
MARKER = "UNVALIDATED"


def test_modinfo_parses_and_carries_marker():
    tree = ET.parse(MODS / "PuppeteerMod.modinfo")
    root = tree.getroot()
    assert root.tag == "Mod"
    assert root.attrib["id"] == "CIV_ARENA_PUPPETEER"
    scripts = root.findall("./Components/AddGameplayScripts/File")
    assert [s.text for s in scripts] == ["PuppeteerMod.lua"]
    assert MARKER in (MODS / "PuppeteerMod.modinfo").read_text()
    assert root.find("./Properties/EnabledByDefault").text == "0", (
        "an unvalidated mod must not be enabled by default"
    )


def test_lua_carries_marker_and_rejected_pattern():
    lua = (MODS / "PuppeteerMod.lua").read_text()
    assert MARKER in lua
    # the design doc explicitly rejects bulk restore-after-freeze:
    # a FinishMoves immediately followed by RestoreMovement in the same
    # block is the upstream proposal's risky ordering
    assert "EXPLICITLY REJECTED" in lua
    # and the forbidden API is called out
    assert "SetCivic" in lua and "FORBIDDEN" not in lua  # documented, not used
    # find the actual calls: FinishMoves exists (freeze), RestoreMovement
    # exists only inside RestoreUnit (per-unit), never in the acquisition
    # helper or its native hook caller.
    acquire = lua.split("local function acquire_lease(", 1)[1]
    acquire, start_complete = acquire.split("local function OnPlayerTurnStartComplete", 1)
    start_complete = start_complete.split("function ", 1)[0]
    assert "UnitManager.FinishMoves(unit)" in acquire
    assert "acquire_lease(playerID, nil)" in start_complete
    assert "RestoreMovement" not in acquire + start_complete, (
        "the hook must freeze, never bulk-restore"
    )


def test_freeze_snapshots_the_frozen_state():
    """Codex P1-1: the lease snapshot is the release-diff baseline, so it
    must be taken AFTER the freeze — snapshotting first books our own
    movement-zeroing as an undeclared violation at release."""
    lua = (MODS / "PuppeteerMod.lua").read_text()
    acquire = lua.split("local function acquire_lease(", 1)[1]
    acquire = acquire.split("local function OnPlayerTurnStartComplete", 1)[0]
    assert acquire.index("UnitManager.FinishMoves(unit)") < acquire.index(
        "snapshot = snapshot_player(playerID)"
    ), (
        "freeze FIRST, then snapshot — else the freeze itself drifts"
    )


def test_recorder_covers_treasury_research():
    """Codex P1-2 (partial): the release/window diffs must book the same
    attributes the digest covers — gold, research — or engine effects move
    the digest with no ledger row to flag. Production-name coverage is
    DEFERRED (no GameCore accessor; recorded in §6)."""
    lua = (MODS / "PuppeteerMod.lua").read_text()
    assert 'book("player.gold"' in lua
    assert 'book("player.research_set"' in lua
    assert "GetGoldBalance" in lua, "the GameCore treasury accessor"
    assert "DEFERRED" in lua, "the production deferral stays written"
    # the residual (districts, queues, promotions, tiles) is DECLARED
    assert "OUTSIDE both" in lua, "the recorder's scope limit stays written"


def test_live_validation_doc_exists_and_documents_both_routes():
    doc = (Path(__file__).resolve().parents[1] / "docs" / "live-validation.md").read_text()
    assert "aspyr-media" in doc and "compatdata/289070" in doc, (
        "both AppOptions.txt routes must stay documented"
    )
    assert "EnableTuner 1" in doc
    assert "4318" in doc


def test_mod_lua_parses():
    """The mod file must PARSE before it can be injected — a Lua syntax
    error costs a live run (run 008: 'function arguments expected' from a
    stray half-line reached the wire). luatex's embedded interpreter is
    the host's available Lua; skipped where absent."""
    import shutil
    import subprocess
    from pathlib import Path

    luatex = shutil.which("luatex")
    if luatex is None:
        pytest.skip("no luatex on this host for the Lua parse gate")
    repo = Path(__file__).resolve().parents[1]
    checker = repo / "tests" / "_lua_parse_check.lua"
    checker.write_text(
        "local chunk, err = loadfile(arg[1])\n"
        "if not chunk then print('PARSE_FAIL|' .. tostring(err)) os.exit(1) end\n"
        "print('PARSE_OK')\n",
        encoding="utf-8",
    )
    proc = subprocess.run(
        [luatex, "--luaonly", str(checker),
         str(repo / "mods" / "PuppeteerMod" / "PuppeteerMod.lua")],
        capture_output=True, text=True, timeout=60.0)
    assert "PARSE_OK" in proc.stdout, proc.stdout + proc.stderr


def test_every_translator_output_parses():
    """EVERY Lua builder's output must parse — the fake cannot catch syntax
    (it pattern-matches, never executes), and the purchase builder shipped
    unparsed for hours because nothing luac'd it (live run 012)."""
    import shutil
    import subprocess
    from pathlib import Path

    from civ_arena.game.civ6 import lua_translator

    luatex = shutil.which("luatex")
    if luatex is None:
        pytest.skip("no luatex on this host for the Lua parse gate")
    repo = Path(__file__).resolve().parents[1]
    checker = repo / "tests" / "_lua_parse_check2.lua"
    checker.write_text(
        "local chunk, err = loadfile(arg[1])\n"
        "if not chunk then print('PARSE_FAIL|' .. tostring(err)) os.exit(1) end\n"
        "print('PARSE_OK')\n",
        encoding="utf-8",
    )
    outputs = [
        lua_translator.poll_turn_state(),
        lua_translator.overview_read(),
        lua_translator.units_read(),
        lua_translator.cities_read(),
        lua_translator.visible_map_read(0, [(0, 0), (1, -1), (-2, 3)]),
        lua_translator.available_research_read(0),
        lua_translator.available_production_read("c0:3"),
        lua_translator.mod_handshake(),
        lua_translator.mod_status(),
        lua_translator.mod_digest(),
        lua_translator.mod_trace(),
        lua_translator.diff_since_last("pos,moves", 4),
        lua_translator.move_unit("u0:131073", "2,3"),
        lua_translator.attack("u0:131073", "u0:65538"),
        lua_translator.fortify("u0:131073"),
        lua_translator.found_city("u0:131073", "Name"),
        lua_translator.set_research(0, "MINING"),
        lua_translator.set_city_production("c0:65536", "MONUMENT"),
        lua_translator.purchase("c0:65536", "MONUMENT"),
        lua_translator.request_end_turn(0),
        lua_translator.finish_all_moves(0),
        lua_translator.set_puppet(0, True),
        lua_translator.release(0, 12),
        lua_translator.restore_unit("u0:7"),
        lua_translator.freeze_unit("u0:7"),
        lua_translator.begin_ambient_window(0),
        lua_translator.end_ambient_window(0),
        lua_translator.dump_ledger(),
        lua_translator.dump_ambient(),
        lua_translator.blocker_query(),
        lua_translator.resolve_civic(),
        lua_translator.fill_policy_slots(),
    ]
    import tempfile

    for lua in outputs:
        with tempfile.NamedTemporaryFile("w", suffix=".lua",
                                         delete=False) as fh:
            fh.write(lua)
            path = fh.name
        proc = subprocess.run(
            [luatex, "--luaonly", str(checker), path],
            capture_output=True, text=True, timeout=60.0)
        assert "PARSE_OK" in proc.stdout, (
            f"Lua parse failure:\n{proc.stdout}{proc.stderr}\n---\n{lua}")
