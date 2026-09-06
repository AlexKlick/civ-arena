"""Four-seat repository proof; no provider, desktop, or live tuner access."""
import copy
import json
import shutil
import subprocess
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from civ_arena import dashboard
from civ_arena.config import load_config
from civ_arena.game.civ6 import live_driver as ld
from civ_arena.game.civ6.fake_tuner_server import FakeMod, FakeTunerServer
from civ_arena.game.civ6.firetuner import FireTunerAdapter
from civ_arena.game.civ6.hotseat_roster import seat_order
from civ_arena.game.civ6.validate_run import validate
from civ_arena.replay import load_calls, replay_run
from test_hotseat_roster import LUA_FIXTURE, ROOT, launch, row, zero
from test_mod_initial_attach import run_mod  # noqa: F401

CONFIG = ROOT / 'configs/live-hotseat-strategic-minimax4-100.yaml'


@pytest.mark.parametrize('count', [2, 3, 4])
def test_only_all_configured_released_seats_complete_each_of_100_rounds(count):
    ledger = ld.CompletedTurns(reversed(range(count)))
    for turn in range(1, 101):
        for pid in range(count):
            assert ledger.rounds == turn - 1
            ledger.append({'turn': turn, 'player': pid}, SimpleNamespace(released=True))
        assert ledger.rounds == turn
    assert len(ledger.rows) == 100 * count
    assert ledger.expected() == (101, 0)


@pytest.mark.parametrize('seats', [[], [0], [0, 1, 2, 3, 4], [0, 1, 1],
                                    [False, 1], [-1, 1], [0, 64], [0, '1']])
def test_invalid_explicit_rosters_refused(seats):
    with pytest.raises(ValueError):
        ld.CompletedTurns(seats)


@pytest.mark.parametrize('pairs', [[(1, 1)], [(1, 0), (1, 2)], [(1, 0), (1, 0)],
    [(1, 0), (1, 1), (2, 2)], [(1, 0), (1, 1), (1, 2), (1, 3), (1, 0)]])
def test_four_seat_missing_duplicate_wrong_turn_cannot_count(pairs):
    ledger = ld.CompletedTurns(range(4))
    with pytest.raises(RuntimeError, match='out-of-order'):
        for turn, pid in pairs:
            ledger.append({'turn': turn, 'player': pid}, SimpleNamespace(released=True))
    assert ledger.rounds <= 1


def test_four_model_config_retains_provider_caps_and_allowance():
    spec = load_config(CONFIG)
    base = load_config(ROOT / 'configs/live-hotseat-strategic-minimax2-060.yaml')
    assert seat_order(a.player_id for a in spec.agents) == list(range(4))
    assert spec.max_turns == 100
    assert spec.declare_own_endpath_drift is base.declare_own_endpath_drift is True
    assert all(a.llm == base.agents[0].llm and a.decision_mode == 'strategic_autopilot'
               for a in spec.agents)
    assert zero.configured_hotseat_seats(SimpleNamespace(config=str(CONFIG))) == list(range(4))


async def test_noncontiguous_config_refuses_before_desktop_input(tmp_path, monkeypatch):
    spec = load_config(CONFIG)
    spec = replace(spec, agents=[replace(a, player_id=a.player_id + 1) for a in spec.agents])
    monkeypatch.setattr(zero, 'load_config', lambda _: spec)
    display = AsyncMock(side_effect=AssertionError('desktop must not be touched'))
    monkeypatch.setattr(zero, 'require_active_display', display)
    with pytest.raises(ValueError, match='contiguous'):
        await zero.run_arch1_session(SimpleNamespace(config=str(CONFIG), artifacts=tmp_path))
    display.assert_not_awaited()


@pytest.mark.parametrize('variant', ['valid', 'missing', 'extra', 'demoted', 'duplicate', 'dead'])
def test_four_seat_complete_census_matches_exact_human_roster(variant):
    rows = [row(pid) for pid in range(4)]
    if variant == 'missing':
        rows.pop()
    elif variant == 'extra':
        rows.append(row(4))
    elif variant == 'demoted':
        rows[3] = row(3, human='false')
    elif variant == 'duplicate':
        rows.append(row(3))
    elif variant == 'dead':
        rows[3] = row(3, alive='false')
    rows.append(row(10, human='false', major='false'))
    output = '\n'.join([*rows, f'CENSUS_END|{len(rows)}'])
    if variant == 'valid':
        assert zero.verify_major_census(output, range(4))['alive_major_ids'] == list(range(4))
    else:
        with pytest.raises(RuntimeError):
            zero.verify_major_census(output, range(4))


@pytest.mark.parametrize('failed_seat', [None, 2, 3])
async def test_all_nonlocal_seats_reflag_sequentially_and_stop_on_failure(monkeypatch, failed_seat):
    called = []
    busy = False
    async def phase(args):
        nonlocal busy
        assert not busy
        busy = True
        pid = int(args[args.index('--reflag-seat') + 1])
        called.append(pid)
        output = f'REFLAG_SLOT|{pid}|3\nREFLAG_CFGHUMAN|{pid}|true'
        busy = False
        return SimpleNamespace(returncode=12 if pid == failed_seat else 0, stdout=output)
    monkeypatch.setattr(zero, 'phase', phase)
    assert await zero.reflag_configured_seats(range(4)) is (failed_seat is None)
    assert called == (list(range(1, failed_seat + 1)) if failed_seat else [1, 2, 3])


@pytest.mark.parametrize('variant', ['stable', 'extra_reopened', 'demoted', 'missing',
                                     'unready_refresh', 'unready_before_start'])
def test_four_seat_real_lua_host_and_final_ready_guard(variant, tmp_path):
    executable = shutil.which('texlua')
    if executable is None:
        pytest.skip('texlua unavailable')
    fixture = LUA_FIXTURE.replace('{0, 1, 2, 3, 10, 63}', '{0, 1, 2, 3, 4, 10, 63}')
    fixture = fixture.replace('pid < 4', 'pid < 5')
    fixture += '''
Network.HostGame=function()
  for pid=0,3 do assert(PlayerConfigurations[pid].slot==3) end
  assert(PlayerConfigurations[4].slot==0)
  PlayerConfigurations[4].slot=1
  Network.IsSessionActive=function() return true end
end
Network.IsGameHost=function() return true end
refreshes=0
function RealizeGameSetup()
  refreshes=refreshes+1
  if refreshes==1 then PlayerConfigurations[4].slot=1 end
end
launches=0
Network.LaunchGame=function()
  for pid=0,3 do
    assert(PlayerConfigurations[pid].slot==3 and PlayerConfigurations[pid].ready)
    assert(PlayerConfigurations[pid].password=='')
  end
  assert(PlayerConfigurations[4].slot==0)
  assert(PlayerConfigurations[10].slot==1 and PlayerConfigurations[63].slot==1)
  launches=launches+1
end
Controls={ReadyButton={},ReadyCheck={}}
Mouse={eLClick=1}
for _,control in pairs(Controls) do
  function control:RegisterCallback(event,callback) self.callback=callback end
end
'''
    if variant == 'unready_before_start':
        fixture += '''
local originalHost=Network.HostGame
Network.HostGame=function() originalHost(); PlayerConfigurations[3].ready=false end
'''
    succeeds = variant in ('stable', 'unready_before_start')
    source = (fixture + launch.config_hotseat_lua(4, empty_passwords=True)
              + launch.HOST_HOTSEAT_LUA + launch.UI_START_GUARD_LUA)
    if variant == 'extra_reopened':
        source += '\nRealizeGameSetup=function() PlayerConfigurations[4].slot=1 end\n'
    elif variant == 'demoted':
        source += '\nPlayerConfigurations[3].slot=1\n'
    elif variant == 'missing':
        source += '\nPlayerConfigurations[3]=nil\n'
    elif variant == 'unready_refresh':
        source += '\nRealizeGameSetup=function() PlayerConfigurations[3].ready=false end\n'
    source += '\nControls.ReadyButton.callback()\nControls.ReadyCheck.callback()\n'
    source += f'assert(launches=={1 if succeeds else 0})\n'
    path = tmp_path / 'four-roster.lua'
    path.write_text(source)
    result = subprocess.run([executable, str(path)], capture_output=True, text=True, timeout=5)
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'POSTHOST_ROSTER|4_humans_no_extra_major_slots' in result.stdout
    if succeeds:
        assert 'UI_START_ROSTER|0,1,2,3|verified_after_native_refresh' in result.stdout
        assert all(f'P{pid}PW|\n' in result.stdout for pid in range(4))
    else:
        assert 'UI_START_ROSTER|failed|' in result.stdout


async def test_four_seat_full_helper_uses_one_connection(monkeypatch):
    conn = SimpleNamespace(lua_states={1: 'StagingRoom'}, disconnect=AsyncMock())
    connect = AsyncMock(return_value=conn)
    monkeypatch.setattr(launch, 'connect_state', connect)
    run = AsyncMock(side_effect=[['MAJOR_ROSTER|0,1,2,3|humans=true|extra_slots=closed'],
        ['POSTHOST_ROSTER|4_humans_no_extra_major_slots', 'UI_START_GUARD|bound_ready_controls']])
    monkeypatch.setattr(launch, 'run_lua', run)
    assert await launch.run_full('127.0.0.1', 4318, 'StagingRoom',
                                 empty_passwords=True, ui_start=True, seat_count=4) == 0
    connect.assert_awaited_once()
    assert run.await_count == 2
    assert all(call.args[0] is conn for call in run.await_args_list)
    conn.disconnect.assert_awaited_once()


async def test_four_seat_driver_fake_engine_completions_and_audits(tmp_path, monkeypatch):
    spec = load_config(CONFIG)
    spec = replace(spec, agents=[replace(a, policy='turtler', llm=None, decision_mode='legacy')
                                for a in reversed(spec.agents)])
    class Runtime:
        def __init__(self, profile):
            self.pid = profile.player_id
        async def take_turn(self, facade):
            for unit in await facade.get_units():
                if unit['owner_id'] == self.pid:
                    await facade.fortify(unit['unit_id'])
            assert (await facade.end_turn())['status'] == 'accepted'
    monkeypatch.setattr(ld, 'build_runtime', lambda profile, **kw: Runtime(profile))
    async def no_desktop(*args, **kwargs):
        pytest.fail('fake run attempted real desktop input')
    monkeypatch.setattr(ld.ui_control.Controller, 'action', no_desktop)
    server = FakeTunerServer(mod=FakeMod(hotseat=[3, 1, 0, 2]))
    port = await server.start()
    adapter = FireTunerAdapter('127.0.0.1', port, simulate_hook=ld._fake_hook, poll_timeout_s=0.1)
    try:
        code = await ld.phase_dispatch_hotseat(spec, adapter, tmp_path / 'run', 2, 'h1',
                                                ld.MOD_DEFAULT.read_text())
    finally:
        await server.stop()
    summary = json.loads((tmp_path / 'run/summary.json').read_text())
    assert code == 0, summary
    assert summary['completed_rounds'] == 2 and summary['violations_total'] == 0
    assert [(r['turn'], r['player']) for r in summary['per_turn']] == [
        (turn, pid) for turn in (1, 2) for pid in range(4)]
    result = validate(tmp_path / 'run', 2, require_live=False)
    assert result['errors'] == (['clean commit identity'] if summary['identity']['dirty'] else [])
    view = dashboard.DashboardStore(tmp_path).load('run')
    assert view['status'] == 'completed' and view['metrics']['completed_rounds'] == 2
    events = [json.loads(line) for line in (tmp_path / 'run/events.jsonl').read_text().splitlines()]
    calls = load_calls(events, {a.agent_id: a.player_id for a in spec.agents})
    assert set(calls) == set(range(4))
    for variant in ('missing', 'duplicate', 'reordered'):
        forged = copy.deepcopy(summary)
        if variant == 'missing':
            forged['per_turn'].pop(2)
        elif variant == 'duplicate':
            forged['per_turn'][2] = forged['per_turn'][1]
        else:
            forged['per_turn'][2], forged['per_turn'][3] = (
                forged['per_turn'][3], forged['per_turn'][2])
        events[-1]['summary'] = forged
        (tmp_path / 'run/summary.json').write_text(json.dumps(forged))
        (tmp_path / 'run/events.jsonl').write_text('\n'.join(json.dumps(e) for e in events) + '\n')
        result = validate(tmp_path / 'run', 2, require_live=False)
        assert result['status'] == 'FAIL'
        assert 'ordered seat/engine-turn pairs' in result['errors']


async def test_four_seat_simulator_replay_refuses_before_touching_artifacts(tmp_path):
    target = tmp_path / 'derived'
    target.mkdir()
    (target / 'events.jsonl').write_text('prior evidence\n')
    with pytest.raises(ValueError, match='simulator replay is unsupported'):
        await replay_run(tmp_path / 'source', load_config(CONFIG), target)
    assert (target / 'events.jsonl').read_text() == 'prior evidence\n'


def test_real_mod_hands_off_all_four_seats_then_advances_round(run_mod):  # noqa: F811
    rows = run_mod('''
local base = Players[0]
local owned = {}
for pid=0,3 do
  local id=pid
  local unit=newUnit(100+pid,1)
  owned[id]=unit
  local player={}
  for key,value in pairs(base) do player[key]=value end
  player.GetID=function() return id end
  player.GetUnits=function() return {
    Members=function() return ipairs({unit}) end,
    FindID=function(_,uid) if uid==unit.id then return unit end end}
  end
  Players[id]=player
  Puppeteer.SetPuppet(id,true)
end
PlayerManager={GetAliveMajors=function() return {Players[0],Players[1],Players[2],Players[3]} end}
PlayerManager.SetLocalPlayerAndObserver=function(nextPlayer)
  local previous=localPlayer
  assert(owned[previous].moves==0)
  localPlayer=nextPlayer
  hooks.deactivated(previous)
  if nextPlayer==0 then currentTurn=currentTurn+1 end
  hooks.start(nextPlayer)
end
Puppeteer.AttachCurrentTurn(0,1)
for pid=0,3 do
  local nextPlayer=(pid+1)%4
  Puppeteer.RestoreUnit(100+pid,pid)
  Puppeteer.GuardedHandoff(pid,1,nextPlayer)
  Puppeteer.Release(pid,1)
  assert(localPlayer==nextPlayer)
  print(Puppeteer.Status())
end
assert(currentTurn==2)
''')
    for pid in range(4):
        assert f'HANDOFF|{pid}|1|{(pid + 1) % 4}|accepted|frozen_then_switched' in rows
    assert 'LEASE_TURN|2' in rows and 'LEASE_PLAYER|0' in rows


async def test_multi_seat_legacy_startup_refuses_before_tuner_connect(monkeypatch):
    connect = AsyncMock(side_effect=AssertionError('no tuner connection'))
    monkeypatch.setattr(launch, 'connect_state', connect)
    with pytest.raises(ValueError, match='requires --ui-start'):
        await launch.run_full('127.0.0.1', 4318, 'StagingRoom', seat_count=4)
    connect.assert_not_awaited()


@pytest.mark.parametrize('pairs', [
    [(1, 0), (1, 1), (1, 2)],
    [(1, 0), (1, 1), (1, 2), (1, 2)],
    [(1, 0), (1, 1), (1, 3), (1, 2)],
    [(1, 0), (1, 1), (1, 2), (2, 3)],
])
def test_dashboard_refuses_partial_or_invalid_four_seat_terminal(tmp_path, pairs):
    from test_dashboard import NOW, event, write_run
    summary = {'clean': True, 'aborted': None, 'completed_rounds': 1}
    events = [event('MATCH_START', config={'agents': [
        [f'seat{pid}', pid, 'llm'] for pid in range(4)]})]
    events += [event('HEARTBEAT', audit='completed_seat_turn', row={
        'turn': turn, 'player': pid, 'agent': f'seat{pid}', 'lease_released': True})
        for turn, pid in pairs]
    events.append(event('MATCH_END', summary=summary))
    write_run(tmp_path, events, summary)
    assert dashboard.DashboardStore(tmp_path).load('match-one', now=NOW)['status'] == 'incomplete'


def test_dashboard_cannot_expand_two_declared_seats_with_extra_completion_rows(tmp_path):
    from test_dashboard import NOW, event, start, write_run
    summary = {'clean': True, 'aborted': None, 'completed_rounds': 1}
    events = [start()]
    events += [event('HEARTBEAT', audit='completed_seat_turn', row={
        'turn': 1, 'player': pid, 'agent': f'seat{pid}', 'lease_released': True})
        for pid in range(4)]
    events.append(event('MATCH_END', summary=summary))
    write_run(tmp_path, events, summary)
    view = dashboard.DashboardStore(tmp_path).load('match-one', now=NOW)
    assert view['status'] == 'incomplete'
    assert 'Observed seat identity is outside the configured roster.' in view['warnings']
