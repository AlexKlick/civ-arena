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


def test_v4_seat_resource_hidden_without_prereq_tech(tmp_path):
    """The v4 wire tech-gates resources exactly like the game's own UI
    mirror (GameInfo.Resources[idx].PrereqTech + techs:HasTech): a
    resource whose prereq the seat lacks reads '-' (observed none from
    the seat's viewpoint), a revealed one rides by name, and an unknown
    resource row reads '?' (unread -> absent)."""
    executable = shutil.which("texlua")
    if executable is None:
        pytest.skip("texlua unavailable for executable v4 fixture")
    fixture = """
if REVEALED == nil then REVEALED = false end
Game = {GetCurrentGameTurn=function() return 4 end}
GameInfo = {
  Terrains={[7]={TerrainType='TERRAIN_GRASS'}},
  Features={[3]={FeatureType='FEATURE_FOREST'}},
  Resources={[11]={ResourceType='RESOURCE_IRON', PrereqTech=4},
             [12]={ResourceType='RESOURCE_WHEAT', PrereqTech=-1},
             [13]={}},
}
Players = {[0]={GetTechs=function() return {
  HasTech=function(_, t) return t == 4 and REVEALED end} end}}
PlayersVisibility = {[0]={IsVisible=function() return true end}}
Map = {GetPlot=function(x, y)
  local plot = {GetTerrainType=function() return 7 end,
   GetOwner=function() return -1 end,
   GetFeatureType=function() return -1 end,
   GetResourceType=function()
     if x == 0 then return 11 elseif x == 1 then return 12
     elseif x == 2 then return 13 else return -1 end
   end,
   GetImprovementType=function() return -1 end,
   GetDistrictType=function() return -1 end,
   IsRiver=function() return false end,
   GetAppeal=function() return 0 end}
  return plot
end}
"""
    path = tmp_path / "v4.lua"
    path.write_text(fixture + lua_translator.visible_map_read(
        0, [(0, 0), (1, 0), (2, 0), (3, 0)]))
    result = subprocess.run([executable, str(path)], capture_output=True,
                            text=True, timeout=5, check=True)
    rows = [row for row in result.stdout.splitlines()
            if row.startswith("TILEROW|")]
    assert len(rows) == 4
    hidden, wheat, secret, bare = (response_parser.parse_visible_map(
        ["VMAP|4", "TURN|4", *rows])["tiles"][f"{q},0"] for q in range(4))
    assert hidden["resource"] == "", "unresearched iron reads '-' (none)"
    assert wheat["resource"] == "RESOURCE_WHEAT"  # no prereq: revealed
    assert "resource" not in secret, "nameless resource row reads ?"
    assert bare["resource"] == ""                 # observed none: '-'
    assert hidden["engine_visible"] is True       # PV table route answered
    assert hidden["river"] is False
    # PrereqTech indexes the tech table: researching tech 4 reveals iron
    path.write_text("REVEALED = true\n" + fixture + lua_translator.visible_map_read(
        0, [(0, 0)]))
    result = subprocess.run([executable, str(path)], capture_output=True,
                            text=True, timeout=5, check=True)
    revealed = response_parser.parse_visible_map([
        row for row in result.stdout.splitlines()
        if row.startswith(("VMAP|", "TURN|", "TILEROW|"))
    ])["tiles"]["0,0"]
    assert revealed["resource"] == "RESOURCE_IRON"


# Actual Map.GetPlotDistance observations, each distance == 1, retained in
# runs/sixty-round-development-20260905/hex-row-frame-probe.log. These fixed
# engine coordinates independently pin the transform, unlike a round-trip.
@pytest.mark.parametrize("origin, axial, neighbors", [
    ((47, 21), (37, 21), [(48, 21), (46, 21), (48, 22), (47, 20), (48, 20), (47, 22)]),
    ((47, 20), (37, 20), [(48, 20), (46, 20), (47, 21), (46, 19), (47, 19), (46, 21)]),
    ((46, 21), (36, 21), [(47, 21), (45, 21), (47, 22), (46, 20), (47, 20), (46, 22)]),
    ((46, 20), (36, 20), [(47, 20), (45, 20), (46, 21), (45, 19), (46, 19), (45, 21)]),
])
def test_all_24_host_observed_neighbors_keep_engine_distance_one(origin, axial, neighbors):
    assert lua_translator.xy_to_axial(*origin) == axial
    directions = [(1, 0), (-1, 0), (0, 1), (0, -1), (1, -1), (-1, 1)]
    for (dq, dr), engine_neighbor in zip(directions, neighbors, strict=True):
        target = axial[0] + dq, axial[1] + dr
        assert lua_translator.axial_to_xy(*target) == engine_neighbor
        assert lua_translator.xy_to_axial(*engine_neighbor) == target


@pytest.mark.parametrize("engine, axial", [
    ((47, 21), (37, 21)), ((47, 20), (37, 20)),
    ((46, 21), (36, 21)), ((46, 20), (36, 20)),
    ((-3, -2), (-2, -2)), ((-3, -3), (-1, -3)),
])
def test_real_lua_units_cities_and_terrain_share_host_frame(tmp_path, engine, axial):
    executable = shutil.which("texlua")
    if executable is None:
        pytest.skip("texlua unavailable for executable coordinate fixture")
    x, y = engine
    fixture = f"""
local unit={{GetID=function() return 131073 end,GetX=function() return {x} end,
 GetY=function() return {y} end,GetType=function() return 1 end}}
local city={{GetID=function() return 65536 end,GetX=function() return {x} end,
 GetY=function() return {y} end,GetName=function() return 'Frame City' end,
 GetBuildQueue=function() return {{GetCurrentProductionTypeHash=function() return 0 end}} end}}
local player={{GetID=function() return 1 end,IsBarbarian=function() return false end,
 GetUnits=function() return {{Members=function() return ipairs({{unit}}) end}} end,
 GetCities=function() return {{Members=function() return ipairs({{city}}) end}} end}}
PlayerManager={{GetAlive=function() return {{player}} end,
 GetAliveMajors=function() return {{player}} end}}
GameInfo={{Units={{[1]={{UnitType='UNIT_WARRIOR'}}}},Buildings={{}},
 Terrains={{[7]={{TerrainType='TERRAIN_GRASS'}}}}}}
Locale={{Lookup=function(value) return value end}}
Game={{GetCurrentGameTurn=function() return 4 end}}
Map={{GetPlot=function(px,py)
 assert(px=={x} and py=={y})
 return {{GetTerrainType=function() return 7 end,GetOwner=function() return 1 end}}
end}}
"""
    path = tmp_path / "frame.lua"
    path.write_text(fixture + lua_translator.units_read() + lua_translator.cities_read()
                    + lua_translator.visible_map_read(1, [axial]))
    result = subprocess.run([executable, str(path)], capture_output=True, text=True,
                            timeout=5, check=True)
    lines = result.stdout.splitlines()
    unit = response_parser.parse_units([r for r in lines if r.startswith("UNITROW|")],
                                       qualified=True)[0]
    city = response_parser.parse_cities([r for r in lines if r.startswith("CITYROW|")],
                                        qualified=True)[0]
    terrain = response_parser.parse_visible_map(
        [r for r in lines if r.startswith(("VMAP|", "TURN|", "TILEROW|"))])
    assert (unit["q"], unit["r"]) == axial
    assert (city["q"], city["r"]) == axial
    assert set(terrain["tiles"]) == {f"{axial[0]},{axial[1]}"}
    assert next(iter(terrain["tiles"].values()))["terrain"] == "GRASSLAND"
