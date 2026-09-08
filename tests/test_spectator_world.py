"""M4 spectator world: the SPECW reads, the world package, the carriers'
inertness to it (contract §3/§4)."""

from __future__ import annotations

import json
import shutil
import subprocess

import pytest

from civ_arena.game.civ6 import world_capture as wc

ROSTER_STUB = """
local function player(id, major, barb)
  return {GetID=function() return id end, IsMajor=function() return major end,
   IsBarbarian=function() return barb end, IsAlive=function() return true end}
end
PlayerManager={GetAlive=function()
  return {player(0, true, false), player(12, false, false),
          player(63, false, true)}
end}
PlayerConfigurations={
  [0]={GetCivilizationTypeName=function() return 'CIVILIZATION_SPAIN' end,
       GetLeaderTypeName=function() return 'LEADER_PHILIP_II' end},
  [12]={GetCivilizationTypeName=function() return 'CIVILIZATION_GENOA' end,
        GetLeaderTypeName=function() return 'LEADER_GENOA' end},
  [63]={GetCivilizationTypeName=function() return 'CIVILIZATION_BARBARIAN' end,
        GetLeaderTypeName=function() return 'LEADER_BARBARIAN' end},
}
"""


def _texlua(tmp_path, source: str) -> list[str]:
    executable = shutil.which("texlua")
    if executable is None:
        pytest.skip("texlua unavailable for executable world fixtures")
    path = tmp_path / "world.lua"
    path.write_text(source)
    result = subprocess.run([executable, str(path)], capture_output=True,
                            text=True, timeout=10, check=True)
    return result.stdout.splitlines()


def test_roster_read_enumerates_all_player_kinds(tmp_path):
    rows = wc.parse_roster(_texlua(tmp_path, ROSTER_STUB + wc.roster_read()))
    by_pid = {r["player_id"]: r for r in rows}
    assert set(by_pid) == {0, 12, 63}
    assert by_pid[0]["kind"] == "major" and by_pid[0]["is_major"] is True
    assert by_pid[12]["kind"] == "city_state" and by_pid[12]["is_major"] is False
    assert by_pid[63]["kind"] == "barbarian" and by_pid[63]["is_barbarian"] is True
    assert by_pid[0]["civ_name"] == "CIVILIZATION_SPAIN"
    assert by_pid[0]["leader"] == "LEADER_PHILIP_II"
    # Amendment 1.3: level is UNREAD everywhere probed -> never claimed
    assert "level" not in by_pid[0] or by_pid[0].get("level", "") == ""
    assert by_pid[12]["suzerain"] == -1


TILES_STUB = """
GameInfo={Terrains={[2]={TerrainType='TERRAIN_GRASS'}},
 Features={[1]={FeatureType='FEATURE_FOREST'}},
 Resources={[4]={ResourceType='RESOURCE_IRON'}},
 Improvements={[5]={ImprovementType='IMPROVEMENT_FARM'}},
 Districts={[6]={DistrictType='DISTRICT_CITY_CENTER'}}}
local plots={}
local function plot(x, y, owner, ridx)
  return {GetX=function() return x end, GetY=function() return y end,
   GetOwner=function() return owner end,
   GetTerrainType=function() return 2 end,
   GetFeatureType=function() return 1 end,
   GetResourceType=function() return ridx or -1 end,
   GetImprovementType=function() return 5 end,
   GetDistrictType=function() return -1 end,
   IsRiver=function() return true end}
end
plots[1]=plot(0, 0, 0)
plots[2]=plot(1, 0, 12, 4)
plots[3]=plot(2, 0, -1)
Map={GetGridSize=function() return 40, 40 end,
 GetPlotCount=function() return #plots end,
 GetPlotByIndex=function(i) return plots[i] end}
"""


def test_owned_tiles_read_owns_and_framing(tmp_path):
    doc = wc.parse_owned_tiles(
        _texlua(tmp_path, TILES_STUB + wc.owned_tiles_read()))
    assert doc["grid"] == {"w": 40, "h": 40}
    owners = {row["owner"] for row in doc["rows"]}
    assert owners == {0, 12}, "unowned plots never ride the world read"
    iron = next(r for r in doc["rows"] if r["owner"] == 12)
    assert iron["resource"] == "RESOURCE_IRON"  # raw: no seat tech gate
    assert iron["river"] is True and iron["improvement"] == "IMPROVEMENT_FARM"
    bare = next(r for r in doc["rows"] if r["owner"] == 0)
    assert bare["resource"] == ""
    assert doc["truncated"] == 0


def test_palette_read_carries_unsigned_normalized_ints(tmp_path):
    stub = """
PlayerManager={GetAlive=function()
  return {{GetID=function() return 0 end}} end}
UI={GetPlayerColors=function()
  return -15395638, -16656137 end}
"""
    palette = wc.parse_palette(
        _texlua(tmp_path, stub + wc.palette_read()))
    # wire carries the raw SIGNED ints; the parsed doc is UNSIGNED
    assert palette == {0: {"primary": -15395638 + 2**32,
                           "secondary": -16656137 + 2**32}}
    with pytest.raises(ValueError):
        wc.parse_palette(["SPECW|1|palette",
                          f"COLORROW|0|{-2**31 - 1}|0", "---END---"])
    with pytest.raises(ValueError):
        wc.parse_palette(["SPECW|1|palette",
                          f"COLORROW|0|{2**32}|0", "---END---"])


def test_roster_bound_sixty_five_players(tmp_path):
    players = ", ".join(
        f"player({i}, {str(i % 2 == 0).lower()}, false)" for i in range(65))
    stub = f"""
local function player(id, major, barb)
  return {{GetID=function() return id end, IsMajor=function() return major end,
   IsBarbarian=function() return barb end, IsAlive=function() return true end}}
end
PlayerManager={{GetAlive=function() return {{{players}}} end}}
"""
    lines = _texlua(tmp_path, stub + wc.roster_read())
    rows = wc.parse_roster(lines)
    assert len(rows) == wc.MAX_ROSTER
    assert any(r.startswith("ROSTER_TRUNCATED|1") for r in lines)
    doc = wc.package(roster=rows + [dict(rows[0], player_id=999)],
                     players=[], cities=[], tiles={"grid": {"w": 1, "h": 1},
                                                   "rows": [], "truncated": 0},
                     palette=None, after_seat=-1, game_era=None, read_ms=0.0)
    assert len(doc["roster"]) == wc.MAX_ROSTER
    assert doc["truncated"]["roster"] == 1


def test_tiles_bound_four_thousand_ninety_seven_plots(tmp_path):
    stub = """
GameInfo={Terrains={[2]={TerrainType='TERRAIN_GRASS'}}}
local plots={}
for i = 0, 4096 do
  local x = i
  plots[i]={GetX=function() return x end, GetY=function() return 0 end,
   GetOwner=function() return 0 end, GetTerrainType=function() return 2 end,
   GetFeatureType=function() return -1 end, GetResourceType=function() return -1 end,
   GetImprovementType=function() return -1 end, GetDistrictType=function() return -1 end,
   IsRiver=function() return false end}
end
Map={GetGridSize=function() return 4097, 1 end,
 GetPlotCount=function() return 4097 end,
 GetPlotByIndex=function(i) return plots[i] end}
"""
    lines = _texlua(tmp_path, stub + wc.owned_tiles_read())
    doc = wc.parse_owned_tiles(lines)
    assert len(doc["rows"]) == wc.MAX_OWNED_TILES
    assert doc["truncated"] == 1
    assert any(row.startswith("TILES_END|4096") for row in lines)
    assert any(row.startswith("TILES_TRUNCATED|1") for row in lines)


def test_strict_parser_rejections():
    with pytest.raises(ValueError, match="header"):
        wc.parse_roster(["PLAYERROW|0|C|L|true|false|true|?|major|-1"])
    with pytest.raises(ValueError, match="non-roster row"):
        wc.parse_roster(["SPECW|1|roster", "GARBAGE|x", "---END---"])
    with pytest.raises(ValueError, match="9 fields"):
        wc.parse_roster(["SPECW|1|roster",
                         "PLAYERROW|0|C|L|true|false|true|?|major",
                         "---END---"])
    with pytest.raises(ValueError, match="level"):
        wc.parse_roster(["SPECW|1|roster",
                         "PLAYERROW|0|C|L|true|false|true|LEVEL_2|major|-1",
                         "---END---"])
    with pytest.raises(ValueError, match="header"):
        wc.parse_owned_tiles(["GRID|1|1|1", "TILES_END|0", "---END---"])
    with pytest.raises(ValueError, match="TILES_END"):
        wc.parse_owned_tiles(["SPECW|1|tiles", "GRID|1|1|1",
                              "TILES_END|2", "---END---"])
    with pytest.raises(ValueError, match="lacks its TILES_END"):
        wc.parse_owned_tiles(["SPECW|1|tiles", "GRID|1|1|1", "---END---"])
    with pytest.raises(ValueError, match="non-boolean river"):
        wc.parse_owned_tiles(["SPECW|1|tiles", "GRID|1|1|1",
                              "OWNEDROW|0|0|0|TERRAIN_GRASS|-|-|-|-|wet|-1",
                              "TILES_END|1", "---END---"])
    with pytest.raises(ValueError, match="non-color row"):
        wc.parse_palette(["SPECW|1|palette", "PAINT|0|1|2", "---END---"])


def test_package_amendment_two_key_sets_and_city_join():
    roster = [{"player_id": 0, "civ_name": "CIV", "leader": "L", "kind": "major",
               "suzerain": -1, "is_major": True, "is_barbarian": False,
               "alive": True}]
    players = [{"player_id": 0, "civ_name": "CIV", "gold": 100,
                "researched": ["MINING"], "researching": "MINING",
                "alive": True, "science": 6, "culture": 5, "faith": 4,
                "gold_per_turn": 2, "upkeep": 3, "era": 0,
                "progressing_civic": "CIVIC_A", "civic_progress": 7,
                "civic_cost": 60, "civics": ["CIVIC_A"]}]
    cities = [{
        "city_id": "c0:1", "owner": 0, "name": "ARENA", "q": 0, "r": 0,
        "population": 3, "production_queue": ["SCOUT"], "is_major": True,
        "is_capital": True, "hp": 180, "max_hp": 200, "food_bucket": 10,
        "food_threshold": 15, "food_surplus": 3, "turns_to_growth": 5,
        "turns_to_production": 4, "buildings": ["BUILDING_MONUMENT"],
        "districts": ["DISTRICT_CITY_CENTER"]}]
    tiles = {"grid": {"w": 40, "h": 40}, "rows": [
        {"q": 0, "r": 0, "owner": 0, "terrain": "TERRAIN_GRASS",
         "feature": "FEATURE_FOREST", "river": True}], "truncated": 0}
    doc = wc.package(roster=roster, players=players, cities=cities,
                     tiles=tiles, palette=None, after_seat=1,
                     game_era="ERA_ANCIENT", read_ms=1.25)
    # Amendment 2 closed key sets — exact
    assert set(doc["players"][0]) == {
        "player_id", "civ_name", "gold", "gold_per_turn", "science",
        "culture", "faith", "upkeep", "era", "researching", "researched",
        "civics"}
    assert set(doc["cities"][0]) == {
        "city_id", "owner", "q", "r", "name", "population", "is_capital",
        "is_major", "hp", "max_hp", "production_queue", "buildings",
        "districts"}
    # the owned-tile city join: the tile carrying the city center gets the
    # raw engine id, everything else -1
    assert doc["owned_tiles_columns"]["0"][0]["city"] == 1
    assert doc["contexts"] == {"roster": "gamecore", "tiles": "gamecore",
                               "palette": "absent"}
    assert doc["after_seat"] == 1 and doc["game_era"] == "ERA_ANCIENT"
    assert doc["schema"] == 1 and doc["read_ms"] == 1.25
    assert doc["truncated"]["world"] is False
    assert "palette" not in doc
    palette_doc = wc.package(roster=roster, players=[], cities=[],
                             tiles=tiles, palette={0: {"primary": 5,
                                                       "secondary": 6}},
                             after_seat=-1, game_era=None, read_ms=0.0)
    assert palette_doc["contexts"]["palette"] == "ingame"
    assert palette_doc["palette"] == {"0": {"primary": 5, "secondary": 6}}


def test_package_cap_records_truncation_never_silent(monkeypatch):
    big = {"q": 0, "r": 0, "owner": 0, "terrain": "TERRAIN_GRASS",
           "feature": "F" * 64}
    tiles = {"grid": {"w": 1, "h": 1},
             "rows": [dict(big, q=i) for i in range(4000)], "truncated": 0}
    monkeypatch.setattr(wc, "MAX_SNAPSHOT_BYTES", 2048)
    doc = wc.package(roster=[], players=[], cities=[], tiles=tiles,
                     palette=None, after_seat=-1, game_era=None, read_ms=0.0)
    assert doc["truncated"]["world"] is True
    assert not doc.get("owned_tiles_columns"), "the biggest block dropped"
    assert doc["roster"] == [] and doc["grid"] == {"w": 1, "h": 1}


async def test_capture_against_fake_mod_with_minors():
    from civ_arena.game.civ6.fake_tuner_server import FakeMod, FakeTunerServer
    from civ_arena.game.civ6.firetuner import FireTunerAdapter

    server = FakeTunerServer(mod=FakeMod(minors=True))
    port = await server.start()
    adapter = FireTunerAdapter("127.0.0.1", port)
    try:
        await adapter.setup({})
        world = await wc.capture(adapter, turn=3, after_seat=0,
                                 include_palette=True)
        kinds = {r["kind"] for r in world["roster"]}
        assert kinds == {"major", "city_state"}
        roster = {r["player_id"]: r for r in world["roster"]}
        assert roster[12]["kind"] == "city_state"
        # economy rows: majors only, Amendment-2 keys only
        assert [p["player_id"] for p in world["players"]] == [0, 1]
        assert all("alive" not in p and "civic_progress" not in p
                   for p in world["players"])
        assert all(p["gold"] == 100 for p in world["players"])
        # cities: the city-state's city rides the world (never OVX)
        city_owners = {c["owner"] for c in world["cities"]}
        assert city_owners == {0, 12}
        cs = next(c for c in world["cities"] if c["owner"] == 12)
        assert cs["is_major"] is False and cs["name"] == "CITYSTATE"
        # territory columns cover both owners; palette unsigned ints
        assert set(world["owned_tiles_columns"]) >= {"0", "12"}
        assert world["contexts"]["palette"] == "ingame"
        assert all(0 <= v <= 2**32 - 1
                   for colors in world["palette"].values()
                   for v in colors.values())
        assert world["game_era"] == "ERA_FAKE"
        assert world["fog_audit"]["requested"] == 0
    finally:
        await adapter.teardown()
        await server.stop()


# -- scope inertness (contract §4a) -------------------------------------------


def _spectator_world_records(world: dict | None = None) -> list[dict]:
    world = world or {"schema": 1, "after_seat": 0, "roster": [], "players": []}
    base = {"schema": 1, "ts": "2026-09-08T00:00:00+00:00",
            "match_id": "m", "game_instance_id": "gi"}
    return [
        {**base, "seq": 0, "kind": "MATCH_START", "turn": 0,
         "phase_player_id": -1, "player_id": None, "agent_id": None,
         "visibility_scope": "referee", "config": {}},
        {**base, "seq": 1, "kind": "HEARTBEAT", "turn": 1,
         "phase_player_id": -1, "player_id": None, "agent_id": None,
         "visibility_scope": "spectator", "audit": "spectator_world",
         "after_seat": 0, "world": world},
        {**base, "seq": 2, "kind": "HEARTBEAT", "turn": 2,
         "phase_player_id": -1, "player_id": None, "agent_id": None,
         "visibility_scope": "spectator", "audit": "spectator_world_failed",
         "after_seat": 1, "error": "TimeoutError: <redacted>"},
    ]


def test_spectator_world_is_inert_to_every_consumer(tmp_path):
    from civ_arena import dashboard_map
    from civ_arena.dashboard import DashboardStore
    from civ_arena.productive_map import project
    from civ_arena.replay import load_calls
    from civ_arena.strategy.store import StrategyStore
    from test_minimap import packet, source

    records = _spectator_world_records()
    # strategy store: claims/observations only — the audit records vanish
    store = StrategyStore.from_log(records)
    assert store.goals == {} and store.predictions == {}
    assert store.current_goals(0) == []
    # productive map: production receipts only
    assert project(records, 0) == []
    # replay call loading: tool calls only
    assert load_calls(records, {}) == {}
    # dashboard_map: one REAL strategy_request packet so the map surface
    # materializes, with the spectator audits interleaved — the player
    # route must see exactly the packet and nothing spectator-shaped
    request = source(packet())
    request["seq"] = 1
    request["match_id"] = "m"
    request["game_instance_id"] = "gi"
    world_audit, failed_audit = records[1], records[2]
    world_audit["seq"] = 2
    failed_audit["seq"] = 3
    map_records = [records[0], request, world_audit, failed_audit]

    class _Redactor:
        @staticmethod
        def sensitive(key):
            return False

        def clean(self, value):
            return value

        def text(self, value):
            return value

    raw = ("\n".join(json.dumps(r, sort_keys=True) for r in map_records)
           + "\n").encode()
    materialized = dashboard_map.materialize(
        raw, player=0, spectator=False, turn=5, redactor=_Redactor())
    assert "spectator_world" not in json.dumps(materialized, default=str)
    # DashboardStore: a minimal hotseat-shaped run carrying the audits
    # loads without the world bleeding into the payload
    run_root = tmp_path / "runs"
    run = run_root / "hotseat-x"
    run.mkdir(parents=True)
    summary = {"match_id": "m", "game_instance_id": "gi", "final_turn": 1,
               "aborted": None, "violations_total": 0,
               "final_state_hash": "h", "telemetry": {}, "scores": {},
               "phase": "dispatch-hotseat", "per_turn": [],
               "completed_rounds": 0, "requested_rounds": 0, "clean": True,
               "cleanup": {"status": "completed"}}
    (run / "events.jsonl").write_text(
        "\n".join(json.dumps(r, sort_keys=True) for r in map_records) + "\n")
    (run / "summary.json").write_text(json.dumps(summary, sort_keys=True))
    payload = DashboardStore(run_root).load("hotseat-x")
    dumped = json.dumps(payload, default=str)
    assert "spectator_world" not in dumped
    assert payload["status"] in {"completed", "incomplete", "aborted"}


def test_validate_spectate_accepts_snapshots_with_and_without_world():
    """validate_spectate's structural checks never inspect the world block
    (with-world and without-world records are both acceptable shapes)."""
    from civ_arena.spectate_audit import structural_problems

    base = {"schema": 1, "ts": "2026-09-08T00:00:00+00:00",
            "match_id": "m", "game_instance_id": "gi",
            "phase_player_id": 0, "player_id": None, "agent_id": None,
            "visibility_scope": "spectator"}
    with_world = [{**base, "seq": 0, "kind": "MATCH_START", "turn": 0},
                  {**base, "seq": 1, "kind": "HUMAN_TURN_START", "turn": 1,
                   "window": "hook_observed", "boundary": "hook_observed"},
                  {**base, "seq": 2, "kind": "SPECTATOR_SNAPSHOT", "turn": 1,
                   "round": 1, "phase": "turn_start", "boundary": "hook_observed",
                   "digest": {"before": "a", "after": "a", "consistent": True},
                   "world": {"schema": 1, "digest_consistent": True}},
                  {**base, "seq": 3, "kind": "HUMAN_TURN_END", "turn": 1}]
    without = [{k: v for k, v in rec.items() if k != "world"}
               for rec in with_world]
    for records in (with_world, without):
        problems = structural_problems(records, summary=None,
                                       allow_open_prefix=True)
        assert not any("world" in problem for problem in problems), problems
