"""Execute generated read Lua against source-shaped mocks; never a native game client."""
import pytest

from civ_arena.arena.visibility import VisibilityPolicy
from civ_arena.game.civ6 import lua_translator, response_parser
from test_live_entity_ids import lua as lua_fixture

lua = lua_fixture
ROW = "UNITROW|u0:1|0|WARRIOR|0|0|{hp}|2|2|20|0|false|false"


@pytest.mark.parametrize("hp,maximum", [(0, 100), (35, 100), (175, 200), (1_000_000, 1_000_000)])
def test_valid_observed_hp_and_maximum(hp, maximum):
    unit = response_parser.parse_units([ROW.format(hp=hp) + f"|{maximum}|true"], qualified=True)[0]
    assert (unit["hp"], unit["max_hp"], unit["health_valid"]) == (hp, maximum, True)


@pytest.mark.parametrize("hp,maximum,valid", [
    ("100", "100", "false"), ("unknown", "100", "false"),
    ("unknown", "unknown", "true"), ("35", "100", "True"),
    ("101", "100", "true"), ("-1", "100", "true"), ("35.0", "100", "true"),
    ("035", "100", "true"), ("1", "0", "true"), ("1", "1000001", "true"),
    ("1", "100", "1"), ("nan", "100", "true"),
])
def test_torn_contradictory_and_invalid_health_rows_refused(hp, maximum, valid):
    with pytest.raises(ValueError, match="health"):
        response_parser.parse_units([ROW.format(hp=hp) + f"|{maximum}|{valid}"], qualified=True)


def test_unknown_owned_health_and_foreign_masking():
    row = ROW.format(hp="unknown") + "|unknown|false"
    native = response_parser.parse_units([row], qualified=True)
    own = VisibilityPolicy().project(native, "units", 0, frozenset({"0,0"}), frozenset())[0]
    assert own["hp"] is own["max_hp"] is own["hp_bucket"] is None
    assert own["health_valid"] is False
    foreign = VisibilityPolicy().project(native, "units", 1, frozenset({"0,0"}), frozenset())[0]
    assert foreign["hp_bucket"] is None
    assert not {"hp", "max_hp", "health_valid"} & set(foreign)
    assert not VisibilityPolicy().project(native, "units", 1, frozenset(), frozenset({"0,0"}))


def test_legacy_rows_do_not_invent_maximum_or_validity():
    for row in [ROW.format(hp=100), ROW.format(hp=100).rsplit("|", 1)[0]]:
        unit = response_parser.parse_units([row], qualified=True)[0]
        assert unit["hp"] == 100
        assert "max_hp" not in unit and "health_valid" not in unit


@pytest.mark.parametrize("maximum,damage,expected", [
    ("100", "65", (35, 100, True)), ("200", "25", (175, 200, True)),
    ("100", "0", (100, 100, True)), ("100", "100", (0, 100, True)),
    ("nil", "0", (None, None, False)), ("100", "nil", (None, None, False)),
    ("error('missing')", "0", (None, None, False)),
    ("100", "101", (None, None, False)), ("100", "-1", (None, None, False)),
    ("100.5", "0", (None, None, False)), ("100", "0/0", (None, None, False)),
])
def test_generated_native_read_validates_real_method_results(lua, maximum, damage, expected):
    source = """
local unit={GetID=function() return 1 end, GetX=function() return 0 end,
 GetY=function() return 0 end, GetType=function() return 1 end,
 GetMaxDamage=function() return MAXIMUM end, GetDamage=function() return DAMAGE end}
local player={GetID=function() return 0 end, IsBarbarian=function() return false end,
 GetUnits=function() return {Members=function() return ipairs({unit}) end} end}
PlayerManager={GetAlive=function() return {player} end}
GameInfo={Units={[1]={UnitType='UNIT_WARRIOR'}}}
""".replace("MAXIMUM", maximum).replace("DAMAGE", damage)
    lines = lua(source + lua_translator.units_read())
    parsed = response_parser.parse_units(lines, qualified=True)[0]
    assert (parsed["hp"], parsed["max_hp"], parsed["health_valid"]) == expected


async def test_sim_health_metadata_does_not_mutate_state_or_digest():
    import copy

    from civ_arena.game.adapter import ObserveKind, ObserveRequest
    from civ_arena.game.sim.simulator import SimulatorAdapter
    adapter = SimulatorAdapter()
    await adapter.setup({"seed": 3})
    before = copy.deepcopy(adapter.state.doc)
    first_digest = adapter.state_hash()
    rows = await adapter.observe(ObserveRequest(kind=ObserveKind.UNITS, player_id=0))
    assert all(row["max_hp"] == 100 and row["health_valid"] for row in rows)
    assert adapter.state.doc == before
    assert adapter.state_hash() == first_digest
    projected = VisibilityPolicy().project(rows, "units", 0, frozenset(), frozenset())
    assert all(row["max_hp"] == 100 and row["health_valid"] for row in projected)
