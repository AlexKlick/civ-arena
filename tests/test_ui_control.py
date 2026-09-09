from dataclasses import replace

import pytest

from civ_arena.game.civ6 import ui_control as ui


def test_select_ignores_overlay_and_hidden_windows(monkeypatch):
    def command(args, display):
        assert display == ':8'
        if '-root' in args:
            return '0x10, 0x20, 0x30'
        wid = args[args.index('-id') + 1]
        if 'WM_CLASS' in args:
            return 'WM_CLASS = "Steam"' if wid == '0x30' else 'WM_CLASS = "Civ6"'
        return ('Map State: ' + ('IsUnMapped' if wid == '0x20' else 'IsViewable')
                + '\nAbsolute upper-left X: 10\nAbsolute upper-left Y: 20\n'
                'Width: 800\nHeight: 600')
    monkeypatch.setattr(ui, 'command', command)
    assert ui.select_window(':8') == ui.Window(':8', 16, (10, 20, 800, 600))


def test_ambiguous_windows_fail(monkeypatch):
    def command(args, display):
        if '-root' in args:
            return '0x10, 0x20'
        if 'WM_CLASS' in args:
            return 'WM_CLASS = "Civ6"'
        return ('Map State: IsViewable\nAbsolute upper-left X: 0\n'
                'Absolute upper-left Y: 0\nWidth: 800\nHeight: 600')
    monkeypatch.setattr(ui, 'command', command)
    assert ui.perform(key='Return').status == 'failed'


@pytest.mark.parametrize('geometry', [(900, 30, 800, 600), (10, 20, 1600, 900)])
def test_changed_geometry_recaptures_and_relocates(monkeypatch, geometry):
    old = ui.Window(':1', 16, (10, 20, 800, 600))
    new = replace(old, geometry=geometry)
    windows = iter([old, old, new, new, new])
    captures, sent = [], []
    monkeypatch.setattr(ui, 'select_window', lambda _: next(windows))
    def capture(window):
        captures.append(window)
        return str(window.geometry).encode()
    monkeypatch.setattr(ui, 'capture', capture)
    monkeypatch.setattr(ui, 'find_teal_banner', lambda png: (0.2, 0.8))
    monkeypatch.setattr(ui, 'send', lambda window, **kw: sent.append(window))
    assert ui.perform(banner=True).status == 'sent'
    assert captures == [old, new]
    assert sent == [new]


@pytest.mark.parametrize('failure,status', [(ui.NoTarget('no window'), 'no_target'),
                                           (RuntimeError('capture failed'), 'failed')])
def test_missing_target_or_capture_never_inputs(monkeypatch, failure, status):
    def fail(_):
        raise failure
    monkeypatch.setattr(ui, 'select_window', fail)
    monkeypatch.setattr(ui, 'send', lambda *a, **kw: pytest.fail('input sent'))
    assert ui.perform(banner=True).status == status


def test_absent_banner(monkeypatch):
    monkeypatch.setattr(ui, 'select_window', lambda _: ui.Window(':1', 1, (0, 0, 80, 60)))
    monkeypatch.setattr(ui, 'capture', lambda _: b'png')
    monkeypatch.setattr(ui, 'find_teal_banner', lambda _: None)
    monkeypatch.setattr(ui, 'send', lambda *a, **kw: pytest.fail('input sent'))
    assert ui.perform(banner=True).status == 'no_target'


async def test_fake_never_spawns(monkeypatch):
    monkeypatch.setattr(ui.asyncio, 'create_subprocess_exec',
                        lambda *a, **kw: pytest.fail('helper spawned'))
    assert (await ui.FakeController().action(key='Return')).status == 'skipped_fake'


@pytest.mark.parametrize('changed', [
    ui.Window(':1', 17, (10,20,800,600)), ui.Window(':1',16,(15,20,800,600)),
    ui.Window(':1',16,(10,20,1024,768)),
])
def test_ui_geometry_target_never_rebases_after_frame_change(monkeypatch, changed):
    expected=ui.Window(':1',16,(10,20,800,600))
    monkeypatch.setattr(ui,'select_window',lambda _:changed)
    monkeypatch.setattr(ui,'capture',lambda _:pytest.fail('wrong frame capture'))
    monkeypatch.setattr(ui,'send',lambda *a,**kw:pytest.fail('stale coordinates clicked'))
    assert ui.perform(at=(0.5,0.5),expected_window=expected).status=='failed'


def test_ui_geometry_target_refuses_resize_during_capture(monkeypatch):
    expected=ui.Window(':1',16,(10,20,800,600))
    frames=iter([expected,expected,replace(expected,geometry=(10,20,1024,768)),
                 replace(expected,geometry=(10,20,1024,768))])
    monkeypatch.setattr(ui,'select_window',lambda _:next(frames))
    monkeypatch.setattr(ui,'capture',lambda _:b'pixels')
    monkeypatch.setattr(ui,'send',lambda *a,**kw:pytest.fail('stale coordinates clicked'))
    assert ui.perform(at=(0.5,0.5),expected_window=expected).status=='failed'


async def test_missing_display():
    result = await ui.Controller('').action(key='Return')
    assert result.status == 'failed'
    assert 'missing display' in result.diagnostic


@pytest.mark.parametrize('kind', ['import', 'nonzero', 'timeout'])
async def test_helper_failures(monkeypatch, kind):
    import asyncio

    class Proc:
        returncode = None if kind == 'timeout' else 1
        pid = 123456789
        async def communicate(self):
            if kind == 'timeout' and self.returncode is None:
                await asyncio.Event().wait()
            return b'', b'ModuleNotFoundError' if kind == 'import' else b'failed'
    proc = Proc()
    async def spawn(*a, **kw):
        assert a[1:3] == ('-m', 'civ_arena.game.civ6.ui_control')
        return proc
    def kill(*a):
        proc.returncode = -9
    monkeypatch.setattr(ui.asyncio, 'create_subprocess_exec', spawn)
    monkeypatch.setattr(ui.os, 'killpg', kill)
    result = await ui.Controller().action(key='Return', timeout=0.01)
    assert result.status == 'failed'
    assert ('timeout' if kind == 'timeout' else 'exit 1') in result.diagnostic


def test_ctypes_signatures():
    import ctypes
    x11, _ = ui.xlib()
    assert x11.XDefaultRootWindow.argtypes == [ctypes.c_void_p]
    assert x11.XDefaultRootWindow.restype == ctypes.c_ulong
    assert x11.XStringToKeysym.restype == ctypes.c_ulong
    assert x11.XKeysymToKeycode.argtypes == [ctypes.c_void_p, ctypes.c_ulong]
