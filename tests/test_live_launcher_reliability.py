import asyncio
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

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
