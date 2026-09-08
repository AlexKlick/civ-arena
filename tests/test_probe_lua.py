"""texlua harness for scripts/probes/spectator_accessors.lua (M4 step 0).

Runs the probe against MINIMAL stub globals (Game/Map/GameInfo present,
PlayersVisibility and UI deliberately ABSENT) and pins the wire contract:
completion sentinel, 4-field rows, no backslash escapes, missing-globals
yield `missing` rows, bounded player enumeration, determinism. texlua is a
HARD requirement — the test fails (never skips) without it, because the M4
gate must prove the probe executes for real.
"""

import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
PROBE_LUA = REPO / 'scripts' / 'probes' / 'spectator_accessors.lua'
PLAYER_CAP = 12  # must match the probe's own bound

# Minimal stub globals. 15 players exercise the enum cap; player 0 is the
# major anchor with a city at (10,4); player 1 is the city-state minor whose
# GetBuildQueue() returns nil (the THIS-MATTERS minor-queue case); players
# 2-14 are barbarian non-majors. PlayersVisibility and UI stay undefined.
STUB = """
local techrows = {[1] = {Index = 0, TechnologyType = 'TECH_POTTERY'}}
local buildingrows = {[1] = {Hash = 7, BuildingType = 'BUILDING_GRANARY'}}
local function iter(rows)
  local i = 0
  return function()
    i = i + 1
    if i <= #rows then return rows[i] end
    return nil
  end
end

local treasury = {
  GetGold = function() return 12 end,
  GetGoldBalance = function() return 12.5 end,
  GetScience = function() return 1 end,
  GetCulture = function() return 2 end,
  GetFaith = function() return 3 end,
  GetGoldFromDiplomacy = function() return 4 end,
}
local culture = {
  GetProgressingCivic = function() return 1 end,
  GetCulturalProgress = function(_, a) if a == nil then return -1 end return 5 end,
  GetCostNextCivic = function(_, a) if a == nil then return 60 end return 61 end,
  HasCivic = function(_, i) return i == 1 end,
  GetCultureYield = function() return 2.5 end,
}
local techs = {
  GetScience = function() return 1.5 end,
  HasTech = function(_, i) return i == 0 end,
}
local influence = {GetSuzerain = function() return 42 end}
local religion = {GetFaithYield = function() return 6 end}

local d1 = {
  GetType = function() return 5 end,
  IsComplete = function() return true end,
  GetDamage = function() return 10 end,
  GetMaxDamage = function() return 100 end,
  GetCurrentHitPoints = function() return 90 end,
}
local districts = {
  Members = function() return ipairs({d1}) end,
  FindID = function(_, t) if t == 5 then return d1 end return nil end,
}
local empty_districts = {
  Members = function() return ipairs({}) end,
  FindID = function() return nil end,
}
local growth = {
  GetFood = function() return 20 end,
  GetFoodThreshold = function() return 25 end,
  GetGrowthThreshold = function() return 26 end,
  GetFoodSurplus = function() return 2 end,
  GetTurnsLeft = function() return 3 end,
}
local plotC, plotN  -- forward: city:GetPlot() captures these

local bq = {
  GetCurrentProductionTypeHash = function() return 0 end,
  GetProduction = function() return 5 end,
  GetProductionProgress = function() return 2 end,
  GetDomainFreeProduction = function() return 1 end,
}
local city = {
  GetX = function() return 10 end,
  GetY = function() return 4 end,
  IsCapital = function() return true end,
  HasBuilding = function(_, h) return h == 9 end,
  GetPopulation = function() return 3 end,
  GetGrowth = function() return growth end,
  GetDistricts = function() return districts end,
  GetBuildQueue = function() return bq end,
  GetFood = function() return 21 end,
  GetTurnsLeft = function() return 4 end,
  GetPlot = function() return plotC end,
}
local minorcity = {
  GetX = function() return 30 end,
  GetY = function() return 6 end,
  IsCapital = function() return false end,
  HasBuilding = function(_) return false end,
  GetPopulation = function() return 1 end,
  GetGrowth = function() return growth end,
  GetDistricts = function() return empty_districts end,
  GetBuildQueue = function() return nil end,
  GetFood = function() return 5 end,
  GetTurnsLeft = function() return 9 end,
}
plotC = {
  GetX = function() return 10 end, GetY = function() return 4 end,
  GetFeatureType = function() return -1 end, GetResourceType = function() return -1 end,
  GetImprovementType = function() return -1 end, GetDistrictType = function() return 5 end,
  IsRiver = function() return false end, GetAppeal = function() return 1 end,
  IsCity = function() return true end, GetOwner = function() return 0 end,
  GetTerrainType = function() return 2 end,
}
plotN = {
  GetX = function() return 11 end, GetY = function() return 4 end,
  GetFeatureType = function() return -1 end, GetResourceType = function() return -1 end,
  GetImprovementType = function() return -1 end, GetDistrictType = function() return -1 end,
  IsRiver = function() return false end, GetAppeal = function() return 0 end,
  IsCity = function() return false end, GetOwner = function() return -1 end,
  GetTerrainType = function() return 2 end,
}

local function P(id, major, barb, city_obj)
  return {
    GetID = function() return id end,
    IsMajor = function() return major end,
    IsAlive = function() return true end,
    IsBarbarian = function() return barb end,
    GetTreasury = function() return treasury end,
    GetCulture = function() return culture end,
    GetTechs = function() return techs end,
    GetInfluence = function() return influence end,
    GetReligion = function() return religion end,
    GetEra = function() return 0 end,
    GetCivilizationLevelType = function() return 1 end,
    GetCities = function()
      if city_obj == nil then
        return {Members = function() return ipairs({}) end}
      end
      return {Members = function() return ipairs({city_obj}) end}
    end,
  }
end

local player_list = {P(0, true, false, city), P(1, false, false, minorcity)}
for i = 2, 14 do player_list[i + 1] = P(i, false, true, nil) end

PlayerManager = {
  GetAliveMajors = function() return {player_list[1]} end,
  GetAlive = function() return player_list end,
}
Game = {
  GetPlayers = function() return player_list end,
  GetEras = function() return {GetCurrentEra = function() return 0 end} end,
}
Map = {
  GetPlotCount = function() return 100 end,
  GetGridSize = function() return 10, 8 end,
  GetPlot = function(x, y)
    if x == 10 and y == 4 then return plotC end
    if x == 11 and y == 4 then return plotN end
    return nil
  end,
  GetPlotByIndex = function(_, i) if i == 0 then return plotC end return nil end,
  GetNeighborPlot = function(x, y, d)
    if d == 2 and x == 10 and y == 4 then return plotN end
    return nil
  end,
}
GameInfo = {
  Technologies = setmetatable({}, {__call = function() return iter(techrows) end}),
  Buildings = setmetatable(
    {BUILDING_MONUMENT = {Hash = 9, BuildingType = 'BUILDING_MONUMENT'}},
    {__call = function() return iter(buildingrows) end}),
  Leaders = {},
}
PlayerConfigurations = {
  [0] = {
    GetLeaderTypeName = function() return 'LEADER_TRAJAN' end,
    GetCivilizationTypeName = function() return 'CIV_ROME' end,
    GetDifficultyType = function() return 3 end,
  },
  [1] = {
    GetLeaderTypeName = function() return 'LEADER_CITY_STATE' end,
    GetCivilizationTypeName = function() return 'CIV_CITY_STATE' end,
    GetDifficultyType = function() return 1 end,
  },
}
CivilizationLevelTypes = {NONE = 0, FULL = 1}
"""


def probe_source() -> str:
    if not PROBE_LUA.is_file():
        pytest.fail(f'probe Lua missing: {PROBE_LUA}')
    return PROBE_LUA.read_text(encoding='utf-8')


@pytest.fixture
def lua(tmp_path):
    executable = shutil.which('texlua')
    if executable is None:
        pytest.fail('texlua unavailable: the probe harness must execute for '
                    'real (the M4 gate fails, never skips, without texlua)')

    def run(code):
        path = tmp_path / 'probe.lua'
        path.write_text(code, encoding='utf-8')
        result = subprocess.run([executable, str(path)], capture_output=True,
                                text=True, timeout=15, check=True)
        return result.stdout.splitlines()
    return run


def run_probe(lua):
    return lua(STUB + '\n' + probe_source())


def probe_rows(rows):
    return [r for r in rows if r.startswith('PROBE|')]


def test_probe_completes_with_end_sentinel_and_honest_count(lua):
    rows = run_probe(lua)
    assert rows[-1] == '---END---'
    match = re.fullmatch(r'PROBE_END\|([0-9]+)', rows[-2])
    assert match, rows[-2:]
    assert int(match.group(1)) == len(probe_rows(rows)) > 100


def test_every_probe_row_has_exactly_four_fields(lua):
    for row in probe_rows(run_probe(lua)):
        fields = row.split('|')
        assert len(fields) == 4, row


def test_no_backslash_escapes_or_gmatch_anywhere(lua):
    source = probe_source()
    assert chr(92) not in source, 'backslash in probe source'
    assert 'gmatch' not in source, 'gmatch in probe source'
    for row in run_probe(lua):
        assert chr(92) not in row, row


def test_missing_globals_yield_missing_rows_never_errors(lua):
    rows = run_probe(lua)
    for expected in (
        'PROBE|pv.table|missing|',
        'PROBE|pv.p0|missing|',
        'PROBE|pv.p0.isvisible|missing|',
        'PROBE|pv.p0.isrevealed|missing|',
        'PROBE|ui.table|missing|',
        'PROBE|ui.getplayercolors|missing|',
        'PROBE|ui.getheadselectedcity|missing|',
        'PROBE|ui.queryplayerlinecolor|missing|',
        'PROBE|pv.p0.isvisible.neighbor|missing|',
        'PROBE|pv.p0.isrevealed.neighbor|missing|',
    ):
        assert expected in rows, expected
    assert all(f'|{t}|' not in r for r in probe_rows(rows) for t in ('error',))


def test_players_enumeration_is_bounded(lua):
    rows = run_probe(lua)
    enum_rows = [r for r in rows if r.startswith('PROBE|players.enum|')]
    assert len(enum_rows) == PLAYER_CAP
    assert f'PROBE|players.enum.count|enum|{PLAYER_CAP}' in rows
    assert 'PROBE|players.enum|enum|pid=0,major=true,alive=true,barb=false' in rows
    assert 'PROBE|players.enum|enum|pid=1,major=false,alive=true,barb=false' in rows


def test_probe_is_deterministic_across_runs(lua):
    assert run_probe(lua) == run_probe(lua)


def test_probe_rows_answer_the_matrix_shape_questions(lua):
    rows = run_probe(lua)
    for expected in (
        'PROBE|map.getplotcount|number|100',
        'PROBE|map.getgridsize.raw|number|10,8,nil,nil,nil,nil',
        'PROBE|map.getplotbyindex0.raw|table|<table>,nil,nil,nil,nil,nil',
        # fixed city/plot anchoring: PlayerManager routes (cities_read
        # mirror) win over the Game.GetPlayers() scan, city:GetPlot() works
        'PROBE|city.anchor.route|enum|playermanager.majors',
        'PROBE|city.enum.majors|enum|pid=0,major=true,barb=false,cities=1',
        'PROBE|city.enum.majors.count|enum|1',
        'PROBE|city.enum.alive|enum|pid=1,major=false,barb=false,cities=1',
        'PROBE|city.enum.players|enum|true',
        'PROBE|city.getplot.raw|table|<table>,nil,nil,nil,nil,nil',
        'PROBE|city.xy|enum|x=10,y=4',
        'PROBE|p0.religion.getfaithyield|number|6',
        'PROBE|p0.anchor|table|<table>',
        'PROBE|p0.treasury.getgold.raw|number|12,nil,nil,nil,nil,nil',
        'PROBE|p0.treasury.getgoldbalance|number|12.5',
        'PROBE|p0.culture.getprogressingcivic|number|1',
        'PROBE|p0.culture.hascivic.arg|boolean|true',
        'PROBE|p0.influence.getsuzerain.raw|number|42,nil,nil,nil,nil,nil',
        'PROBE|playerconfig.p0.getleadertypename|string|LEADER_TRAJAN',
        'PROBE|plot.center.iscity|boolean|true',
        'PROBE|plot.neighbor.iscity|boolean|false',
        'PROBE|city.iscapital|boolean|true',
        'PROBE|city.hasbuilding.monument|boolean|true',
        'PROBE|city.districts.d1.gettype|number|5',
        'PROBE|city.buildqueue.getcurrentproductiontypehash.raw|number|0,nil,nil,nil,nil,nil',
        # the minor queue: GetBuildQueue() exists but returns nil — the exact
        # live question the probe must answer for a MINOR's city
        'PROBE|minorcity.buildqueue|nil|nil',
        'PROBE|minorcity.buildqueue.getcurrentproductiontypehash|missing|',
    ):
        assert expected in rows, expected
