"""The Lua mod is an UNVALIDATED draft; the marker must not silently vanish."""

from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path

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


def test_recorder_covers_treasury_research_production():
    """Codex P1-2 (partial): the release/window diffs must book the same
    attributes the digest covers — gold, research, per-city production —
    or engine effects move the digest with no ledger row to flag."""
    lua = (MODS / "PuppeteerMod.lua").read_text()
    assert 'book("player.gold"' in lua
    assert 'book("player.research_set"' in lua
    assert 'book("city.production_set"' in lua
    # the residual (districts, queues, promotions, tiles) is DECLARED
    assert "OUTSIDE both" in lua, "the recorder's scope limit stays written"


def test_live_validation_doc_exists_and_documents_both_routes():
    doc = (Path(__file__).resolve().parents[1] / "docs" / "live-validation.md").read_text()
    assert "aspyr-media" in doc and "compatdata/289070" in doc, (
        "both AppOptions.txt routes must stay documented"
    )
    assert "EnableTuner 1" in doc
    assert "4318" in doc
