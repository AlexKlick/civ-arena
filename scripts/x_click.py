"""M17b — synthetic X input for the zero-touch ladder (ctypes XTest).

No xdotool on this host; libX11 + libXtst are present and are all a
warp-and-click needs. The Civ VI window geometry comes from xwininfo
(window class "Civ6"); button coordinates are window-relative fractions
of that geometry, so a resolution change only edits the fractions.

    uv run python scripts/x_click.py --at 0.5,0.93   # click window center-bottom
    uv run python scripts/x_click.py --key space      # or just poke the keyboard
"""

from __future__ import annotations

import argparse
import ctypes
import subprocess
import sys
import time

BUTTON_LEFT = 1


def xlib():
    x11 = ctypes.cdll.LoadLibrary("libX11.so.6")
    xtst = ctypes.cdll.LoadLibrary("libXtst.so.6")
    for lib in (x11, xtst):
        lib.XOpenDisplay.restype = ctypes.c_void_p
        lib.XOpenDisplay.argtypes = [ctypes.c_char_p]
        lib.XFlush.argtypes = [ctypes.c_void_p]
        lib.XSync.argtypes = [ctypes.c_void_p, ctypes.c_int]
    xtst.XTestFakeButtonEvent.argtypes = [ctypes.c_void_p, ctypes.c_uint,
                                          ctypes.c_int, ctypes.c_ulong]
    x11.XWarpPointer.argtypes = [ctypes.c_void_p, ctypes.c_void_p,
                                 ctypes.c_ulong, ctypes.c_int, ctypes.c_int,
                                 ctypes.c_uint, ctypes.c_uint, ctypes.c_int,
                                 ctypes.c_int]
    xtst.XTestFakeKeyEvent.argtypes = [ctypes.c_void_p, ctypes.c_uint,
                                       ctypes.c_int, ctypes.c_ulong]
    return x11, xtst


def civ6_geometry() -> tuple[int, int, int, int]:
    """Absolute x, y, width, height of the Civ6 window on :1."""
    wid = subprocess.run(
        ["bash", "-c",
         "xprop -root _NET_CLIENT_LIST | grep -o '0x[0-9a-f]*' | tail -1"],
        capture_output=True, text=True,
        env={"DISPLAY": ":1", "PATH": "/usr/bin:/bin"}).stdout.strip()
    info = subprocess.run(
        ["xwininfo", "-id", wid],
        capture_output=True, text=True,
        env={"DISPLAY": ":1", "PATH": "/usr/bin:/bin:/usr/local/bin"}
    ).stdout
    geo: dict[str, int] = {}
    for line in info.splitlines():
        for key in ("Absolute upper-left X", "Absolute upper-left Y",
                    "Width", "Height"):
            if line.strip().startswith(key):
                geo[key] = int(line.split(":")[1])
    return (geo["Absolute upper-left X"], geo["Absolute upper-left Y"],
            geo["Width"], geo["Height"])


def civ6_window_id() -> int:
    """The Civ6 client window on :1 (highest id wins — topmost stacking)."""
    out = subprocess.run(
        ["bash", "-c", "xprop -root _NET_CLIENT_LIST"],
        capture_output=True, text=True,
        env={"DISPLAY": ":1", "PATH": "/usr/bin:/bin"}).stdout
    ids = [int(w, 16) for w in
           out.split(":", 1)[-1].replace(",", " ").split()
           if w.startswith("0x")]
    civ = []
    for wid in ids:
        cls = subprocess.run(
            ["xprop", "-id", str(wid), "WM_CLASS"],
            capture_output=True, text=True,
            env={"DISPLAY": ":1", "PATH": "/usr/bin:/bin"}).stdout
        if '"Civ6"' in cls:
            civ.append(wid)
    if not civ:
        raise RuntimeError(f"no Civ6 window in {ids}")
    return civ[-1]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--focus", action="store_true", default=True,
                    help="raise + focus the Civ6 window first (default)")
    ap.add_argument("--no-focus", dest="focus", action="store_false")
    ap.add_argument("--at", default=None,
                    help="window-relative click point as FRAC,FRAC (0..1)")
    ap.add_argument("--abs", default=None, help="absolute x,y")
    ap.add_argument("--key", default=None,
                    help="instead of a click, tap this keysym name (space, Return)")
    ap.add_argument("--display", default=":1")
    opts = ap.parse_args()

    x11, xtst = xlib()
    d = x11.XOpenDisplay(opts.display.encode())
    if not d:
        print(f"cannot open display {opts.display}", file=sys.stderr)
        return 2

    win = civ6_window_id()
    x11.XRaiseWindow.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
    x11.XSetInputFocus.argtypes = [ctypes.c_void_p, ctypes.c_ulong,
                                   ctypes.c_int, ctypes.c_ulong]
    if opts.focus:
        x11.XRaiseWindow(d, win)
        x11.XSetInputFocus(d, win, 1, 0)  # RevertToPointerRoot
        x11.XFlush(d)
        time.sleep(0.3)
        print(f"raised + focused {hex(win)}")

    if opts.key is not None:
        keysym = x11.XStringToKeysym(opts.key.encode())
        if not keysym:
            print(f"unknown keysym {opts.key}", file=sys.stderr)
            return 2
        code = x11.XKeysymToKeycode(d, keysym)
        xtst.XTestFakeKeyEvent(d, code, True, 0)
        xtst.XTestFakeKeyEvent(d, code, False, 0)
        x11.XFlush(d)
        print(f"tapped {opts.key} (keycode {code})")
        return 0

    if opts.abs:
        x, y = (int(v) for v in opts.abs.split(","))
    else:
        fx, fy = (float(v) for v in (opts.at or "0.5,0.5").split(","))
        wx, wy, ww, wh = civ6_geometry()
        print(f"civ6 window {ww}x{wh} at {wx},{wy}")
        x, y = int(wx + fx * ww), int(wy + fy * wh)
    # dest_w must be the ROOT window — dest None means a RELATIVE move
    # (the classic XWarpPointer trap: a None dest silently teleports the
    # cursor by (x, y) instead of to (x, y), landing it in a corner)
    x11.XWarpPointer(d, 0, x11.XDefaultRootWindow(d), 0, 0, 0, 0, x, y)
    x11.XFlush(d)
    time.sleep(0.15)
    xtst.XTestFakeButtonEvent(d, BUTTON_LEFT, True, 0)
    xtst.XTestFakeButtonEvent(d, BUTTON_LEFT, False, 0)
    x11.XFlush(d)
    x11.XSync(d, 0)
    print(f"clicked {x},{y}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
