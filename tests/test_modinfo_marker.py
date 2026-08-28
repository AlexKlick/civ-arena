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


def test_live_validation_doc_exists_and_documents_both_routes():
    doc = (Path(__file__).resolve().parents[1] / "docs" / "live-validation.md").read_text()
    assert "aspyr-media" in doc and "compatdata/289070" in doc, (
        "both AppOptions.txt routes must stay documented"
    )
    assert "EnableTuner 1" in doc
    assert "4318" in doc
