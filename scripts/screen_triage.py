"""M17b — screen triage for the live gaming session (Xorg :1).

Two DETERMINISTIC diagnostics that need no vision model, plus optional
VL backends behind a seam:

- ``classify``: pixel statistics — brightness, saturation, palette
  variety, edge density — mapped to the live lane's canonical states.
  Distinguishes blank desktop / dark menu / busy game UI / bright modal
  without any model.
- ``watch``: freeze detection — two captures ``--interval`` seconds
  apart; byte-identical (or near-identical) frames mean the engine is
  wedged. This is the decisive wedge diagnostic (runs 010/011's "the
  AI's turn never started" would have been caught in one command).

VL findings, corrected 2026-08-31: the host's 4B VL lane (:18001)
read EVERY real capture correctly (the blank-desktop screens really
were uniform dark — verified by md5-identical frames, ffmpeg
signalstats YAVG=66, and a 17 KB compression size for 3440x1440).
The initial "VL can't read dark UI" conclusion was poisoned by
unreliable image PREVIEWS in the agent harness (cache-collided
renders showing a Steam UI that was never in the bytes). LESSON:
ground-truth pixels with local decoders, never with previews.
Z.AI's anthropic-compat /v1/messages returned empty content for
image blocks in the same session — that path stays unwired.

    uv run python scripts/screen_triage.py                      # classify
    uv run python scripts/screen_triage.py watch --interval 15  # freeze?
    uv run python scripts/screen_triage.py --image x.png
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
import zlib
from pathlib import Path

DISPLAY = ":1"

# classification thresholds (hand-set against the 2026-08-31 captures:
# blank openbox desktop, Steam client dark UI)
EDGE_DENSITY_GAME = 0.045     # fraction of pixel pairs that differ hard
PALETTE_GAME = 900            # distinct quantized colors in a busy game
BRIGHT_MODAL = 150            # mean brightness of a light popup


def capture(display: str = DISPLAY, region: str | None = None) -> bytes:
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as fh:
        path = fh.name
    cmd = ["scrot", "-o", path] + (["-a", region] if region else [])
    subprocess.run(cmd, check=True,
                   env={"DISPLAY": display, "PATH": "/usr/bin:/bin:/usr/local/bin"})
    data = Path(path).read_bytes()
    Path(path).unlink(missing_ok=True)
    return data


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


def pixel_stats(png: bytes, sample: int = 4) -> dict:
    """Deterministic stats on a pixel lattice: brightness, saturation,
    palette variety, edge density."""
    width, height, rows = _decode_rgb(png)
    total = bright = sat_hi = 0
    palette: set[tuple[int, int, int]] = set()
    edges = edge_pairs = 0
    for y in range(0, height, sample):
        row = rows[y]
        for x in range(0, width, sample):
            r, g, b = row[x]
            total += 1
            bright += (r + g + b) // 3
            mx, mn = max(r, g, b), min(r, g, b)
            if mx > 0 and (mx - mn) * 100 // mx > 35:
                sat_hi += 1
            palette.add((r // 24, g // 24, b // 24))
            if x + sample < width:
                r2, g2, b2 = row[x + sample]
                if abs(r - r2) + abs(g - g2) + abs(b - b2) > 90:
                    edges += 1
                edge_pairs += 1
    return {
        "width": width, "height": height, "sampled": total,
        "mean_brightness": bright // max(1, total),
        "sat_frac": round(sat_hi / max(1, total), 4),
        "palette": len(palette),
        "edge_density": round(edges / max(1, edge_pairs), 4),
    }


def classify_stats(stats: dict) -> dict:
    """Map stats to the live lane's canonical states."""
    b, pal, edge = (stats["mean_brightness"], stats["palette"],
                    stats["edge_density"])
    if edge < 0.004 and pal < 60:
        state = "blank_or_locked"
        note = "uniform field: desktop background or locked/black screen"
    elif b > BRIGHT_MODAL and edge > EDGE_DENSITY_GAME:
        state = "modal_dialog"
        note = "bright busy surface: a light popup over the game"
    elif edge >= EDGE_DENSITY_GAME or pal >= PALETTE_GAME:
        state = "civ_in_game"
        note = "dense varied UI: in-game (or any rich client — see details)"
    else:
        state = "civ_main_menu"
        note = "dark structured UI: menu or client window"
    return {"state": state,
            "details": f"{note} (brightness={b} palette={pal} edges={edge})",
            "blocking": state in ("modal_dialog", "blank_or_locked"),
            "recommended_action": ("dismiss_modal" if state == "modal_dialog"
                                   else "none")}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", nargs="?", default="classify",
                    choices=("classify", "watch"))
    ap.add_argument("--image", type=Path, default=None)
    ap.add_argument("--display", default=DISPLAY)
    ap.add_argument("--interval", type=float, default=15.0,
                    help="watch: seconds between the two frames")
    ap.add_argument("--region", default=None,
                    help="scrot geometry WxH+X+Y or X,Y,W,H")
    opts = ap.parse_args()

    if opts.mode == "watch":
        import time

        a = capture(opts.display, opts.region)
        time.sleep(opts.interval)
        b = capture(opts.display, opts.region)
        frozen = a == b
        verdict = {"frozen": frozen,
                   "frames_identical_bytes": frozen,
                   "stats_a": pixel_stats(a), "stats_b": pixel_stats(b),
                   "state": classify_stats(pixel_stats(b))["state"]}
        verdict["details"] = ("screen unchanged over the interval — engine "
                              "wedged or waiting on input"
                              if frozen else
                              "screen changed — something is animating")
        print(json.dumps(verdict, sort_keys=True))
        return 1 if frozen else 0

    png = opts.image.read_bytes() if opts.image else capture(opts.display)
    stats = pixel_stats(png)
    verdict = classify_stats(stats)
    verdict["stats"] = stats
    print(json.dumps(verdict, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
