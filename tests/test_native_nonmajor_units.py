"""Native player classification crosses only the visible-unit boundary."""

import pytest

from civ_arena.arena.visibility import VisibilityPolicy
from civ_arena.game.civ6 import lua_translator, response_parser
from test_live_entity_ids import lua as lua_fixture

lua = lua_fixture


ROW = 'UNITROW|u63:131073|63|WARRIOR|1|0|100|2|2|20|0|false'


@pytest.mark.parametrize('token,expected', [('true', True), ('false', False)])
def test_explicit_native_boolean(token, expected):
    unit = response_parser.parse_units([ROW + '|' + token], qualified=True)[0]
    assert unit['is_barbarian'] is expected


@pytest.mark.parametrize('token', ['', '1', '0', 'True', 'FALSE', 'nil', 'unknown', 'true|x'])
def test_invalid_native_boolean_rejected(token):
    with pytest.raises(ValueError):
        response_parser.parse_units([ROW + '|' + token], qualified=True)


def test_legacy_missing_classification_stays_absent():
    unit = response_parser.parse_units([ROW], qualified=True)[0]
    assert 'is_barbarian' not in unit
    projected = VisibilityPolicy().project([unit], 'units', 0, frozenset({'1,0'}), frozenset())
    assert 'is_barbarian' not in projected[0]


def test_all_alive_native_units_visible_and_hidden_projection(lua):
    source = '''
local function player(id, barbarian, x)
 local unit={GetID=function() return 131073 end,GetX=function() return x end,
 GetY=function() return 0 end,GetType=function() return 1 end}
 return {GetID=function() return id end,IsBarbarian=function() return barbarian end,
 GetUnits=function() return {Members=function() return ipairs({unit}) end} end}
end
local major=player(0,false,0)
local citystate=player(12,false,1)
local barbarian=player(63,true,1)
local hidden=player(62,true,99)
PlayerManager={GetAlive=function() return {major,citystate,barbarian,hidden} end,
 GetAliveMajors=function() error('major-only enumeration is forbidden') end}
GameInfo={Units={[1]={UnitType='UNIT_WARRIOR'}}}
'''
    units = response_parser.parse_units(lua(source + lua_translator.units_read()), qualified=True)
    assert len(units) == 4
    visible = VisibilityPolicy().project(units, 'units', 0, frozenset({'0,0', '1,0'}),
                                         frozenset({'99,0'}))
    assert {u['unit_id'] for u in visible} == {'u0:131073', 'u12:131073', 'u63:131073'}
    by_owner = {u['owner_id']: u for u in visible}
    assert by_owner[0]['is_barbarian'] is False
    assert by_owner[12]['is_barbarian'] is False
    assert by_owner[63]['is_barbarian'] is True
    assert 'movement' not in by_owner[63]
    assert 'fortified' not in by_owner[12]


@pytest.mark.parametrize('bad', [1, 0, 'true', None, {'secret': True}])
def test_projection_does_not_promote_malformed_classification(bad):
    unit = response_parser.parse_units([ROW], qualified=True)[0]
    unit.update(is_barbarian=bad, secret='private')
    projected = VisibilityPolicy().project([unit], 'units', 0, frozenset({'1,0'}), frozenset())
    assert 'is_barbarian' not in projected[0]
    assert 'secret' not in projected[0]
