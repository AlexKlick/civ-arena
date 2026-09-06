"""Native-event causal attribution; no Civ6, desktop, or provider access."""
import json
import shutil
import subprocess
from unittest.mock import AsyncMock

import pytest

from civ_arena.game.adapter import ActionCommand
from civ_arena.game.civ6 import lua_translator as lt
from civ_arena.game.civ6 import response_parser as rp
from civ_arena.game.civ6.firetuner import FireTunerAdapter
from test_mod_initial_attach import ENGINE, MOD

NONCE = 'a' * 64
ATTRS = 'pos,moves,damage,exists'
NATIVE = '''
Events.GoodyHutReward={Add=function(fn) hooks.reward=fn end,
                      Remove=function(fn) if hooks.reward==fn then hooks.reward=nil end end}
function city(id,x)
  return {id=id, x=x, pop=3, GetID=function(s) return s.id end,
          GetPopulation=function(s) return s.pop end, GetOwner=function() return 0 end,
          GetX=function(s) return s.x end, GetY=function() return 0 end}
end
cities={city(30,1)}
Players[0].GetCities=function() return {Members=function() return ipairs(cities) end} end
units[1].x=0
units[1].GetX=function(s) return s.x end
improvement=1
Map={GetPlot=function() return {GetImprovementType=function() return improvement end} end,
     GetPlotDistance=function(x,y,cx,cy) return math.abs(x-cx)+math.abs(y-cy) end}
GameInfo={Improvements={[1]={ImprovementType='IMPROVEMENT_GOODY_HUT'}},GoodyHuts={[1892398955]={GoodyHutType='GOODYHUT_SURVIVORS'}},
  GoodyHutSubTypes={[16]={SubTypeGoodyHut='GOODYHUT_ADD_POP',
    GoodyHut='GOODYHUT_SURVIVORS',ModifierID='GOODY_SURVIVORS_ADD_POPULATION'}},
  Modifiers={GOODY_SURVIVORS_ADD_POPULATION={
    ModifierType='MODIFIER_PLAYER_NEAREST_CITY_ADD_POPULATION'}},
  ModifierArguments=function()
    local done=false
    return function() if not done then done=true; return {
      ModifierId='GOODY_SURVIVORS_ADD_POPULATION',Name='Amount',Value='1'} end end
  end}
'''
NATIVE += '''
-- The live subtype table has string/ordinal access but no hash lookup.
GameInfo.GoodyHutSubTypes.GOODYHUT_ADD_POP = GameInfo.GoodyHutSubTypes[16]
DB={MakeHash=function(name)
  if name=='GOODYHUT_ADD_POP' then return 1038837136 end
  return -1
end}
'''
BEGIN = f"Puppeteer.BeginRewardCommand(0,1,10,'{NONCE}',1,0,1)\n"
FINISH = f"Puppeteer.FinishRewardCommand('{NONCE}','{ATTRS}',1)\n"
GROW = 'improvement=0; units[1].x=1; cities[1].pop=4; hooks.reward(0,10,1892398955,1038837136)\n'
ROW = 'LEDGER|city.growth|city|c0:30|population|3|4'


@pytest.fixture
def native(tmp_path):
    def run(body, setup=''):
        path = tmp_path / 'reward.lua'
        path.write_text(ENGINE + NATIVE + setup + '\ndofile(' + json.dumps(str(MOD)) + ')\n'
                        + 'Puppeteer.SetPuppet(0,true)\nPuppeteer.AttachCurrentTurn(0,1)\n'
                        + body)
        result = subprocess.run([shutil.which('texlua'), str(path)], capture_output=True,
                                text=True, check=True, timeout=5)
        return result.stdout.splitlines()
    return run


def test_native_exact_population_reward_is_one_causal_command_and_finish_retry(native):
    rows = native(BEGIN + GROW + FINISH + FINISH + 'Puppeteer.DumpLedger()')
    assert rows.count(ROW) == 2  # identical transport replay, not two ledger bookings
    assert rows.count(f'REWARD_FINISH|{NONCE}|1') == 2
    start = rows.index(f'REWARD_FINISH|{NONCE}|1')
    end = rows.index('---END---', start)
    ledger, audit = rp.parse_reward_finish(rows[start:end], NONCE, 1, 0, 1, 'u0:10')
    assert ROW in ledger
    assert audit[0]['city_id'] == 'c0:30' and audit[0]['after'] == 4
    assert audit[1]['observation'] == 'matched_add_population'


@pytest.mark.parametrize('event', [
    '', 'hooks.reward(1,10,1892398955,1038837136)', 'hooks.reward(0,20,1892398955,1038837136)',
    'hooks.reward(0,10,123,1038837136)', 'hooks.reward(0,10,1892398955,17)',
    'hooks.reward(0,10,1892398955,1038837136); hooks.reward(0,10,1892398955,1038837136)',
    'units[1].x=0; hooks.reward(0,10,1892398955,1038837136)',
])
def test_native_unmatched_or_duplicate_events_never_authorize_growth(native, event):
    rows = native(BEGIN + 'units[1].x=1; cities[1].pop=4; ' + event + '\n' + FINISH
                  + "print('UNDECLARED'); Puppeteer.DumpLedger()")
    assert not any(r.startswith('REWARD_CAUSE|') for r in rows)
    assert ROW in rows[rows.index('UNDECLARED'):]


@pytest.mark.parametrize('setup,action', [
    ('improvement=0', GROW),
    ('', GROW + 'improvement=1'),
    ('cities={city(30,0),city(31,2)}', GROW),  # nearest-city tie
    ('cities={city(30,1),city(31,4)}', GROW + 'cities[2].pop=4'),
    ('cities={city(30,1),city(31,4)}', GROW + 'cities[2]=nil'),
    ('', GROW + 'cities[1].pop=5'),
    ('', GROW + "GameInfo.GoodyHutSubTypes[16].ModifierID='other'"),
])
def test_native_ambiguous_target_or_nonexact_delta_stays_uncommanded(native, setup, action):
    # Modifier mismatch must be present when native event occurs.
    if 'ModifierID' in action:
        action = action.split('GameInfo')[1]
        action = 'GameInfo' + action + '\n' + GROW
    rows = native(BEGIN + action + '\n' + FINISH + 'Puppeteer.DumpLedger()', setup)
    assert not any(r.startswith('REWARD_CAUSE|') for r in rows)
    assert any('LEDGER|city.growth|' in r for r in rows)


def test_prior_population_drift_cannot_be_covered_by_later_reward(native):
    rows = native('cities[1].pop=4\n' + BEGIN
                  + 'units[1].x=1; cities[1].pop=5; hooks.reward(0,10,1892398955,1038837136)\n'
                  + FINISH + 'Puppeteer.DumpLedger()')
    assert not any(r.startswith('REWARD_CAUSE|') for r in rows)
    assert 'LEDGER|city.growth|city|c0:30|population|3|5' in rows


def test_valid_reward_preserves_simultaneous_unrelated_drift(native):
    rows = native(BEGIN + GROW
                  + 'Players[0].GetTreasury=function() return '
                  + '{GetGoldBalance=function() return 90 end} end\n'
                  + FINISH + "print('UNDECLARED'); Puppeteer.DumpLedger()")
    assert any(r.startswith('REWARD_CAUSE|') for r in rows)
    assert 'LEDGER|player.gold|player|p0|gold|0|90' in rows[rows.index('UNDECLARED'):]


def test_event_after_cancellation_cannot_authorize_next_move(native):
    second = 'b' * 64
    rows = native(BEGIN + f"Puppeteer.CancelRewardCommand('{NONCE}')\n" + GROW
                  + BEGIN.replace(NONCE, second) + FINISH.replace(NONCE, second)
                  + 'Puppeteer.DumpLedger()')
    assert not any(r.startswith('REWARD_CAUSE|') for r in rows)
    assert ROW in rows


def test_begin_retry_does_not_reset_observed_event(native):
    rows = native(BEGIN + GROW + BEGIN + FINISH)
    assert f'REWARD_BEGIN|{NONCE}|duplicate' in rows
    assert any(r.startswith('REWARD_CAUSE|') for r in rows)


def test_native_capability_requires_event_registration_and_functions(native):
    rows = native('Puppeteer.Handshake(); Puppeteer.FinishRewardCommand=nil; Puppeteer.Handshake()')
    assert [r for r in rows if r.startswith('SUPPORTS_REWARD_RECEIPTS|')] == [
        'SUPPORTS_REWARD_RECEIPTS|true', 'SUPPORTS_REWARD_RECEIPTS|false']
    rows = native('Puppeteer.Handshake()', 'Events.GoodyHutReward=nil')
    assert 'SUPPORTS_REWARD_RECEIPTS|false' in rows


def completion(nonce=NONCE, seq=1):
    return [f'REWARD_FINISH|{nonce}|{seq}',
            f'REWARD_OBSERVATION|{nonce}|1|1892398955|1038837136|matched_add_population',
            f'REWARD_CAUSE|{nonce}|0|1|10|GOODYHUT_SURVIVORS|GOODYHUT_ADD_POP|30|3|4|'
            'IMPROVEMENT_GOODY_HUT', ROW]


@pytest.mark.parametrize('alter', [
    lambda r: r[1:], lambda r: r + [r[0]], lambda r: r[:-1],
    lambda r: [x.replace('|0|1|10|', '|1|1|10|') for x in r],
    lambda r: [x.replace('|30|3|4', '|30|3|5') for x in r],
    lambda r: [x.replace('GOODYHUT_ADD_POP', 'GOODYHUT_GOLD') for x in r],
    lambda r: r + ['LEDGER|city.growth|city|c0:31|population|3|4'],
    lambda r: [x.replace(NONCE, 'b' * 64) for x in r],
])
def test_receipt_parser_refuses_mismatch_or_missing_proof(alter):
    with pytest.raises((RuntimeError, ValueError)):
        rp.parse_reward_finish(alter(completion()), NONCE, 1, 0, 1, 'u0:10')


async def test_adapter_opens_before_restore_and_emits_exact_causal_result():
    adapter = FireTunerAdapter()
    adapter._phase_open, adapter._turn_mirror = 0, 1
    adapter._refresh_digest = AsyncMock()
    seen = []

    async def read(lua):
        seen.append(lua)
        if 'BeginRewardCommand' in lua:
            nonce = lua.split("'")[1]
            return [f'REWARD_BEGIN|{nonce}|accepted']
        if 'RestoreUnit' in lua:
            return ['RESTORE_UNIT|0|10|restored']
        if 'FinishRewardCommand' in lua:
            return completion(lua.split("'")[1])
        raise AssertionError(lua)

    adapter._conn = AsyncMock()
    adapter._conn.execute_read.side_effect = read
    adapter._conn.execute_write.return_value = ['ACT|move_unit|OK|1,0']
    result = await adapter.act(ActionCommand('move_unit', {'unit_id': 'u0:10', 'dest': '1,0'},
                                            0, 'key', 'lease'))
    assert result.status == 'accepted'
    assert 'BeginRewardCommand' in seen[0] and 'RestoreUnit' in seen[1]
    assert 'FinishRewardCommand' in seen[2]
    assert result.mutations[0].entity_id == 'c0:30'
    assert result.result['causal_receipts'][0]['reward_subtype'] == 'GOODYHUT_ADD_POP'
    assert adapter._journal == list(result.mutations)


async def test_adapter_begin_failure_sends_no_move_or_restore():
    adapter = FireTunerAdapter()
    adapter._phase_open, adapter._turn_mirror = 0, 1
    adapter._conn = AsyncMock()
    adapter._conn.execute_read.return_value = []
    with pytest.raises(RuntimeError, match='begin receipt'):
        await adapter.act(ActionCommand('move_unit', {'unit_id': 'u0:10', 'dest': '1,0'},
                                       0, 'key', 'lease'))
    adapter._conn.execute_write.assert_not_called()
    assert not any('RestoreUnit' in c.args[0] for c in adapter._conn.execute_read.call_args_list)


@pytest.mark.parametrize('cap', [None, False])
async def test_preflight_refuses_missing_reward_hook(cap):
    adapter = FireTunerAdapter()
    doc = {'mod_version': '0.3.10', 'supports_freeze': True, 'supports_ledger': True,
           'supports_digest': True, 'supports_command_diff': True, 'supports_guarded_handoff': True}
    if cap is not None:
        doc['supports_reward_receipts'] = cap
    adapter.mod_handshake = AsyncMock(return_value=doc)
    with pytest.raises(RuntimeError, match='reward_receipts'):
        await adapter.require_mod()


def test_builders_preserve_identity_and_coordinate_frame():
    assert "'" + NONCE + "', 30, 13, 1)" in lt.begin_reward_command(
        1, 21, 'u1:327683', NONCE, '24,13', 1)
    with pytest.raises(ValueError):
        lt.begin_reward_command(0, 21, 'u1:327683', NONCE, '24,13', 1)
    with pytest.raises(ValueError):
        lt.cancel_reward_command("');evil()")


def test_missing_consumption_event_quarantines_across_lease_change_and_late_event(native):
    rows = native(BEGIN + "improvement=0; units[1].x=1; cities[1].pop=4\n" + FINISH
                  + "Puppeteer.Release(0,1); currentTurn=2; hooks.start(0)\n"
                  + "hooks.reward(0,10,1892398955,1038837136); improvement=1\n"
                  + BEGIN.replace(NONCE, 'b' * 64).replace('(0,1,10', '(0,2,10')
                  + 'Puppeteer.DumpLedger()')
    assert f'REWARD_OBSERVATION|{NONCE}|0|0|0|missing_consumption_event' in rows
    assert f"REWARD_BEGIN|{'b' * 64}|quarantined_missing_event" in rows
    assert ROW in rows
    assert not any(r.startswith('REWARD_CAUSE|') for r in rows)


def test_begin_quarantine_has_explicit_diagnostic():
    with pytest.raises(RuntimeError, match='consumed village lacked native event'):
        rp.parse_reward_begin([f'REWARD_BEGIN|{NONCE}|quarantined_missing_event'], NONCE)


def test_unsupported_native_reward_is_audited_without_population_authority(native):
    rows = native(BEGIN + 'improvement=0; units[1].x=1; hooks.reward(0,10,3000000000,17)\n'
                  + FINISH)
    start = rows.index(f'REWARD_FINISH|{NONCE}|1')
    end = rows.index('---END---', start)
    _, audit = rp.parse_reward_finish(rows[start:end], NONCE, 1, 0, 1, 'u0:10')
    assert audit == [{'event': 'GoodyHutReward', 'command_nonce': NONCE,
                      'observation': 'unsupported_or_unmatched', 'event_count': 1,
                      'native_reward_type': 3000000000, 'native_reward_subtype': 17}]


async def test_final_move_missing_reward_event_cannot_reach_clean_end_phase():
    adapter = FireTunerAdapter()
    adapter._phase_open, adapter._turn_mirror = 0, 60
    adapter._refresh_digest = AsyncMock()
    adapter.end_phase = AsyncMock()

    async def read(lua):
        if 'BeginRewardCommand' in lua:
            return [f"REWARD_BEGIN|{lua.split(chr(39))[1]}|accepted"]
        if 'RestoreUnit' in lua:
            return ['RESTORE_UNIT|0|10|restored']
        if 'FinishRewardCommand' in lua:
            nonce = lua.split("'")[1]
            # The reward need not touch a watched attribute: missing native
            # evidence itself must prevent a clean final turn.
            return [f'REWARD_FINISH|{nonce}|1',
                    f'REWARD_OBSERVATION|{nonce}|0|0|0|missing_consumption_event',
                    'LEDGER|unit.moved|unit|u0:10|pos|0,0|1,0']
        raise AssertionError(lua)

    adapter._conn = AsyncMock()
    adapter._conn.execute_read.side_effect = read
    adapter._conn.execute_write.return_value = ['ACT|move_unit|OK|1,0']
    with pytest.raises(RuntimeError, match='consumed village lacked native event'):
        await adapter.act(ActionCommand('move_unit', {'unit_id': 'u0:10', 'dest': '1,0'},
                                       0, 'final-move', 'turn-60-lease'))
        await adapter.end_phase(0, 60)
    adapter.end_phase.assert_not_awaited()
    assert adapter._phase_open == 0


async def test_rejected_move_is_refrozen_even_when_reward_cancellation_fails():
    adapter = FireTunerAdapter()
    adapter._phase_open, adapter._turn_mirror = 0, 1
    moves = 0

    async def read(lua):
        nonlocal moves
        if 'BeginRewardCommand' in lua:
            return [f"REWARD_BEGIN|{lua.split(chr(39))[1]}|accepted"]
        if 'RestoreUnit' in lua:
            moves = 2
            return ['RESTORE_UNIT|0|10|restored']
        if 'FreezeUnit' in lua:
            moves = 0
            return ['FROZEN|10']
        if 'CancelRewardCommand' in lua:
            assert moves == 0
            raise TimeoutError('cancel failed')
        raise AssertionError(lua)

    adapter._conn = AsyncMock()
    adapter._conn.execute_read.side_effect = read
    adapter._conn.execute_write.return_value = ['ACT|move_unit|ERR|ILLEGAL_MOVE|blocked']
    with pytest.raises(TimeoutError, match='cancel failed'):
        await adapter.act(ActionCommand('move_unit', {'unit_id': 'u0:10', 'dest': '1,0'},
                                       0, 'rejected', 'lease'))
    assert moves == 0


SUMERIA = """
improvement=2
GameInfo.Improvements[2]={ImprovementType='IMPROVEMENT_BARBARIAN_CAMP'}
civilization='CIVILIZATION_SUMERIA'
PlayerConfigurations={[0]={GetCivilizationTypeName=function() return civilization end}}
function each(rows)
    local i=0
    return function() i=i+1; return rows[i] end
end
civilizationTraits={{CivilizationType='CIVILIZATION_SUMERIA',
                    TraitType='TRAIT_CIVILIZATION_FIRST_CIVILIZATION'}}
traitModifiers={{TraitType='TRAIT_CIVILIZATION_FIRST_CIVILIZATION',
                 ModifierId='TRAIT_BARBARIAN_CAMP_GOODY'}}
GameInfo.CivilizationTraits=function() return each(civilizationTraits) end
GameInfo.TraitModifiers=function() return each(traitModifiers) end
GameInfo.Modifiers.TRAIT_BARBARIAN_CAMP_GOODY={
    ModifierType='MODIFIER_PLAYER_ADJUST_IMPROVEMENT_GOODY_HUT'}
GameInfo.DynamicModifiers={MODIFIER_PLAYER_ADJUST_IMPROVEMENT_GOODY_HUT={
    CollectionType='COLLECTION_OWNER',EffectType='EFFECT_ADJUST_IMPROVEMENT_GOODY_HUT'}}
modifierArguments={
    {ModifierId='GOODY_SURVIVORS_ADD_POPULATION',Name='Amount',Value='1'},
    {ModifierId='TRAIT_BARBARIAN_CAMP_GOODY',Name='ImprovementType',
     Value='IMPROVEMENT_BARBARIAN_CAMP'},
    {ModifierId='TRAIT_BARBARIAN_CAMP_GOODY',Name='GoodyHutImprovementType',
     Value='IMPROVEMENT_GOODY_HUT'}}
GameInfo.ModifierArguments=function() return each(modifierArguments) end
"""


def test_sumerian_camp_exact_reward_uses_native_event_and_reports_site(native):
    rows = native(BEGIN + GROW + FINISH + 'Puppeteer.DumpLedger()', SUMERIA)
    start = rows.index(f'REWARD_FINISH|{NONCE}|1')
    end = rows.index('---END---', start)
    ledger, audit = rp.parse_reward_finish(rows[start:end], NONCE, 1, 0, 1, 'u0:10')
    assert ledger.count(ROW) == 1
    assert audit[0]['site_improvement'] == 'IMPROVEMENT_BARBARIAN_CAMP'
    assert audit[0]['before'] == 3 and audit[0]['after'] == 4
    assert rows.count(ROW) == 1  # not also booked as undeclared


@pytest.mark.parametrize('alter', [
    "civilization='CIVILIZATION_EGYPT'", 'PlayerConfigurations=nil',
    'civilizationTraits={}', 'traitModifiers={}',
    "traitModifiers[1].ModifierId='OTHER'",
    "GameInfo.Modifiers.TRAIT_BARBARIAN_CAMP_GOODY.ModifierType='OTHER'",
    'GameInfo.Modifiers.TRAIT_BARBARIAN_CAMP_GOODY=nil',
    'GameInfo.DynamicModifiers={}',
    "GameInfo.DynamicModifiers.MODIFIER_PLAYER_ADJUST_IMPROVEMENT_GOODY_HUT.EffectType='OTHER'",
    "modifierArguments[2].Value='OTHER'", "modifierArguments[3].Value='OTHER'",
    'modifierArguments[4]=modifierArguments[2]', 'modifierArguments[3]=nil',
])
def test_other_civilization_or_missing_changed_trait_chain_cannot_authorize_camp(native, alter):
    rows = native(BEGIN + GROW + FINISH + "print('UNDECLARED'); Puppeteer.DumpLedger()",
                  SUMERIA + alter + '\n')
    assert not any(row.startswith('REWARD_CAUSE|') for row in rows)
    assert ROW in rows[rows.index('UNDECLARED'):]


def test_ordinary_camp_disappearance_does_not_expect_reward_or_quarantine(native):
    second = 'b' * 64
    rows = native(BEGIN + 'improvement=0; units[1].x=1\n' + FINISH
                  + BEGIN.replace(NONCE, second).replace(',0,1)', ',0,2)'),
                  SUMERIA + "civilization='CIVILIZATION_EGYPT'\n")
    assert f'REWARD_OBSERVATION|{NONCE}|0|0|0|no_matching_event' in rows
    assert f'REWARD_BEGIN|{second}|accepted' in rows


@pytest.mark.parametrize('setup,site', [(SUMERIA, 2), ('', 1)])
def test_unchanged_eligible_site_cannot_authorize_population(native, setup, site):
    rows = native(BEGIN + GROW + f'improvement={site}\n' + FINISH
                  + 'Puppeteer.DumpLedger()', setup)
    assert not any(row.startswith('REWARD_CAUSE|') for row in rows)
    assert ROW in rows


def test_sumerian_camp_without_native_event_still_aborts(native):
    rows = native(BEGIN + 'improvement=0; units[1].x=1\n' + FINISH, SUMERIA)
    start = rows.index(f'REWARD_FINISH|{NONCE}|1')
    end = rows.index('---END---', start)
    with pytest.raises(RuntimeError, match='consumed village lacked native event'):
        rp.parse_reward_finish(rows[start:end], NONCE, 1, 0, 1, 'u0:10')


@pytest.mark.parametrize('setup,event', [
    ('', 'hooks.reward(0,10,1892398955,16)'),  # row ordinal is not a native hash
    ('', 'hooks.reward(0,10,1892398955,1038837137)'),
    ('DB=nil', 'hooks.reward(0,10,1892398955,1038837136)'),
    ('DB.MakeHash=nil', 'hooks.reward(0,10,1892398955,1038837136)'),
    ('DB.MakeHash=function() return 7 end', 'hooks.reward(0,10,1892398955,1038837136)'),
    ('GameInfo.GoodyHutSubTypes.GOODYHUT_ADD_POP=nil',
     'hooks.reward(0,10,1892398955,1038837136)'),
])
def test_native_subtype_hash_is_required_and_cannot_be_replaced_by_row_index(native, setup, event):
    rows = native(BEGIN + 'improvement=0; units[1].x=1; cities[1].pop=4; '
                  + event + '\n' + FINISH + "print('UNDECLARED'); Puppeteer.DumpLedger()", setup)
    assert not any(row.startswith('REWARD_CAUSE|') for row in rows)
    assert ROW in rows[rows.index('UNDECLARED'):]


def test_native_live_hash_resolves_named_row_without_numeric_hash_lookup(native):
    rows = native("assert(GameInfo.GoodyHutSubTypes[1038837136]==nil)\n"
                  + BEGIN + GROW + FINISH)
    assert any(row.startswith('REWARD_CAUSE|') for row in rows)
    assert f'REWARD_OBSERVATION|{NONCE}|1|1892398955|1038837136|matched_add_population' in rows


@pytest.mark.parametrize('setup', ['DB=nil', 'DB.MakeHash=nil'])
def test_reward_capability_requires_engine_hash_binding(native, setup):
    rows = native('Puppeteer.Handshake()', setup)
    assert 'SUPPORTS_REWARD_RECEIPTS|false' in rows
