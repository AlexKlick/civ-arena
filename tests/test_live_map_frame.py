"""Execute terrain queries to bind their output to the requested axial frame."""
from __future__ import annotations

import shutil
import subprocess

import pytest

from civ_arena.game.civ6 import lua_translator, response_parser


@pytest.mark.parametrize("coords", [
    [(46, -1), (47, -1), (49, 0)],
    [(0, 0), (1, 3), (2, -1)],
    [(-1, 4), (-2, 3), (-3, -2)],
])
def test_real_lua_terrain_rows_use_requested_axial_frame(tmp_path, coords):
    lua = shutil.which("texlua")
    if lua is None:
        pytest.skip("texlua unavailable for executable terrain-frame fixture")
    fixture = """
Game = {GetCurrentGameTurn=function() return 4 end}
GameInfo = {Terrains={[7]={TerrainType='TERRAIN_GRASS'}}}
Map = {GetPlot=function(x,y)
  print('LOOKUP|' .. x .. '|' .. y)
  return {GetTerrainType=function() return 7 end, GetOwner=function() return 0 end}
end}
"""
    path = tmp_path / "map.lua"
    path.write_text(fixture + lua_translator.visible_map_read(0, coords))
    result = subprocess.run([lua, str(path)], capture_output=True, text=True,
                            timeout=5, check=True)
    lines = result.stdout.splitlines()
    lookups = [tuple(map(int, row.split("|")[1:])) for row in lines
               if row.startswith("LOOKUP|")]
    assert lookups == [lua_translator.axial_to_xy(q, r) for q, r in coords]
    parsed = response_parser.parse_visible_map(
        [row for row in lines if not row.startswith("LOOKUP|")])
    expected = {f"{q},{r}" for q, r in coords}
    assert set(parsed["tiles"]) == expected
    assert parsed["visible"] == expected
    assert all(tile["terrain"] == "GRASSLAND" for tile in parsed["tiles"].values())
    assert parsed["unknown_terrain"] == 0


@pytest.mark.parametrize("engine, expected, unknown", [
    ("TERRAIN_COAST", "COAST", 0), ("TERRAIN_OCEAN", "OCEAN", 0),
    ("TERRAIN_PLAINS_HILLS", "HILL", 0), ("GRASS", "GRASSLAND", 0),
    ("TERRAIN_UNRECOGNIZED", "PLAINS", 1),
])
def test_terrain_vocabulary_preserves_water_hills_and_legacy_rows(engine, expected, unknown):
    result = response_parser.parse_visible_map([
        "VMAP|3", "TURN|4", f"TILEROW|46|-1|{engine}|true|0|", "---END---",
    ])
    assert result["tiles"]["46,-1"]["terrain"] == expected
    assert result["unknown_terrain"] == unknown
