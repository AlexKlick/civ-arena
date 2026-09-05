"""Checked restore completion with no live engine or provider traffic."""
from unittest.mock import AsyncMock

import pytest

from civ_arena.game.adapter import ActionCommand
from civ_arena.game.civ6 import lua_translator as lt
from civ_arena.game.civ6 import response_parser as rp
from civ_arena.game.civ6.firetuner import FireTunerAdapter
from civ_arena.game.civ6.vendor.connection import GameConnection, LuaError
from civ_arena.game.civ6.vendor.tuner_client import Message
from test_mod_initial_attach import run_mod  # noqa: F401


@pytest.mark.parametrize('status', ['restored', 'already_restored', 'unknown_entity'])
def test_valid_bound_receipt(status):
    assert rp.parse_restore_receipt(
        [f'RESTORE_UNIT|1|9007199254740991|{status}\n---END---'],
        'u1:9007199254740991') == status


@pytest.mark.parametrize('rows', [[], ['---END---'], ['RESTORE_UNIT|1|7|nil'],
    ['RESTORE_UNIT|1|7|wrong_lease'],
    ['RESTORE_UNIT|0|7|restored'], ['RESTORE_UNIT|1|65543|restored'],
    ['RESTORE_UNIT|1|7|restored'] * 2, ['RESTORE_UNIT|01|7|restored']])
def test_missing_failed_misbound_or_duplicate_receipt_refused(rows):
    with pytest.raises(RuntimeError, match='restore completion'):
        rp.parse_restore_receipt(rows, 'u1:7')


@pytest.mark.parametrize('value', ['u7', 'u1:07', 'u1:9007199254740992', 'u1:7;evil()'])
def test_restore_uses_exact_live_decoder(value):
    with pytest.raises(ValueError):
        lt.restore_unit(value)
    with pytest.raises(ValueError):
        rp.parse_restore_receipt([], value)


def test_real_mod_receipt_guards_and_repeat_does_not_refill(run_mod):  # noqa: F811
    rows = run_mod('''
units[1].id=131073
Puppeteer.SetPuppet(1,true)
localPlayer=1
Puppeteer.AttachCurrentTurn(1,1)
''' + lt.restore_unit('u0:131073') + '\n' + lt.restore_unit('u1:999') + '\n'
        + lt.restore_unit('u1:131073') + '''
assert(restores==1)
units[1].moves=0
units[1].attacks=0
''' + lt.restore_unit('u1:131073') + '''
assert(restores==1 and units[1].moves==0 and units[1].attacks==0)
''')
    receipts = [r for r in rows if r.startswith('RESTORE_UNIT|')]
    assert receipts == ['RESTORE_UNIT|0|131073|wrong_lease',
                        'RESTORE_UNIT|1|999|unknown_entity',
                        'RESTORE_UNIT|1|131073|restored',
                        'RESTORE_UNIT|1|131073|already_restored']


@pytest.mark.parametrize('failure', [
    [], TimeoutError('restore timeout'), LuaError('restore error')])
async def test_restore_failure_prevents_action_and_attempts_refreeze(failure):
    adapter = FireTunerAdapter()
    adapter._phase_open = 1
    adapter._turn_mirror = 1
    async def read(lua):
        if 'BeginRewardCommand' in lua:
            return [f"REWARD_BEGIN|{lua.split(chr(39))[1]}|accepted"]
        if 'CancelRewardCommand' in lua:
            return []
        if 'FreezeUnit' in lua:
            return ['FROZEN|131073']
        if isinstance(failure, Exception):
            raise failure
        return failure
    adapter._conn = AsyncMock()
    adapter._conn.execute_read.side_effect = read
    command = ActionCommand(tool='move_unit', args={'unit_id': 'u1:131073', 'dest': '2,3'},
                            player_id=1, idempotency_key='fixture', lease_id='fixture')
    with pytest.raises((RuntimeError, TimeoutError, LuaError)):
        await adapter.act(command)
    adapter._conn.execute_write.assert_not_called()
    assert 'FreezeUnit' in adapter._conn.execute_read.call_args.args[0]


async def test_sentinel_finishes_collection_without_waiting_for_empty_response(monkeypatch):
    from civ_arena.game.civ6.vendor import tuner_client
    connection = GameConnection()
    connection._reader = object()
    connection._writer = object()
    monkeypatch.setattr(tuner_client, 'send_message', AsyncMock())
    monkeypatch.setattr(tuner_client, 'drain_messages', AsyncMock(return_value=[]))
    receive = AsyncMock(side_effect=[
        Message(3, 'O\x00GameCore_Tuner: RESTORE_UNIT|1|7|restored'),
        Message(3, 'O\x00GameCore_Tuner: ---END---')])
    monkeypatch.setattr(tuner_client, 'recv_message_timeout', receive)
    rows = await connection._locked_execute(0, lt.restore_unit('u1:7'), 5)
    assert rp.parse_restore_receipt(rows, 'u1:7') == 'restored'
    assert receive.await_count == 2  # no third receive reaching the two-second idle timeout


async def test_previous_mod_is_rejected_at_preflight():
    adapter = FireTunerAdapter()
    adapter.mod_handshake = AsyncMock(return_value={
        'mod_version': '0.3.4', 'supports_freeze': True, 'supports_ledger': True,
        'supports_digest': True, 'supports_command_diff': True})
    with pytest.raises(RuntimeError, match='0.3.9'):
        await adapter.require_mod()
