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
    # exists only inside RestoreUnit (per-unit), never in OnPlayerTurnStartComplete
    start_complete = lua.split("function OnPlayerTurnStartComplete", 1)[1]
    start_complete = start_complete.split("function ", 1)[0]
    assert "FinishMoves" in start_complete
    assert "RestoreMovement" not in start_complete, (
        "the hook must freeze, never bulk-restore"
    )


def test_freeze_snapshots_the_frozen_state():
    """Codex P1-1: the lease snapshot is the release-diff baseline, so it
    must be taken AFTER the freeze — snapshotting first books our own
    movement-zeroing as an undeclared violation at release."""
    lua = (MODS / "PuppeteerMod.lua").read_text()
    hook = lua.split("function OnPlayerTurnStartComplete", 1)[1]
    hook = hook.split("function ", 1)[0]
    assert hook.index("FinishMoves") < hook.index("snapshot_player"), (
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
