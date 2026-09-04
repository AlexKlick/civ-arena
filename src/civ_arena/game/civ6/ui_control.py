"""Window-bound desktop input. `sent` means input, never engine progress."""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import ctypes
import hashlib
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import zlib
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass(frozen=True)
class Window:
    display: str
    window_id: int
    geometry: tuple[int, int, int, int]


@dataclass
class Outcome:
    status: str
    diagnostic: str = ""
    window: dict | None = None
    capture_sha256: str | None = None


class NoTarget(RuntimeError):
    pass


def redact(text: str) -> str:
    # Only bounded diagnostics escape a helper; never environment contents.
    for name, value in os.environ.items():
        if value and any(k in name.upper() for k in ("TOKEN", "SECRET", "PASSWORD", "API_KEY")):
            text = text.replace(value, "<redacted>")
    return re.sub(r"(?i)(bearer\s+|sk-)[^\s\"']+", r"\1<redacted>", text)[:1500]


def command(args: list[str], display: str) -> str:
    if not display:
        raise RuntimeError("missing display")
    return subprocess.run(args, check=True, capture_output=True, text=True,
                          timeout=4, env={**os.environ, "DISPLAY": display}).stdout


def select_window(display: str = ":1") -> Window:
    ids = re.findall(r"0x[0-9a-fA-F]+", command(
        ["xprop", "-root", "_NET_CLIENT_LIST"], display))
    matches = []
    for wid in dict.fromkeys(ids):
        cls = command(["xprop", "-id", wid, "WM_CLASS"], display)
        if "Civ6" not in re.findall(r'"([^"\n]+)"', cls):
            continue
        info = command(["xwininfo", "-id", wid], display)
        if "Map State: IsViewable" not in info:
            continue
        keys = ("Absolute upper-left X", "Absolute upper-left Y", "Width", "Height")
        geo = tuple(int(re.search(rf"{key}:\s*(-?\d+)", info)[1]) for key in keys)
        if geo[2] <= 0 or geo[3] <= 0:
            raise RuntimeError("invalid window geometry")
        matches.append(Window(display, int(wid, 16), geo))
    if not matches:
        raise NoTarget("no visible WM_CLASS=Civ6 window")
    if len(matches) != 1:
        raise RuntimeError("ambiguous visible WM_CLASS=Civ6 windows")
    return matches[0]


def capture(window: Window) -> bytes:
    x11, _ = xlib()
    d = x11.XOpenDisplay(window.display.encode())
    if not d:
        raise RuntimeError("cannot open display for capture")
    try:
        x11.XRaiseWindow(d, window.window_id)
        x11.XSetInputFocus(d, window.window_id, 1, 0)
        x11.XSync(d, 0)
    finally:
        x11.XCloseDisplay(d)
    # No whole-screen fallback: the pixels have exactly this coordinate frame.
    with tempfile.TemporaryDirectory(prefix="civ-ui-") as tmp:
        path = Path(tmp) / "frame.png"
        command(["scrot", "-a", ",".join(map(str, window.geometry)),
                 "-o", str(path)], window.display)
        data = path.read_bytes()
        w, h, _ = _decode_rgb(data)
        if (w, h) != window.geometry[2:]:
            raise RuntimeError("capture dimensions differ from window")
        return data


def xlib():
    x11 = ctypes.CDLL("libX11.so.6")
    xtst = ctypes.CDLL("libXtst.so.6")
    ptr, win, integer = ctypes.c_void_p, ctypes.c_ulong, ctypes.c_int
    declarations = {
        "XOpenDisplay": ([ctypes.c_char_p], ptr),
        "XCloseDisplay": ([ptr], integer),
        "XFlush": ([ptr], integer), "XSync": ([ptr, integer], integer),
        "XDefaultRootWindow": ([ptr], win),
        "XStringToKeysym": ([ctypes.c_char_p], ctypes.c_ulong),
        "XKeysymToKeycode": ([ptr, ctypes.c_ulong], ctypes.c_ubyte),
        "XRaiseWindow": ([ptr, win], integer),
        "XSetInputFocus": ([ptr, win, integer, ctypes.c_ulong], integer),
        "XWarpPointer": ([ptr, win, win, integer, integer, ctypes.c_uint,
                           ctypes.c_uint, integer, integer], integer),
    }
    for name, (args, result) in declarations.items():
        fn = getattr(x11, name)
        fn.argtypes, fn.restype = args, result
    for name in ("XTestFakeKeyEvent", "XTestFakeButtonEvent"):
        fn = getattr(xtst, name)
        fn.argtypes = [ptr, ctypes.c_uint, integer, ctypes.c_ulong]
        fn.restype = integer
    return x11, xtst


def send(window: Window, *, key: str | None = None,
         at: tuple[float, float] | None = None) -> None:
    x11, xtst = xlib()
    d = x11.XOpenDisplay(window.display.encode())
    if not d:
        raise RuntimeError("cannot open display")
    try:
        x11.XRaiseWindow(d, window.window_id)
        x11.XSetInputFocus(d, window.window_id, 1, 0)
        if key is not None:
            symbol = x11.XStringToKeysym(key.encode())
            code = x11.XKeysymToKeycode(d, symbol) if symbol else 0
            if not code:
                raise RuntimeError("unknown keysym")
            fn, value = xtst.XTestFakeKeyEvent, code
        else:
            if at is None or not all(0 <= v < 1 for v in at):
                raise ValueError("click fractions must be in [0,1)")
            wx, wy, w, h = window.geometry
            x11.XWarpPointer(d, 0, x11.XDefaultRootWindow(d), 0, 0, 0, 0,
                             int(wx + at[0] * w), int(wy + at[1] * h))
            fn, value = xtst.XTestFakeButtonEvent, 1
        pressed = fn(d, value, True, 0)
        released = fn(d, value, False, 0)
        x11.XSync(d, 0)
        if not pressed or not released:
            raise RuntimeError("XTest refused input")
    finally:
        x11.XCloseDisplay(d)


def perform(*, display: str = ":1", key: str | None = None,
            at: tuple[float, float] | None = None, banner: bool = False,
            evidence: Path | None = None) -> Outcome:
    try:
        original = select_window(display)
        for _ in range(3):
            window = select_window(display)
            if window.window_id != original.window_id:
                raise RuntimeError("target window identity changed")
            png = capture(window)
            point = find_teal_banner(png) if banner else at
            if select_window(display) != window:
                continue  # recapture and locate again in the new geometry
            digest = hashlib.sha256(png).hexdigest()
            if evidence is not None:
                evidence.parent.mkdir(parents=True, exist_ok=True)
                evidence.write_bytes(png)
            if banner and point is None:
                return Outcome("no_target", "banner absent", asdict(window), digest)
            # Focus/capture/input all use the same selected ID and geometry.
            send(window, key=key, at=point)
            return Outcome("sent", window=asdict(window), capture_sha256=digest)
        raise RuntimeError("window kept moving or resizing; no input sent")
    except NoTarget as exc:
        return Outcome("no_target", str(exc))
    except Exception as exc:
        return Outcome("failed", redact(f"{type(exc).__name__}: {exc}"))


class Controller:
    def __init__(self, display: str = ":1") -> None:
        self.display = display

    async def action(self, *, key: str | None = None, banner: bool = False,
                     at: tuple[float, float] | None = None,
                     timeout: float = 15, evidence: Path | None = None) -> Outcome:
        args = [sys.executable, "-m", "civ_arena.game.civ6.ui_control",
                "--display", self.display]
        if key is not None:
            args += ["--key", key]
        if banner:
            args += ["--banner"]
        if at is not None:
            args += ["--at", ",".join(map(str, at))]
        if evidence is not None:
            args += ["--evidence", str(evidence)]
        proc = None
        try:
            async with asyncio.timeout(timeout):
                proc = await asyncio.create_subprocess_exec(
                    *args, stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE, start_new_session=True)
                stdout, stderr = await proc.communicate()
            if proc.returncode:
                return Outcome("failed", redact(
                    f"helper exit {proc.returncode}: {stderr.decode(errors='replace')} "
                    f"{stdout.decode(errors='replace')}"))
            doc = json.loads(stdout)
            outcome = Outcome(**doc)
            if outcome.status not in {"sent", "no_target", "failed", "skipped_fake"}:
                raise ValueError("invalid helper outcome")
            return outcome
        except TimeoutError:
            return Outcome("failed", "helper timeout")
        except Exception as exc:
            return Outcome("failed", redact(f"{type(exc).__name__}: {exc}"))
        finally:
            if proc is not None and proc.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(proc.pid, signal.SIGKILL)
                await proc.communicate()


class FakeController:
    async def action(self, **kwargs) -> Outcome:
        return Outcome("skipped_fake", "fake game; desktop input disabled")


def _paeth(a: int, b: int, c: int) -> int:
    p = a + b - c
    pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
    if pa <= pb and pa <= pc:
        return a
    return b if pb <= pc else c


def _decode_rgb(png: bytes) -> tuple[int, int, list[list[tuple[int, int, int]]]]:
    """Minimal PNG reader for scrot output: 8-bit truecolor with the FULL
    adaptive-filter repertoire (none/sub/up/average/paeth — scrot filters
    rows, and flat regions filter to zeros, so ignoring filters reads a
    live screen as black)."""
    assert png[:8] == b"\x89PNG\r\n\x1a\n", "not a PNG"
    pos, width, height, bitd, ctype = 8, 0, 0, 0, 0
    idat = b""
    while pos < len(png):
        length = int.from_bytes(png[pos:pos + 4], "big")
        ctype_bytes = png[pos + 4:pos + 8]
        data = png[pos + 8:pos + 8 + length]
        if ctype_bytes == b"IHDR":
            width = int.from_bytes(data[0:4], "big")
            height = int.from_bytes(data[4:8], "big")
            bitd, ctype = data[8], data[9]
        elif ctype_bytes == b"IDAT":
            idat += data
        pos += 12 + length
    if bitd != 8 or ctype != 2:
        raise ValueError(f"unsupported PNG (depth={bitd} type={ctype}) — "
                         "re-encode via ffmpeg")
    raw = zlib.decompress(idat)
    stride = width * 3 + 1
    rows: list[list[tuple[int, int, int]]] = []
    prev = bytearray(width * 3)
    for y in range(height):
        ftype = raw[y * stride]
        row = bytearray(raw[y * stride + 1:(y + 1) * stride])
        for i in range(len(row)):
            a = row[i - 3] if i >= 3 else 0
            b = prev[i]
            c = prev[i - 3] if i >= 3 else 0
            if ftype == 1:
                row[i] = (row[i] + a) & 0xFF
            elif ftype == 2:
                row[i] = (row[i] + b) & 0xFF
            elif ftype == 3:
                row[i] = (row[i] + (a + b) // 2) & 0xFF
            elif ftype == 4:
                row[i] = (row[i] + _paeth(a, b, c)) & 0xFF
            elif ftype != 0:
                raise ValueError(f"unknown PNG filter {ftype}")
        prev = row
        rows.append([(row[x * 3], row[x * 3 + 1], row[x * 3 + 2])
                     for x in range(width)])
    return width, height, rows


def find_teal_banner(png: bytes) -> tuple[float, float] | None:
    """The BEGIN GAME / CONTINUE GAME ribbon by pixel color (teal: blue+
    green high, red low) — locate UI by pixels, not by a vision model's
    guess. Attempt-6 lesson (live, 2026-09-03): return the DENSEST teal
    cluster, never the bbox of every hit — ocean water passes the same
    filter, and the diluted bbox center clicked open water beside the
    ribbon. The load-path intro ribbon sits at window y~0.93; scan the
    whole lower half."""
    w, h, rows = _decode_rgb(png)
    grid = 32
    counts: dict[tuple[int, int], int] = {}
    for y in range(int(h * 0.50), h, 2):
        for x in range(0, w, 2):
            r, g, b = rows[y][x]
            if b > 120 and g > 110 and r < 90 and (b - r) > 60:
                cell = (x // grid, y // grid)
                counts[cell] = counts.get(cell, 0) + 1
    if not counts:
        return None

    def hood(cx: int, cy: int) -> int:
        return sum(counts.get((cx + dx, cy + dy), 0)
                   for dx in (-1, 0, 1) for dy in (-1, 0, 1))

    bx, by = max(counts, key=lambda c: hood(*c))
    sx = sy = tot = 0
    for (cx, cy), n in counts.items():
        if abs(cx - bx) <= 1 and abs(cy - by) <= 1:
            sx += (cx + 0.5) * grid * n
            sy += (cy + 0.5) * grid * n
            tot += n
    return (sx / tot / w, sy / tot / h)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--display", default=":1")
    group = ap.add_mutually_exclusive_group(required=True)
    group.add_argument("--key")
    group.add_argument("--at")
    group.add_argument("--banner", action="store_true")
    ap.add_argument("--evidence", type=Path)
    opts = ap.parse_args()
    result = perform(display=opts.display, key=opts.key, banner=opts.banner,
                     at=tuple(map(float, opts.at.split(","))) if opts.at else None,
                     evidence=opts.evidence)
    print(json.dumps(asdict(result)))
    return 1 if result.status == "failed" else 0


if __name__ == "__main__":
    raise SystemExit(main())
