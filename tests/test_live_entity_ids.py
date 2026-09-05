"""Owner-qualified identity across live observations, commands and accounting."""

import shutil
import subprocess
from types import SimpleNamespace

import pytest

from civ_arena.arena.referee import Referee
from civ_arena.game.adapter import ActionCommand, MutationRecord
from civ_arena.game.civ6 import entity_ids
from civ_arena.game.civ6 import lua_translator as lt
from civ_arena.game.civ6 import response_parser as rp
from civ_arena.game.civ6.firetuner import FireTunerAdapter
from test_mod_initial_attach import run_mod  # noqa: F401


@pytest.fixture
def lua(tmp_path):
    executable = shutil.which('texlua')
    if executable is None:
        pytest.skip('texlua unavailable for executable identity fixtures')

    def run(code):
        path = tmp_path / 'identity.lua'
        path.write_text(code)
        result = subprocess.run([executable, str(path)], capture_output=True,
                                text=True, timeout=5, check=True)
        return result.stdout.splitlines()
    return run


@pytest.mark.parametrize('value', ['u131073', 'u01:7', 'u1:07', 'u-1:7',
                                  'u1:2;evil()', 'c1:2', 'u1:9007199254740992'])
def test_live_unit_decoder_rejects_ambiguous_or_inexact_ids(value):
    with pytest.raises(ValueError):
        entity_ids.decode(value, 'u')
    with pytest.raises(ValueError):
        lt.move_unit(value, '0,0')


def test_qualified_projection_cannot_collide_with_other_owner_or_generation():
    rows = ['UNITROW|u0:131073|0|WARRIOR|0|0|100|2|2|20|0|false',
            'UNITROW|u1:65537|1|WARRIOR|1|0|100|2|2|20|0|false',
            'UNITROW|u1:131073|1|WARRIOR|2|0|100|2|2|20|0|false']
    units = rp.parse_units(rows, qualified=True)
    assert [u['unit_id'] for u in units] == ['u0:131073', 'u1:65537', 'u1:131073']
    assert rp.parse_cities(['CITYROW|c1:131073|1|Uruk|0|0|1|-'], qualified=True)[0][
        'city_id'] == 'c1:131073'
    with pytest.raises(ValueError, match='owner'):
        rp.parse_units([rows[0].replace('|0|WARRIOR', '|1|WARRIOR')], qualified=True)
    # Historical observation/ledger reads retain bytes; they cannot be executed live.
    assert rp.parse_units(['UNITROW|131073|1|WARRIOR|0|0|100|2|2|20|0|false'])[0][
        'unit_id'] == 'u131073'
    with pytest.raises(ValueError):
        rp.parse_units(['UNITROW|131073|1|WARRIOR|0|0|100|2|2|20|0|false'], qualified=True)
    with pytest.raises(ValueError):
        rp.parse_ledger_lines(['LEDGER|unit.moves|unit|u131073|moves|2|0'], qualified=True)
    with pytest.raises(ValueError, match='duplicate'):
        rp.parse_units([rows[0], rows[0]], qualified=True)


def test_digest_identity_is_owner_bound_and_unique():
    digest = 'u0:131073|0|0|0|0|0;u1:131073|1|11|25|0|0;c1:65536|1|1'
    assert rp.parse_digest(['DIGEST|' + digest], qualified=True) == digest
    for bad in ('u131073|1|0|0|0|0', 'u0:131073|1|0|0|0|0',
                'u0:1|0|0|0|0|0;u0:1|0|0|0|0|0'):
        with pytest.raises(ValueError):
            rp.parse_digest(['DIGEST|' + bad], qualified=True)


@pytest.mark.parametrize('local_owner, returned_id, expected', [
    (1, 131073, 1), (0, 131073, 0), (1, 1, 0),
])
def test_move_uses_exact_engine_id_and_owner_before_request(lua, local_owner, returned_id,
                                                        expected):
    code = f"""
requests=0
Game={{GetLocalPlayer=function() return {local_owner} end}}
UnitOperationTypes={{MOVE_TO=1, PARAM_X='x', PARAM_Y='y'}}
UnitManager={{
 GetUnit=function(owner, raw)
   assert(owner==1 and raw==131073)
   return {{GetID=function() return {returned_id} end,
    GetMovesRemaining=function() return 2 end, GetX=function() return 0 end,
    GetY=function() return 0 end}}
 end,
 CanStartOperation=function() return true end,
 RequestOperation=function() requests=requests+1 end}}
local function act()
{lt.move_unit('u1:131073', '2,3')}
end
act()
assert(requests=={expected})
print('REQUESTS|'..requests)
"""
    assert lua(code)[-1] == f'REQUESTS|{expected}'


def test_attack_target_keeps_explicit_foreign_owner_and_full_id(lua):
    rows = lua(f"""
requests=0
Game={{GetLocalPlayer=function() return 0 end}}
GameInfo={{Units={{[1]={{RangedCombat=1}}}}}}
Map={{GetPlotDistance=function() return 2 end}}
UnitOperationTypes={{RANGE_ATTACK=1,PARAM_X='x',PARAM_Y='y'}}
UnitManager={{GetUnit=function(owner, raw)
 assert((owner==0 and raw==131073) or (owner==1 and raw==65537))
 return {{GetID=function() return raw end, GetX=function() return 0 end,
 GetY=function() return 0 end,GetType=function() return 1 end}}
 end,CanStartOperation=function() return true end,
 RequestOperation=function() requests=requests+1 end}}
local function act()
{lt.attack('u0:131073','u1:65537')}
end
act()
assert(requests==1)
print('REQUESTS|'..requests)
""")
    assert rows[-1] == 'REQUESTS|1'


def test_mod_ledger_and_lease_helpers_bind_owner_and_full_raw_id(run_mod):  # noqa: F811
    rows = run_mod("""
units[1].id=131073
Puppeteer.SetPuppet(1,true)
localPlayer=1
Puppeteer.AttachCurrentTurn(1,1)
Puppeteer.RestoreUnit(131073,0)
assert(restores==0)
Puppeteer.RestoreUnit(131073,1)
assert(restores==1 and units[1].moves==2)
Puppeteer.FreezeUnit(131073,0)
assert(units[1].moves==2)
print(Puppeteer.DiffSinceLast('moves',1))
Puppeteer.FreezeUnit(131073,1)
assert(units[1].moves==0)
""")
    assert 'LEDGER|unit.moves|unit|u1:131073|moves|0|2' in rows
    assert not any('|unit|u131073|' in row for row in rows)


async def test_allowance_admits_own_large_id_and_rejects_foreign_same_raw_id():
    units = rp.parse_units([
        'UNITROW|u0:131073|0|WARRIOR|0|0|100|0|2|20|0|false',
        'UNITROW|u1:131073|1|WARRIOR|11|20|100|0|2|20|0|false'], qualified=True)
    journal = [MutationRecord.from_doc(doc) for doc in rp.parse_ledger_lines([
        'LEDGER|unit.moved|unit|u1:131073|pos|10,23|11,25',
        'LEDGER|unit.moves|unit|u1:131073|moves|2|0',
        'LEDGER|unit.moved|unit|u0:131073|pos|0,0|1,0',
        'LEDGER|unit.damage|unit|u1:131073|damage|0|10'], qualified=True)]

    async def observe(request):
        return units

    audits = []
    subject = SimpleNamespace(adapter=SimpleNamespace(observe=observe, _journal=journal),
                              _ls=SimpleNamespace(acknowledged=[]),
                              log=SimpleNamespace(write=lambda kind, **kw: audits.append(kw)),
                              match_id='identity-fixture', game_instance_id='fixture')
    await Referee._declare_own_endpath_drift(
        subject, SimpleNamespace(player_id=1, turn=3, agent_id='seat1'))
    assert subject._ls.acknowledged == journal[:2]
    assert audits[0]['owners'] == {'u1:131073': 1}
    assert [m['attr'] for m in audits[0]['mutations']] == ['pos', 'moves']


async def test_adapter_rejects_foreign_or_legacy_identity_before_any_wire():
    adapter = FireTunerAdapter()
    adapter._phase_open = 0
    for unit_id, reason in [('u1:131073', 'not_your_unit'), ('u131073', 'args_invalid')]:
        result = await adapter.act(ActionCommand(tool='move_unit',
            args={'unit_id': unit_id, 'dest': '0,0'}, player_id=0,
            idempotency_key='fixture', lease_id='fixture'))
        assert result.rejection == reason


async def test_adapter_refuses_old_mod_before_driving():
    adapter = FireTunerAdapter()

    async def handshake():
        return {'mod_version': '0.3.3', 'supports_freeze': True, 'supports_ledger': True,
                'supports_digest': True, 'supports_command_diff': True}

    adapter.mod_handshake = handshake
    with pytest.raises(RuntimeError, match='owner-qualified'):
        await adapter.require_mod()


def test_units_and_cities_generated_read_uses_qualified_raw_identity(lua):
    prelude = """
local unit={GetID=function() return 131073 end,GetX=function() return 1 end,
 GetY=function() return 0 end,GetType=function() return 1 end}
local city={GetID=function() return 65537 end,GetX=function() return 1 end,
 GetY=function() return 0 end,GetName=function() return 'Uruk' end}
local player={GetID=function() return 1 end,
 GetUnits=function() return {Members=function() return ipairs({unit}) end} end,
 GetCities=function() return {Members=function() return ipairs({city}) end} end}
PlayerManager={GetAliveMajors=function() return {player} end}
GameInfo={Units={[1]={UnitType='UNIT_WARRIOR'}},Buildings={}}
Locale={Lookup=function(x) return x end}
"""
    units = rp.parse_units(lua(prelude + lt.units_read()), qualified=True)
    cities = rp.parse_cities(lua(prelude + lt.cities_read()), qualified=True)
    assert units[0]['unit_id'] == 'u1:131073'
    assert cities[0]['city_id'] == 'c1:65537'


@pytest.mark.parametrize('builder', [lt.set_city_production, lt.purchase])
def test_city_action_builders_keep_full_raw_identity(builder):
    source = builder('c1:131073', 'MONUMENT')
    assert 'CityManager.GetCity(me, 131073)' in source
    assert 'if me ~= 1 then' in source
    assert 'pCity:GetID() ~= 131073' in source


def test_city_read_builders_keep_full_raw_identity():
    for source in (lt.current_production_read('c1:131073'),
                   lt.available_production_read('c1:131073')):
        assert 'CityManager.GetCity(me, 131073)' in source
        assert 'if me ~= 1 then' in source
        assert 'pCity:GetID() ~= 131073' in source


@pytest.mark.parametrize('local_owner, returned_id, expected', [
    (1, 131073, 1), (0, 131073, 0), (1, 1, 0),
])
def test_city_production_checks_owner_and_exact_generation_before_mutation(
        lua, local_owner, returned_id, expected):
    rows = lua(f"""
requests=0
Game={{GetLocalPlayer=function() return {local_owner} end}}
GameInfo={{Units={{}},Buildings={{BUILDING_MONUMENT={{Hash=9}}}}}}
CityOperationTypes={{PARAM_BUILDING_TYPE='building',BUILD=1,
 PARAM_INSERT_MODE='insert',VALUE_EXCLUSIVE=1}}
CityManager={{
 GetCity=function(owner, raw)
   assert(owner==1 and raw==131073)
   return {{GetID=function() return {returned_id} end,
     GetBuildQueue=function() return {{GetTurnsLeft=function() return 1 end,
      GetCurrentProductionTypeHash=function() return requests>0 and 9 or 0 end}} end}}
 end,
 CanStartOperation=function() return true end,
 RequestOperation=function() requests=requests+1 end}}
local function act()
{lt.set_city_production('c1:131073','MONUMENT')}
end
act()
assert(requests=={expected})
print('REQUESTS|'..requests)
""")
    assert rows[-1] == f'REQUESTS|{expected}'
