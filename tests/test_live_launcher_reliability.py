import asyncio
import importlib.util
import json
import re
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

SPEC = importlib.util.spec_from_file_location(
    'zero_touch', Path(__file__).resolve().parents[1] / 'scripts/live_zero_touch.py')
z = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(z)


async def test_launcher_subprocess_result_is_awaited(monkeypatch):
    async def run(args, **kw):
        return SimpleNamespace(stdout='127.0.0.1:4318\n', returncode=0)
    monkeypatch.setattr(z, 'run', run)
    assert await z.port_up()


async def test_startup_deadline_preserves_saves_and_terminal_record(tmp_path, monkeypatch):
    saves = tmp_path / 'saves'
    saves.mkdir()
    (saves / 'old.Civ6Save').write_bytes(b'old save')
    monkeypatch.setattr(z, 'SAVES', saves)
    async def stall(opts):
        await asyncio.Event().wait()
    monkeypatch.setattr(z, 'run_arch1_session', stall)
    opts = SimpleNamespace(run_id='unique', runs_root=tmp_path / 'runs',
                           startup_timeout=0.03, config='unused')
    assert await z.controlled_arch1(opts) == 2
    assert (opts.artifacts / 'saves-before/old.Civ6Save').read_bytes() == b'old save'
    summary = json.loads((opts.artifacts / 'summary.json').read_text())
    records = [json.loads(x) for x in (opts.artifacts / 'events.jsonl').read_text().splitlines()]
    assert [r['kind'] for r in records] == ['MATCH_START', 'MATCH_END']
    assert records[-1]['summary'] == summary
    assert not summary['clean'] and 'TimeoutError' in summary['aborted']
    with pytest.raises(FileExistsError):
        await z.controlled_arch1(opts)


def test_slot_backup_is_run_specific(tmp_path, monkeypatch):
    saves = tmp_path / 'saves'
    source = saves / 'Hotseat/quick/quicksave.Civ6Save'
    dest = saves / 'Single/auto/AutoSave_0001.Civ6Save'
    source.parent.mkdir(parents=True)
    dest.parent.mkdir(parents=True)
    source.write_bytes(b'new')
    dest.write_bytes(b'old')
    monkeypatch.setattr(z, 'SAVES', saves)
    assert z.swap_save_into_load_slot(tmp_path / 'run-a')
    source.write_bytes(b'next')
    assert z.swap_save_into_load_slot(tmp_path / 'run-b')
    assert (tmp_path / 'run-a' / dest.name).read_bytes() == b'old'
    assert (tmp_path / 'run-b' / dest.name).read_bytes() == b'new'


def test_launcher_allows_helper_own_transition_budget():
    import inspect
    # --full itself waits up to 180s after its initial wire setup.
    assert inspect.signature(z.run).parameters['timeout'].default > 180


def test_launch_preserves_previous_diagnostics(tmp_path, monkeypatch):
    old = tmp_path / 'civ6-zero.log'
    old.write_bytes(b'previous diagnosis')
    calls = []
    monkeypatch.setattr(z.subprocess, 'Popen', lambda *args, **kw: calls.append((args, kw)))
    z.launch(tmp_path)
    z.launch(tmp_path)
    assert old.read_bytes() == b'previous diagnosis'
    assert len(list(tmp_path.glob('civ6-launch-*.log'))) == 2
    assert all(call[0][0][0] == '/usr/games/steam' for call in calls)


async def test_launcher_forwards_only_remaining_startup_budget(tmp_path, monkeypatch):
    monkeypatch.setattr(z, 'SAVES', tmp_path / 'absent-saves')
    monkeypatch.setattr(z, 'TUNER_COOLDOWN_S', 0.01)
    async def startup(opts):
        await asyncio.sleep(0.01)
        return 0
    monkeypatch.setattr(z, 'run_arch1_session', startup)
    captured = []
    class Proc:
        returncode = 0
        async def wait(self):
            return 0
    async def spawn(*args, **kwargs):
        captured.extend(args)
        return Proc()
    monkeypatch.setattr(z.asyncio, 'create_subprocess_exec', spawn)
    opts = SimpleNamespace(run_id='remaining', runs_root=tmp_path / 'runs',
                           startup_timeout=0.3, config='config.yaml', rounds=3,
                           match_timeout=7200, agent_turn_timeout=600,
                           recovery_timeout=180, recovery_sweeps=8)
    assert await z.controlled_arch1(opts) == 0
    forwarded = float(captured[captured.index('--startup-timeout') + 1])
    assert 0 < forwarded < 0.29
    summary = json.loads((opts.artifacts / 'summary.json').read_text())
    assert summary['startup_timeout_s'] == 0.3
    assert summary['driver_startup_timeout_s'] == forwarded
    assert summary['elapsed_s'] + forwarded <= 0.31


@pytest.mark.parametrize('output', [
    'HDMI-0 connected primary 1920x1080+0+0 (normal left inverted right x axis y axis)',
    'DFP-0 connected 2944x1840+0+0 (normal left inverted right x axis y axis)',
    'VIRTUAL-1 connected 1280x720-1280+0 (normal left inverted right x axis y axis)',
])
async def test_display_preflight_accepts_physical_and_headless_outputs(
        output, tmp_path, monkeypatch):
    async def run(args, **kw):
        assert args == ['xrandr', '--display', ':1', '--query']
        assert kw['timeout'] == 10
        return SimpleNamespace(returncode=0, stdout=output, stderr='')
    monkeypatch.setattr(z, 'run', run)
    await z.require_active_display(tmp_path)
    records = list(tmp_path.glob('display-preflight-*.json'))
    assert len(records) == 1
    assert json.loads(records[0].read_text())['active_outputs'] == [output.split()[0]]


@pytest.mark.parametrize('output', [
    'Screen 0: minimum 8 x 8, current 640 x 480, maximum 32767 x 32767\n'
    'HDMI-0 disconnected primary (normal left inverted right x axis y axis)',
    'HDMI-0 connected primary (normal left inverted right x axis y axis)',
    'VIRTUAL-1 connected 0x0+0+0 (normal left inverted right x axis y axis)',
])
async def test_display_preflight_rejects_absent_or_inactive_output(
        output, tmp_path, monkeypatch):
    async def run(args, **kw):
        return SimpleNamespace(returncode=0, stdout=output, stderr='')
    monkeypatch.setattr(z, 'run', run)
    with pytest.raises(RuntimeError, match='no verified active output'):
        await z.require_active_display(tmp_path)
    diagnostic = json.loads(next(tmp_path.glob('display-preflight-*.json')).read_text())
    assert diagnostic['active_outputs'] == []
    assert diagnostic['stdout'] == output


@pytest.mark.parametrize('failure', ['nonzero', 'missing', 'timeout'])
async def test_display_preflight_retains_helper_failures(failure, tmp_path, monkeypatch):
    async def run(args, **kw):
        if failure == 'missing':
            raise FileNotFoundError('xrandr')
        if failure == 'timeout':
            raise TimeoutError('display probe')
        return SimpleNamespace(returncode=1, stdout='', stderr="Can't open display :1")
    monkeypatch.setattr(z, 'run', run)
    with pytest.raises(RuntimeError, match='display preflight failed'):
        await z.require_active_display(tmp_path)
    diagnostic = json.loads(next(tmp_path.glob('display-preflight-*.json')).read_text())
    if failure == 'nonzero':
        assert diagnostic['returncode'] == 1
        assert diagnostic['stderr'] == "Can't open display :1"
    else:
        assert diagnostic['returncode'] is None
        expected = 'FileNotFoundError' if failure == 'missing' else 'TimeoutError'
        assert expected in diagnostic['error']


@pytest.mark.parametrize('failure_at', [0, 1])
async def test_arch1_display_failure_preserves_terminal_before_game_launch(
        failure_at, tmp_path, monkeypatch):
    monkeypatch.setattr(z, 'SAVES', tmp_path / 'absent-saves')
    actions = []
    async def display(artifacts):
        actions.append('display')
        if actions.count('display') == failure_at + 1:
            raise RuntimeError('display preflight failed: :1 has no verified active output')
    async def bounce():
        actions.append('bounce')
        return True
    async def kill():
        pytest.fail('must not kill or launch a game after failed display preflight')
    monkeypatch.setattr(z, 'require_active_display', display)
    monkeypatch.setattr(z, 'bounce_x', bounce)
    monkeypatch.setattr(z, 'kill_game', kill)
    monkeypatch.setattr(z, 'launch', lambda *a: pytest.fail('must not launch'))
    opts = SimpleNamespace(run_id='display-failed', runs_root=tmp_path / 'runs',
                           startup_timeout=1, config='unused', fresh_x=True)
    assert await z.controlled_arch1(opts) == 2
    assert actions == (['display'] if failure_at == 0 else ['display', 'bounce', 'display'])
    summary = json.loads((opts.artifacts / 'summary.json').read_text())
    records = [json.loads(x) for x in (opts.artifacts / 'events.jsonl').read_text().splitlines()]
    assert not summary['clean'] and 'display preflight failed' in summary['aborted']
    assert [row['kind'] for row in records] == ['MATCH_START', 'MATCH_END']
    assert records[-1]['summary'] == summary


async def test_arch1_from_menu_preserves_process_and_skips_intro_key(tmp_path, monkeypatch):
    calls = []
    async def display(*args):
        calls.append('display')
    async def no_kill():
        pytest.fail('must preserve the user-started game')
    async def no_key(*args):
        pytest.fail('must not send an intro Escape at the main menu')
    async def ready(check, budget, label, *args):
        calls.append(label)
        return True
    async def phase(args, **kwargs):
        calls.append('host-configure')
        # Stop before hosting completes; no live save/UI operations in this test.
        return SimpleNamespace(stdout='', returncode=1)
    async def normalize(*args):
        calls.append('normalize')
    monkeypatch.setattr(z, 'require_active_display', display)
    monkeypatch.setattr(z, 'kill_game', no_kill)
    monkeypatch.setattr(z, 'launch', lambda *a: pytest.fail('must not relaunch'))
    monkeypatch.setattr(z, 'key', no_key)
    monkeypatch.setattr(z, 'wait_for', ready)
    monkeypatch.setattr(z, 'phase', phase)
    monkeypatch.setattr(z, 'normalize_game_window', normalize)
    opts = SimpleNamespace(artifacts=tmp_path, fresh_x=False, kill_first=False)
    assert await z.run_arch1_session(opts) == 23
    assert calls == ['display', 'tuner-bind', 'menu', 'normalize', 'host-configure']


async def test_from_menu_refuses_x_restart_before_bounce(tmp_path, monkeypatch):
    async def display(*args):
        pass
    async def bounce():
        pytest.fail('must preserve the running session')
    monkeypatch.setattr(z, 'require_active_display', display)
    monkeypatch.setattr(z, 'bounce_x', bounce)
    with pytest.raises(ValueError, match='cannot restart X'):
        await z.run_arch1_session(SimpleNamespace(
            artifacts=tmp_path, fresh_x=True, kill_first=False))


def resolution_reply(lua, *, result='ok', applied='true', after='1024|768|0'):
    token = re.search(r"RESOLUTION\|([a-f0-9]+)\|", lua)[1]
    return [f'RESOLUTION|{token}|before|2881|1788|0',
            f'RESOLUTION|{token}|applied|{applied}',
            f'RESOLUTION|{token}|after|{after}',
            f'RESOLUTION|{token}|result|{result}', f'RESOLUTION_END|{token}']


@pytest.fixture
def resolution_fixture(monkeypatch):
    original = z.ui_control.Window(':1', 10, (0, 0, 2881, 1788))
    resized = z.ui_control.Window(':1', 10, (0, 0, 1024, 768))
    windows = iter([original, resized])
    monkeypatch.setattr(z.ui_control, 'select_window', lambda _display: next(windows))
    monkeypatch.setattr(z, 'TUNER_COOLDOWN_S', 0)
    monkeypatch.setattr(z, 'WINDOW_RESIZE_S', 0.02)
    conn = SimpleNamespace(
        connect=AsyncMock(), disconnect=AsyncMock(), _lock=asyncio.Lock(), is_connected=True,
        lua_states={4: 'Options'},
        _locked_execute=AsyncMock(side_effect=lambda _i, lua, _t: resolution_reply(lua)))
    monkeypatch.setattr(z, 'GameConnection', lambda *args: conn)
    return conn


async def test_runtime_resolution_apply_and_geometry_are_verified_without_saving(
        resolution_fixture, tmp_path):
    conn = resolution_fixture
    await z.normalize_game_window(tmp_path)
    conn.connect.assert_awaited_once()
    conn.disconnect.assert_awaited_once()
    conn._locked_execute.assert_awaited_once()
    state, lua, timeout = conn._locked_execute.await_args.args
    assert state == 4 and timeout == 8.0
    assert "ContextPtr:GetID() ~= 'Options'" in lua
    assert "Options.SetAppOption('Video', 'FullScreen', 0)" in lua
    assert 'SaveOptions' not in lua and 'OnConfirm' not in lua
    record = json.loads(next(tmp_path.glob('window-normalization-*.json')).read_text())
    assert record['status'] == 'verified' and record['cleanup'] == 'disconnected'
    assert record['options']['after'] == [1024, 768, 0]
    assert record['window_after']['geometry'][2:] == [1024, 768]


@pytest.mark.parametrize('failure', [
    'missing_options', 'apply_false', 'wrong_readback', 'wrong_geometry', 'wrong_window',
    'missing_state', 'ambiguous_state', 'disconnected', 'incomplete',
])
async def test_resolution_failure_stops_before_first_menu_input_or_host(
        failure, resolution_fixture, tmp_path, monkeypatch):
    conn = resolution_fixture
    if failure == 'missing_options':
        conn._locked_execute.side_effect = lambda _i, lua, _t: resolution_reply(
            lua, result='missing_options_api')
    elif failure == 'apply_false':
        conn._locked_execute.side_effect = lambda _i, lua, _t: resolution_reply(
            lua, result='apply_failed', applied='false')
    elif failure == 'wrong_readback':
        conn._locked_execute.side_effect = lambda _i, lua, _t: resolution_reply(
            lua, after='2881|1788|0')
    elif failure == 'wrong_geometry':
        monkeypatch.setattr(z.ui_control, 'select_window', lambda _: z.ui_control.Window(
            ':1', 10, (0, 0, 2881, 1788)))
    elif failure == 'wrong_window':
        windows = iter([z.ui_control.Window(':1', 10, (0, 0, 2881, 1788)),
                        z.ui_control.Window(':1', 11, (0, 0, 1024, 768))])
        monkeypatch.setattr(z.ui_control, 'select_window', lambda _: next(windows))
    elif failure == 'missing_state':
        conn.lua_states = {}
    elif failure == 'ambiguous_state':
        conn.lua_states[5] = 'Options'
    elif failure == 'disconnected':
        conn.is_connected = False
    else:
        conn._locked_execute.side_effect = lambda _i, lua, _t: resolution_reply(lua)[:-1]
    monkeypatch.setattr(z, 'require_active_display', AsyncMock())
    monkeypatch.setattr(z, 'wait_for', AsyncMock(return_value=True))
    host, key, click = AsyncMock(), AsyncMock(), AsyncMock()
    monkeypatch.setattr(z, 'phase', host)
    monkeypatch.setattr(z, 'key', key)
    monkeypatch.setattr(z, 'click', click)
    opts = SimpleNamespace(artifacts=tmp_path, fresh_x=False, kill_first=False)
    with pytest.raises(RuntimeError, match='window normalization failed'):
        await z.run_arch1_session(opts)
    host.assert_not_awaited()
    key.assert_not_awaited()
    click.assert_not_awaited()
    conn.disconnect.assert_awaited_once()
    record = json.loads(next(tmp_path.glob('window-normalization-*.json')).read_text())
    assert record['status'] == 'failed'


@pytest.mark.parametrize('stage', ['connect', 'apply', 'disconnect'])
async def test_resolution_timeout_closes_connection_and_preserves_failed_diagnostic(
        stage, resolution_fixture, tmp_path, monkeypatch):
    conn = resolution_fixture
    monkeypatch.setattr(z, 'WINDOW_NORMALIZE_S', 0.02)
    monkeypatch.setattr(z, 'WINDOW_DISCONNECT_S', 0.02)
    async def stall(*args):
        await asyncio.Event().wait()
    getattr(conn, {'connect': 'connect', 'apply': '_locked_execute',
                   'disconnect': 'disconnect'}[stage]).side_effect = stall
    with pytest.raises(RuntimeError, match='window normalization failed'):
        await z.normalize_game_window(tmp_path)
    conn.disconnect.assert_awaited_once()
    record = json.loads(next(tmp_path.glob('window-normalization-*.json')).read_text())
    assert record['status'] == 'failed'
    assert 'TimeoutError' in str(record)


async def test_second_launch_normalization_precedes_load_menu_inputs(tmp_path, monkeypatch):
    saves = tmp_path / 'saves'
    for name in ('Hotseat/auto/AutoSave_0001.Civ6Save', 'Hotseat/quick/quicksave.Civ6Save'):
        path = saves / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'fixture')
    monkeypatch.setattr(z, 'SAVES', saves)
    monkeypatch.setattr(z.time, 'time', lambda: 0)
    monkeypatch.setattr(z.asyncio, 'sleep', AsyncMock())
    monkeypatch.setattr(z, 'require_active_display', AsyncMock())
    monkeypatch.setattr(z, 'wait_for', AsyncMock(return_value=True))
    normalize = AsyncMock(side_effect=[None, RuntimeError('normalization failed second menu')])
    monkeypatch.setattr(z, 'normalize_game_window', normalize)
    monkeypatch.setattr(z, 'phase', AsyncMock(return_value=SimpleNamespace(
        stdout='InSession|true\nP0PW|\nP1PW|\nUI_START_READY|posthost_roster_verified\n',
        returncode=0)))
    monkeypatch.setattr(z, 'swap_save_into_load_slot', lambda *_: True)
    kill, key, click = AsyncMock(), AsyncMock(), AsyncMock()
    monkeypatch.setattr(z, 'kill_game', kill)
    monkeypatch.setattr(z, 'launch', lambda *_: None)
    monkeypatch.setattr(z, 'key', key)
    monkeypatch.setattr(z, 'click', click)
    with pytest.raises(RuntimeError, match='second menu'):
        await z.run_arch1_session(SimpleNamespace(
            artifacts=tmp_path, fresh_x=False, kill_first=False))
    assert normalize.await_count == 2
    kill.assert_awaited_once()  # only the required A1 save/load restart
    assert [call.args for call in click.await_args_list] == [(0.50, 0.888), (0.50, 0.383)]
    key.assert_awaited_once_with('Escape')  # quicksave menu, no second-menu Escape


@pytest.mark.parametrize('smoke_rc', [0, 10])
async def test_arch1_waits_between_each_final_tuner_client(smoke_rc, tmp_path, monkeypatch):
    saves = tmp_path / 'saves'
    for name in ('Hotseat/auto/AutoSave_0001.Civ6Save', 'Hotseat/quick/quicksave.Civ6Save'):
        path = saves / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'fixture')
    monkeypatch.setattr(z, 'SAVES', saves)
    monkeypatch.setattr(z.time, 'time', lambda: 0)
    # A distinct value proves every reconnect follows the configured discipline.
    monkeypatch.setattr(z, 'TUNER_COOLDOWN_S', 11)
    actions = []

    async def sleep(seconds):
        actions.append(('sleep', seconds))

    async def run(args, **kwargs):
        script = Path(args[1]).name
        actions.append(('client', script, tuple(args[2:])))
        if '--full' in args:
            output = ('InSession|true\nP0PW|\nP1PW|\n'
                      'UI_START_READY|posthost_roster_verified\n')
        elif '--load' in args:
            output = 'loadgame-returned|true'
        elif '--reflag' in args:
            output = 'REFLAG_SLOT|1|3\nREFLAG_CFGHUMAN|1|true'
        elif script == 'live_seat_check.py':
            output = ('P0|human=true|major=true|alive=true|slot=3|cfghuman=true\n'
                      'P1|human=true|major=true|alive=true|slot=3|cfghuman=true\n'
                      'CENSUS_END|2\n')
        else:
            assert script == 'firetuner_smoke.py'
            output = 'SMOKE OK' if smoke_rc == 0 else 'SMOKE FAILED'
        return SimpleNamespace(stdout=output, stderr='',
                               returncode=smoke_rc if script == 'firetuner_smoke.py' else 0)

    monkeypatch.setattr(z.asyncio, 'sleep', sleep)
    monkeypatch.setattr(z, 'run', run)
    for method in ('require_active_display', 'normalize_game_window', 'kill_game', 'key', 'click'):
        monkeypatch.setattr(z, method, AsyncMock())
    monkeypatch.setattr(z, 'wait_for', AsyncMock(return_value=True))
    monkeypatch.setattr(z, 'tuner_states', AsyncMock(return_value=['GameCore_Tuner']))
    monkeypatch.setattr(z, 'launch', lambda *_: None)
    monkeypatch.setattr(z, 'swap_save_into_load_slot', lambda *_: True)
    opts = SimpleNamespace(artifacts=tmp_path, fresh_x=False, kill_first=False,
                           no_smoke=False, config='unused')
    assert await z.run_arch1_session(opts) == (0 if smoke_rc == 0 else 32)
    clients = [(i, row) for i, row in enumerate(actions) if row[0] == 'client']
    assert [row[1] for _, row in clients[-4:]] == [
        'live_seat_check.py', 'live_hotseat_launch.py',
        'live_seat_check.py', 'firetuner_smoke.py']
    for i, _ in clients[-4:]:
        assert actions[i - 1] == ('sleep', 11)


@pytest.mark.parametrize('variant, expected', [
    ('success', 'ok'), ('false_apply', 'apply_failed'), ('missing_api', 'missing_options_api'),
])
def test_resolution_lua_executes_game_owned_api_without_saving(variant, expected, tmp_path):
    executable = shutil.which('texlua')
    if executable is None:
        pytest.skip('texlua unavailable for executable Options fixture')
    source = """
local values = {RenderWidth=2881, RenderHeight=1788, FullScreen=2}
ContextPtr = {GetID=function() return 'Options' end}
Options = {
  GetAppOption=function(_, name) return values[name] end,
  SetAppOption=function(_, name, value) values[name]=value end,
  GetAvailableDisplayModes=function() return {{Width=1024, Height=768}} end,
  ApplyGraphicsOptions=function() return true end,
  SaveOptions=function() error('must not persist settings') end
}
"""
    if variant == 'false_apply':
        source += 'Options.ApplyGraphicsOptions=function() return false end\n'
    elif variant == 'missing_api':
        source += 'Options=nil\n'
    path = tmp_path / 'resolution.lua'
    path.write_text(source + z.window_resolution_lua('abc'))
    result = subprocess.run([executable, str(path)], capture_output=True, text=True,
                            timeout=5, check=True)
    observed = z.parse_window_resolution(result.stdout.splitlines(), 'abc')
    assert observed['result'] == expected
    if variant == 'success':
        assert observed['before'] == [2881, 1788, 2]
        assert observed['after'] == [1024, 768, 0]
        assert observed['applied'] == 'true'
