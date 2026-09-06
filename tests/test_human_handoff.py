"""Control transfer must not give the outgoing native AI an active slice."""
import asyncio
import shutil
import subprocess

import pytest

from civ_arena.game.civ6 import human_handoff as hh
from civ_arena.game.civ6.fake_tuner_server import FakeMod

TOKEN = 'a' * 32
ENGINE = '''
turn,lp,active,lease=1,0,0,0
humans,cfg={[0]=true,[1]=true},{[0]=true,[1]=true}
purchases,switches,ends,reflags=0,0,0,0
Players,PlayerConfigurations={},{}
for pid=0,1 do
 Players[pid]={IsHuman=function() return humans[pid] end,
  IsTurnActive=function() return active==pid end}
 PlayerConfigurations[pid]={IsHuman=function() return cfg[pid] end,
  SetSlotStatus=function(_,value) cfg[pid]=true;humans[pid]=true;reflags=reflags+1 end}
end
Game={GetCurrentGameTurn=function() return turn end,GetLocalPlayer=function() return lp end}
Puppeteer={Status=function()
 return 'PUPPET_ACTIVE|true\\nLEASE_PLAYER|'..lease..'\\nLEASE_TURN|'..turn..'\\n' end}
PlayerManager={SetLocalPlayerAndObserver=function(next)
 if active==lp then purchases=purchases+1 end
 humans[lp]=false;cfg[lp]=false;lp=next;switches=switches+1
end}
Network={BroadcastPlayerInfo=function() end};SlotStatus={SS_TAKEN=3}
ActionTypes={ACTION_ENDTURN=1}
UI={RequestAction=function()
 ends=ends+1
 if active==1 then turn=turn+1 end
 active=1-active;lease=active
end}
'''


def lua(tmp_path, body):
    executable = shutil.which('texlua')
    if executable is None:
        pytest.skip('texlua unavailable')
    path = tmp_path / 'human_handoff.lua'
    path.write_text(ENGINE + body)
    return subprocess.run([executable, str(path)], capture_output=True,
                          text=True, timeout=5, check=True).stdout


def test_executable_two_rounds_switches_only_inactive_and_reflags(tmp_path):
    body = ''
    for turn in (1, 2):
        for player in (0, 1):
            for op in ('activate', 'reflag', 'verify', 'end'):
                body += hh.script(op, player, turn, (0, 1), TOKEN)
    body += "assert(purchases==0 and ends==4 and switches==3 and reflags==3)"
    lua(tmp_path, body)


@pytest.mark.parametrize('alter,operation', [
    ('turn=2', 'end'), ('lp=1', 'end'), ('cfg[1]=false', 'end'),
    ('humans[1]=false', 'verify'), ('lease=1', 'activate'),
    ('active=1', 'reflag'),
])
def test_executable_refusals_precede_actions(tmp_path, alter, operation):
    body = alter + '\nlocal ok=pcall(function()\n'
    body += hh.script(operation, 0, 1, (0, 1), TOKEN)
    body += '\nend)\nassert(not ok and ends==0 and switches==0 and reflags==0)'
    lua(tmp_path, body)


def test_switch_demotes_even_with_no_movement_but_safe_path_does_not_buy(tmp_path):
    # The failure is an economy action independent of movement availability.
    lua(tmp_path, 'PlayerManager.SetLocalPlayerAndObserver(1); assert(purchases==1)')
    body = hh.script('end', 0, 1, (0, 1), TOKEN)
    body += hh.script('activate', 1, 1, (0, 1), TOKEN)
    body += "assert(not humans[0] and purchases==0)"
    body += hh.script('reflag', 1, 1, (0, 1), TOKEN)
    body += hh.script('verify', 1, 1, (0, 1), TOKEN)
    body += 'assert(humans[0] and purchases==0)'
    lua(tmp_path, body)


@pytest.mark.parametrize('player,turn,seats', [
    (True, 1, (0, 1)), (0, True, (0, 1)), (0, 0, (0, 1)),
    (0, 2**53, (0, 1)), (0, 1, (0, 0)), (0, 1, (0, 2)),
    (0, 1, (0, True)), (2, 1, (0, 1)), (0, 1, [0, 1]),
])
def test_identity_rejection(player, turn, seats):
    with pytest.raises(ValueError):
        hh.script('end', player, turn, seats, TOKEN)


class Connection:
    def __init__(self):
        self._reader, self._writer = object(), object()
        self._lock = asyncio.Lock()
        self.is_connected = True
        self.lua_states = {2: 'GameCore_Tuner', 94: 'InGame'}
        self.mod = FakeMod(hotseat=[0, 1])
        self.mod.puppets = {0: True, 1: True}
        self.mod.lease = {'player': 0, 'turn': 1}
        self.mod.turn_active = True
        self.calls = []
        self.result = None

    async def _locked_execute(self, state, code, timeout):
        self.calls.append((state, code))
        if isinstance(self.result, BaseException):
            raise self.result
        if self.result is not None:
            return self.result
        return self.mod.respond(code)


async def test_three_rounds_use_local_end_before_every_display_switch():
    conn = Connection()
    for turn in (1, 2, 3):
        for player in (0, 1):
            assert conn.mod.lease == {'player': player, 'turn': turn}
            await hh.activate(conn, player, turn, (0, 1))
            assert conn.mod.local_player == player and all(conn.mod.humans.values())
            await hh.end_current(conn, player, turn, (0, 1))
            assert conn.mod.local_player == player
    assert conn.mod.lease == {'player': 0, 'turn': 4}
    assert len(conn.calls) == 30


@pytest.mark.parametrize('result', [[], ['wrong'], ['---END---'],
                                        ConnectionError('lost'), OSError('lost')])
async def test_ambiguous_results_never_replay_or_send_later_operations(result):
    conn = Connection()
    conn.result = result
    with pytest.raises((RuntimeError, ConnectionError, OSError)):
        await hh.end_current(conn, 0, 1, (0, 1))
    assert len(conn.calls) == 1 and conn.mod.local_player == 0


async def test_lost_end_receipt_does_not_repeat_end_or_switch():
    conn = Connection()
    original = conn._locked_execute
    async def lost(state, code, timeout):
        rows = await original(state, code, timeout)
        if 'human_handoff=end,' in code:
            raise ConnectionError('accepted command response lost')
        return rows
    conn._locked_execute = lost
    with pytest.raises(ConnectionError):
        await hh.end_current(conn, 0, 1, (0, 1))
    assert len(conn.calls) == 2
    assert conn.mod.lease == {'player': 1, 'turn': 1}
    assert conn.mod.local_player == 0


async def test_cancel_while_waiting_sends_nothing():
    conn = Connection()
    await conn._lock.acquire()
    task = asyncio.create_task(hh.end_current(conn, 0, 1, (0, 1)))
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    conn._lock.release()
    assert conn.calls == []


async def test_connection_changed_during_lock_wait_refuses_dispatch():
    conn = Connection()
    await conn._lock.acquire()
    task = asyncio.create_task(hh.activate(conn, 0, 1, (0, 1)))
    await asyncio.sleep(0)
    conn._reader = object()
    conn._lock.release()
    with pytest.raises(RuntimeError, match='connection changed'):
        await task
    assert conn.calls == []


@pytest.mark.parametrize('states', [{2: 'GameCore_Tuner', 3: 'GameCore_Tuner'},
                                  {94: 'InGame'}])
async def test_missing_or_ambiguous_vm_refuses_dispatch(states):
    conn = Connection()
    conn.lua_states = states
    with pytest.raises(RuntimeError, match='VM unavailable or ambiguous'):
        await hh.activate(conn, 0, 1, (0, 1))
    assert conn.calls == []
