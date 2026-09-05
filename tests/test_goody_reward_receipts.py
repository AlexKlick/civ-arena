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
BEGIN = f"Puppeteer.BeginRewardCommand(0,1,10,'{NONCE}',1,0,1)\n"
FINISH = f"Puppeteer.FinishRewardCommand('{NONCE}','{ATTRS}',1)\n"
GROW = 'improvement=0; units[1].x=1; cities[1].pop=4; hooks.reward(0,10,1892398955,16)\n'
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
    '', 'hooks.reward(1,10,1892398955,16)', 'hooks.reward(0,20,1892398955,16)',
    'hooks.reward(0,10,123,16)', 'hooks.reward(0,10,1892398955,17)',
    'hooks.reward(0,10,1892398955,16); hooks.reward(0,10,1892398955,16)',
    'units[1].x=0; hooks.reward(0,10,1892398955,16)',
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
                  + 'units[1].x=1; cities[1].pop=5; hooks.reward(0,10,1892398955,16)\n'
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
            f'REWARD_OBSERVATION|{nonce}|1|1892398955|16|matched_add_population',
            f'REWARD_CAUSE|{nonce}|0|1|10|GOODYHUT_SURVIVORS|GOODYHUT_ADD_POP|30|3|4', ROW]


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
    doc = {'mod_version': '0.3.8', 'supports_freeze': True, 'supports_ledger': True,
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
                  + "hooks.reward(0,10,1892398955,16); improvement=1\n"
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
