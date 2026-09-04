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
