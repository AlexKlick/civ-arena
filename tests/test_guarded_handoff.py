"""Atomic freeze-before-transfer against real Lua and the audited adapter."""
from unittest.mock import AsyncMock

import pytest

from civ_arena.game.civ6 import lua_translator as lt
from civ_arena.game.civ6 import response_parser as rp
from civ_arena.game.civ6.fake_tuner_server import FakeMod
from civ_arena.game.civ6.firetuner import FireTunerAdapter
from test_mod_initial_attach import run_mod  # noqa: F401

SETUP = '''
Players[0].GetID=function() return 0 end
PlayerManager={GetAliveMajors=function()
  return {{GetID=function() return 0 end},{GetID=function() return 1 end}}
end}
Puppeteer.SetPuppet(0,true)
Puppeteer.SetPuppet(1,true)
Puppeteer.AttachCurrentTurn(0,1)
Puppeteer.RestoreUnit(10,0)
switches=0
PlayerManager.SetLocalPlayerAndObserver=function(nextPlayer)
  switches=switches+1
  assert(units[1].moves==0 and units[2].moves==0, 'native AI can act')
  localPlayer=nextPlayer
end
'''


def test_real_lua_freezes_before_switch_and_duplicate_never_refills(run_mod):  # noqa: F811
    rows = run_mod(SETUP + '''
Puppeteer.GuardedHandoff(0,1,1)
assert(localPlayer==1 and switches==1)
Puppeteer.GuardedHandoff(0,1,1)
assert(switches==1 and units[1].moves==0)
Puppeteer.Release(0,1)
print(Puppeteer.DumpLedger())
''')
    assert 'HANDOFF|0|1|1|accepted|frozen_then_switched' in rows
    assert 'HANDOFF|0|1|1|duplicate|already_sent' in rows


@pytest.mark.parametrize('alter,reason', [
    ('Puppeteer.Release(0,1)', 'wrong_lease'),
    ('localPlayer=1', 'wrong_local'),
    ('currentTurn=2', 'wrong_turn'),
    ('Puppeteer.SetPuppet(1,false)', 'invalid_next'),
    ('PlayerManager.GetAliveMajors=function() return {} end', 'next_not_alive_major'),
    ('active=false', 'current_not_active_local_human'),
])
def test_real_lua_precondition_failure_never_freezes_or_switches(run_mod, alter, reason):  # noqa: F811
    rows = run_mod(SETUP + alter + '''
freezes=0
Puppeteer.GuardedHandoff(0,1,1)
assert(freezes==0 and switches==0 and units[1].moves==2)
''')
    assert f'HANDOFF|0|1|1|rejected|{reason}' in rows


@pytest.mark.parametrize('alter,reason', [
    ('UnitManager.FinishMoves=function() end', 'freeze_incomplete'),
    ("UnitManager.FinishMoves=function() error('injected') end", 'engine_error'),
    ('UnitManager.FinishMoves=function(u) u.moves=0; localPlayer=1 end',
     'changed_during_freeze'),
    ('PlayerManager.SetLocalPlayerAndObserver=function() end', 'switch_not_observed'),
])
def test_real_lua_partial_failure_is_explicit(run_mod, alter, reason):  # noqa: F811
    rows = run_mod(SETUP + alter + '\nPuppeteer.GuardedHandoff(0,1,1)')
    assert f'HANDOFF|0|1|1|failed|{reason}' in rows


def test_real_lua_next_engagement_survives_and_prior_drift_is_retained(run_mod):  # noqa: F811
    rows = run_mod(SETUP + '''
units[1].GetDamage=function() return 7 end
PlayerManager.SetLocalPlayerAndObserver=function(nextPlayer)
  assert(units[1].moves==0 and units[2].moves==0)
  localPlayer=nextPlayer
  hooks.deactivated(0)
  hooks.start(1)
end
Puppeteer.GuardedHandoff(0,1,1)
Puppeteer.Release(0,1)
print(Puppeteer.Status())
print(Puppeteer.DumpLedger())
''')
    assert 'HANDOFF|0|1|1|accepted|frozen_then_switched' in rows
    assert 'LEASE_PLAYER|1' in rows
    assert any('unit.damage|unit|u0:10|damage|0|7' in row for row in rows)


@pytest.mark.parametrize('rows', [[], ['---END---'],
    ['HANDOFF|0|1|1|failed|freeze_incomplete'],
    ['HANDOFF|1|1|0|accepted|frozen_then_switched'],
    ['HANDOFF|0|2|1|accepted|frozen_then_switched'],
    ['HANDOFF|0|1|1|accepted|frozen_then_switched'] * 2])
def test_decoder_refuses_missing_failure_wrong_identity_or_duplicate_rows(rows):
    with pytest.raises(RuntimeError, match='handoff completion'):
        rp.parse_handoff_receipt(rows, 0, 1, 1)


@pytest.mark.parametrize('args', [(True, 1, 1), (0, 0, 1), (0, 1, 0),
                                   ('0;evil()', 1, 1), (0, 1, 2**53)])
def test_builder_rejects_invalid_identity(args):
    with pytest.raises(ValueError):
        lt.guarded_handoff(*args)


@pytest.mark.parametrize('failure', [[], TimeoutError('timeout'), RuntimeError('Lua failed')])
async def test_adapter_failure_never_releases_or_sends_separate_switch(failure):
    adapter = FireTunerAdapter()
    adapter._pre_end_switch = 1
    adapter._phase_open, adapter._turn_mirror = 0, 1
    adapter._refresh_digest = AsyncMock()
    adapter._digest_text = 'p0|0|-1'
    adapter._conn = AsyncMock()
    if isinstance(failure, Exception):
        adapter._conn.execute_read.side_effect = failure
    else:
        adapter._conn.execute_read.return_value = failure
    with pytest.raises((RuntimeError, TimeoutError)):
        await adapter.end_phase(0, 1)
    assert adapter._phase_open == 0
    assert adapter._conn.execute_read.await_count == 1
    assert adapter._conn.execute_read.call_args.args == (lt.guarded_handoff(0, 1, 1),)
    adapter._conn.execute_write.assert_not_called()


def test_fake_native_settling_hazard_prevented_without_hiding_other_drift():
    class NativeSwitchMod(FakeMod):
        def _switch_local_player(self, player):
            old = self.local_player
            for uid, unit in list(self.units.items()):
                if unit['owner'] == old and unit['type'] == 'SETTLER' and unit['moves'] > 0:
                    del self.units[uid]
                    self.cities[999] = {'owner': old, 'pop': 1}
            super()._switch_local_player(player)

    def ready():
        mod = NativeSwitchMod(hotseat=[0, 1])
        mod.puppets = {0: True, 1: True}
        mod.lease = {'player': 0, 'turn': 1}
        mod.turn_active = True
        uid = next(uid for uid, u in mod.units.items() if u['owner'] == 0)
        mod.units[uid]['type'] = 'SETTLER'
        mod.units[uid]['moves'] = 2
        mod.mark = mod._snapshot(0)
        return mod, uid
    old, old_uid = ready()
    old.respond(lt.switch_local_player(1))
    assert old_uid not in old.units and 999 in old.cities
    mod, uid = ready()
    mod.units[uid]['damage'] = 7
    rows = mod.respond(lt.guarded_handoff(0, 1, 1))
    assert rp.parse_handoff_receipt(rows, 0, 1, 1) == 'accepted'
    assert uid in mod.units and 999 not in mod.cities
    assert mod.lease == {'player': 1, 'turn': 1}
    assert any('|damage|0|7' in row for row in mod.ledger_rows)
    assert any('|moves|2|0' in row for row in mod.ledger_rows)


async def test_adapter_enforces_bound_when_handoff_never_returns():
    import asyncio
    adapter = FireTunerAdapter()
    adapter._pre_end_switch = 1
    adapter._phase_open, adapter._turn_mirror = 0, 1
    adapter._refresh_digest = AsyncMock()
    adapter._digest_text = 'p0|0|-1'
    adapter._conn = AsyncMock()
    cancelled = False

    async def stalled(_lua):
        nonlocal cancelled
        try:
            await asyncio.Event().wait()
        finally:
            cancelled = True
    adapter._conn.execute_read.side_effect = stalled
    with pytest.raises(TimeoutError):
        await adapter.end_phase(0, 1)
    assert cancelled and adapter._phase_open == 0
    assert adapter._conn.execute_read.await_count == 1


@pytest.mark.parametrize('capability', [None, False])
async def test_preflight_refuses_absent_guarded_handoff(capability):
    adapter = FireTunerAdapter()
    doc = {'mod_version': '0.3.9', 'supports_freeze': True,
           'supports_ledger': True, 'supports_digest': True, 'supports_command_diff': True}
    if capability is not None:
        doc['supports_guarded_handoff'] = capability
    adapter.mod_handshake = AsyncMock(return_value=doc)
    with pytest.raises(RuntimeError, match='guarded_handoff capability'):
        await adapter.require_mod()


def test_real_mod_handshake_is_function_backed(run_mod):  # noqa: F811
    rows = run_mod("Puppeteer.Handshake()\nPuppeteer.GuardedHandoff=nil\nPuppeteer.Handshake()")
    caps = [row for row in rows if row.startswith('SUPPORTS_GUARDED_HANDOFF|')]
    assert caps == ['SUPPORTS_GUARDED_HANDOFF|true', 'SUPPORTS_GUARDED_HANDOFF|false']
    assert rp.parse_handshake(['MOD_PRESENT|true', 'SUPPORTS_GUARDED_HANDOFF|true'])[
        'supports_guarded_handoff'] is True
