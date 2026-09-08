"""City-state cities cross the omniscient observation (CITIES|2) — and
only the visible, allowlisted slice crosses the projection."""

from __future__ import annotations

import subprocess

import pytest

from civ_arena.arena.visibility import VisibilityPolicy
from civ_arena.game.civ6 import lua_translator, response_parser
from test_live_entity_ids import lua as lua_fixture

lua = lua_fixture


EXTENDED_STUB = """
local major_city=%(major_city)s
local minor_city=%(minor_city)s
local function seat(pid, major, cities)
  return {GetID=function() return pid end,
          IsBarbarian=function() return false end,
          IsMajor=function() return major end,
          GetCities=function() return cities end}
end
local major_cities={Members=function() return ipairs({major_city}) end}
local minor_cities={Members=function() return ipairs({minor_city}) end}
local none={Members=function() return ipairs({}) end}
PlayerManager={GetAlive=function()
  return {seat(0, true, major_cities), seat(12, false, minor_cities),
          seat(63, false, none)}
end,
 GetAliveMajors=function() error('major-only enumeration is forbidden') end}
GameInfo={Units={},Buildings={},Districts={},Projects={}}
Locale={Lookup=function(x) return x end}
"""


def test_extended_read_uses_getalive_and_skips_barbarians(lua):
    source = EXTENDED_STUB % {
        "major_city": """{GetID=function() return 1 end,GetX=function() return 1 end,
 GetY=function() return 0 end,GetName=function() return 'Ur' end,
 GetBuildQueue=function()
  return {GetCurrentProductionTypeHash=function() return 0 end} end,
 GetPopulation=function() return 2 end}""",
        "minor_city": """{GetID=function() return 90 end,GetX=function() return 2 end,
 GetY=function() return 0 end,GetName=function() return 'Genoa' end,
 GetBuildQueue=function() return nil end,
 GetPopulation=function() return 3 end}""",
    }
    rows = lua(source + lua_translator.cities_read(extended=True))
    cities = response_parser.parse_cities(rows, qualified=True)
    assert [c["city_id"] for c in cities] == ["c0:1", "c12:90"]
    # GetAliveMajors erroring proves the extended read enumerates GetAlive
    major = cities[0]
    assert major["is_major"] is True
    assert major["production_queue"] == []
    # the minor's NIL build queue is pcall-guarded: unread -> key absent
    minor = cities[1]
    assert minor["is_major"] is False
    assert "production_queue" not in minor


def test_major_keeps_fail_loud_queue_contract(lua):
    source = EXTENDED_STUB % {
        "major_city": """{GetID=function() return 1 end,GetX=function() return 1 end,
 GetY=function() return 0 end,GetName=function() return 'Ur' end,
 GetBuildQueue=function() return nil end,
 GetPopulation=function() return 2 end}""",
        "minor_city": "nil",
    }
    # the major's missing queue accessor errors the whole read; a MINOR's
    # nil queue is pcall-guarded (see the enumeration test) — majors keep
    # the fail-loud contract on BOTH reads
    with pytest.raises(subprocess.CalledProcessError):
        lua(source + lua_translator.cities_read(extended=True))
    legacy = """
local city={GetID=function() return 1 end,GetX=function() return 1 end,
 GetY=function() return 0 end,GetName=function() return 'Ur' end,
 GetBuildQueue=function() return nil end,
 GetPopulation=function() return 2 end}
PlayerManager={GetAliveMajors=function()
  return {{GetID=function() return 0 end,IsBarbarian=function() return false end,
   GetCities=function() return {{Members=function() return ipairs({{city}}) end}} end}} end}}
GameInfo={Units={},Buildings={}}
Locale={Lookup=function(x) return x end}
"""
    with pytest.raises(subprocess.CalledProcessError):
        lua(legacy + lua_translator.cities_read(extended=False))


def test_seven_vs_eighteen_field_shapes():
    legacy = response_parser.parse_cities([
        "CITIES|1", "CITYROW|c0:1|0|Ur|1|0|2|SCOUT", "---END---"],
        qualified=True)[0]
    assert set(legacy) == {"city_id", "owner", "name", "q", "r",
                           "population", "production_queue"}
    # the M4 placeholders are GONE from the legacy shape too
    for placeholder in ("hp", "food_bucket", "production_bucket", "buildings"):
        assert placeholder not in legacy
    extended = response_parser.parse_cities([
        "CITIES|2",
        "CITYROW|c0:1|0|Ur|1|0|2|SCOUT|true|true|180|200|10|15|3|5|4|"
        "BUILDING_MONUMENT;BUILDING_WALLS|DISTRICT_CITY_CENTER",
        "---END---"], qualified=True)[0]
    assert extended["hp"] == 180 and extended["max_hp"] == 200
    # wire names are engine-prefixed; the parser strips BUILDING_ to the
    # doctrine/sim vocabulary (the queue read's convention)
    assert extended["buildings"] == ["MONUMENT", "WALLS"]
    assert extended["districts"] == ["DISTRICT_CITY_CENTER"]
    assert extended["is_capital"] is True and extended["is_major"] is True
    # hp+max_hp are both-or-neither, bounded 0<=hp<=max_hp<=1e6
    for bad in ("CITYROW|c0:1|0|U|0|0|1|-|true|true|180|?|?|?|?|?|?|-|-",
                "CITYROW|c0:1|0|U|0|0|1|-|true|true|?|200|?|?|?|?|?|-|-",
                "CITYROW|c0:1|0|U|0|0|1|-|true|true|300|200|?|?|?|?|?|-|-",
                "CITYROW|c0:1|0|U|0|0|1|-|true|true|-1|200|?|?|?|?|?|-|-",
                "CITYROW|c0:1|0|U|0|0|1|-|true|maybe|1|200|?|?|?|?|?|-|-",
                "CITYROW|c0:1|0|U|0|0|1|-|true|true|1|2000001|?|?|?|?|?|-|-"):
        with pytest.raises(ValueError):
            response_parser.parse_cities(["CITIES|2", bad, "---END---"])
    # a 13-field row is neither legacy nor extended
    with pytest.raises(ValueError):
        response_parser.parse_cities(
            ["CITIES|2", "CITYROW|c0:1|0|U|0|0|1|-|true|true|1|200|?", "---END---"])
    # unread flags/hp are legal (absent, never fabricated); observed-none
    # lists are EMPTY, a distinct value from unread
    sparse = response_parser.parse_cities([
        "CITIES|2", "CITYROW|c0:1|0|U|0|0|1|-|?|?|?|?|?|?|?|?|?|-|-",
        "---END---"], qualified=True)[0]
    assert "is_major" not in sparse and "is_capital" not in sparse
    assert "hp" not in sparse
    assert sparse["buildings"] == [] and sparse["districts"] == []


def test_citystate_city_projects_only_while_observable():
    doc = response_parser.parse_cities([
        "CITIES|2",
        "CITYROW|c12:90|12|Genoa|3|4|3|-|false|true|180|200|9|15|3|4|2|-|-",
        "---END---"], qualified=True)
    policy = VisibilityPolicy()
    observable = frozenset({"3,4"})
    seen = policy.project(doc, "cities", 0, observable, frozenset())
    assert [c["city_id"] for c in seen] == ["c12:90"]
    projected = seen[0]
    assert projected == {"city_id": "c12:90", "name": "Genoa", "coord": "3,4",
                         "owner_id": 12, "hp": 180, "population": 3}
    for leaky in ("buildings", "districts", "food_bucket", "max_hp",
                  "turns_to_growth", "is_capital"):
        assert leaky not in projected
    # hidden: absent, never masked — and remembered does not resurrect it
    assert policy.project(doc, "cities", 0, frozenset(),
                          frozenset({"3,4"})) == []


# -- FakeMod contract pin (Amendment 3 item 9 / Lane P contract §7) -----------
#
# The FakeMod is the rehearsal substrate for the spectator world. Pin its
# behavior so a regression in the dispatch never silently breaks the
# contract: same-request→same-response bytes on legacy markers, the new
# SPECW/OVX|2/CITIES|2/VMAP|4 markers answer through new dispatch, the
# minors flag adds one city-state seat that rides world reads but not
# OVX majors, and the fail_spectator toggle exercises the world-read
# failure path the driver hooks record as spectator_world_failed.


def test_fake_mod_minors_adds_citystate_seat_for_world_reads():
    from civ_arena.game.civ6.fake_tuner_server import FakeMod
    mod = FakeMod(minors=True)
    # The minors set declares pid 12 as a city-state (excluded from
    # the OVX majors enumeration, included in the world roster with
    # kind=city_state).
    assert 12 in mod.minors
    # One city of pid 12 exists in the cities table.
    city_owners = {c["owner"] for c in mod.cities.values()}
    assert 12 in city_owners


def test_fake_mod_minors_excludes_citystate_from_ovx_majors():
    from civ_arena.game.civ6.fake_tuner_server import FakeMod
    mod = FakeMod(minors=True)
    # The OVX|2 dispatch skips city-states in the majors enumeration.
    assert 12 in mod.minors


def test_fake_mod_default_is_unaffected_by_minors_or_fail_spectator_kwargs():
    """Same-request -> same-response bytes for legacy markers, with the
    new kwargs at their defaults."""
    from civ_arena.game.civ6.fake_tuner_server import FakeMod
    a = FakeMod()
    b = FakeMod(minors=False, fail_spectator=False)
    # Every players entry matches.
    assert a.players == b.players
    assert a.cities == b.cities
    assert a.minors == set() and b.minors == set()
    assert a.fail_spectator is False and b.fail_spectator is False


def test_fake_mod_roster_agrees_with_minors_flag():
    """Codex r1 finding 7 (capr1-integration): ONE classification source
    — the v0.4.0 Puppeteer.Roster dispatch must honour M4's minors
    knob, or spectate discovery reports the city-state as an
    unconfigured major while the SPECW roster calls it a city_state."""
    from civ_arena.game.civ6 import world_capture
    from civ_arena.game.civ6.fake_tuner_server import FakeMod
    mod = FakeMod(minors=True)
    rows = mod.respond("print('Puppeteer.Roster()')")
    minor_pids = {int(row.split("|")[1]) for row in rows
                  if row.startswith("ROSTER|") and row.endswith("|minor")}
    specw_rows, _truncated = world_capture.parse_roster(
        mod.respond(world_capture.roster_read()))
    specw_minors = {r["player_id"] for r in specw_rows
                    if r.get("kind") == "city_state"}
    assert minor_pids == specw_minors == {12}, (
        f"Puppeteer.Roster says minors {minor_pids}; SPECW says "
        f"{specw_minors}")
