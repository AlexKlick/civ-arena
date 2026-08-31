"""M17b — the zero-touch ladder, one command.

Chains every proven rung (2026-08-31 session) into a single sequencer:
kill any stale game -> URI launch through the resident Steam client ->
wait out the front-end boot -> duel config in StagingRoom (read-back
verified) -> Network.HostGame (auto-launches in SINGLEPLAYER) -> click
the leader intro's BEGIN GAME (located by PIXEL COLOR, never by VL) ->
wait for GameCore_Tuner -> optionally run the live smoke.

    uv run python scripts/live_zero_touch.py            # full ladder
    uv run python scripts/live_zero_touch.py --from-menu
                                                        # game already at menu
    uv run python scripts/live_zero_touch.py --smoke    # + firetuner smoke
"""

from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
DISPLAY = ":1"
STEAM_URI = "steam://rungameid/289070"

# Timing lessons from the 2026-08-31 runs, all live-learned:
COLD_BOOT_S = 600        # first launch after Steam start: ~8-10 min
WARM_BOOT_S = 420        # relaunch: states register in ~4-7 min
INTRO_SETTLE_S = 150     # host -> BEGIN GAME clickable
MAP_LOAD_S = 300         # BEGIN GAME -> GameCore_Tuner


def run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=REPO, capture_output=True, text=True, **kw)


def civ6_pids() -> list[int]:
    out = run(["pgrep", "-f", r"Civilization VI/./Civ6"]).stdout
    return [int(x) for x in out.split()]


def kill_game() -> None:
    for _ in range(3):
        pids = civ6_pids()
        if not pids:
            break
        run(["pkill", "-f", r"Civilization VI/./Civ6"])
        time.sleep(3)
    run(["pkill", "-f", "SteamLaunch AppId=289070"])
    time.sleep(3)
    print(f"[kill] game clear: {civ6_pids() or 'none'}")


def launch() -> None:
    subprocess.Popen(
        ["bash", "-c",
         f"HOME=/home/alexk DISPLAY={DISPLAY} setsid nohup "
         f"/usr/games/steam {STEAM_URI} </dev/null >/tmp/civ6-zero.log 2>&1 &"])
    print("[launch] URI handoff fired")


def port_up() -> bool:
    return ":4318" in run(["ss", "-tln"]).stdout


def civ6_window_geometry() -> tuple[int, int, int, int]:
    """Absolute x, y, w, h of the Civ6 window on :1."""
    out = run(["bash", "-c",
               "xprop -root _NET_CLIENT_LIST | grep -o '0x[0-9a-f]*' "
               "| tail -1"]).stdout.strip()
    info = run(["xwininfo", "-id", out]).stdout
    geo: dict[str, int] = {}
    for line in info.splitlines():
        for key in ("Absolute upper-left X", "Absolute upper-left Y",
                    "Width", "Height"):
            if line.strip().startswith(key):
                geo[key] = int(line.split(":")[1])
    return (geo["Absolute upper-left X"], geo["Absolute upper-left Y"],
            geo["Width"], geo["Height"])


def find_teal_banner(png: bytes) -> tuple[float, float] | None:
    """The BEGIN GAME banner by pixel color (teal: blue+green high, red
    low). VL said 'bottom center'; the banner is at x~0.28 — locate UI by
    pixels, not by a vision model's guess."""
    sys.path.insert(0, str(REPO / "scripts"))
    from screen_triage import _decode_rgb
    w, h, rows = _decode_rgb(png)
    hits = []
    for y in range(int(h * 0.75), h):
        for x in range(0, w, 2):
            r, g, b = rows[y][x]
            if b > 120 and g > 110 and r < 90 and (b - r) > 60:
                hits.append((x, y))
    if not hits:
        return None
    xs = [p[0] for p in hits]
    ys = [p[1] for p in hits]
    return ((min(xs) + max(xs)) / 2 / w, (min(ys) + max(ys)) / 2 / h)


def capture_window() -> bytes:
    import tempfile
    wx, wy, ww, wh = civ6_window_geometry()
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as fh:
        path = fh.name
    run(["bash", "-c",
         f"DISPLAY={DISPLAY} scrot -a {wx},{wy},{ww},{wh} -o {path}"])
    data = Path(path).read_bytes()
    Path(path).unlink(missing_ok=True)
    return data


def click(fx: float, fy: float) -> None:
    run([sys.executable, str(REPO / "scripts" / "x_click.py"),
         "--at", f"{fx:.4f},{fy:.4f}"])


async def tuner_states() -> list[str]:
    from civ_arena.game.civ6.vendor import tuner_client
    try:
        r, w = await tuner_client.connect("127.0.0.1", 4318, timeout=4)
    except Exception:
        return []
    try:
        _, states = await tuner_client.handshake(r, w)
        return [s for s in states if s and not s.isdigit()]
    finally:
        w.close()


async def wait_for(check, budget_s: int, label: str,
                    interval_s: float = 20.0) -> bool:
    deadline = time.monotonic() + budget_s
    while time.monotonic() < deadline:
        res = check()
        if __import__("inspect").isawaitable(res):
            res = await res
        if res:
            print(f"[{label}] up at {time.strftime('%H:%M:%S')}")
            return True
        await asyncio.sleep(interval_s)
    print(f"[{label}] TIMEOUT after {budget_s}s")
    return False


async def main(opts) -> int:
    if opts.kill_first:
        kill_game()
        launch()
        if not await wait_for(port_up, 120, "tuner-bind", 10.0):
            return 1

    async def menu_up() -> bool:
        return "StagingRoom" in await tuner_states()

    if not await wait_for(menu_up,
                          COLD_BOOT_S if opts.kill_first else WARM_BOOT_S,
                          "menu"):
        return 2
    # configure-then-host (order proven live): values stick through hosting
    cfg = run([sys.executable, str(REPO / "scripts" / "live_newgame.py"),
               "config"])
    print("[config]", cfg.stdout.strip() or cfg.stderr.strip())
    if cfg.returncode != 0:
        return 3
    if not all(f"{k}|" in cfg.stdout for k in
               ("MapSize|388991850", "MinMajor|2", "Participating|2")):
        print("[config] read-back mismatch — refusing to host")
        return 3
    host = run([sys.executable, str(REPO / "scripts" / "live_newgame.py"),
                "host"])
    print("[host]", host.stdout.strip() or host.stderr.strip())
    if host.returncode != 0:
        return 4
    # the leader intro: wait for the banner, click it (pixel-located)
    async def intro_ready() -> bool:
        try:
            return find_teal_banner(capture_window()) is not None
        except Exception:
            return False
    if not await wait_for(intro_ready, INTRO_SETTLE_S, "intro", 15.0):
        return 5
    for attempt in range(3):
        fx, fy = find_teal_banner(capture_window())  # type: ignore[misc]
        click(fx, fy)
        print(f"[begin] clicked ({fx:.3f}, {fy:.3f}) attempt {attempt + 1}")
        await asyncio.sleep(12)
        try:
            if find_teal_banner(capture_window()) is None:
                print("[begin] banner gone — game loading")
                break
        except Exception:
            break
    async def ingame_up() -> bool:
        return "GameCore_Tuner" in await tuner_states()

    if not await wait_for(ingame_up, MAP_LOAD_S, "ingame"):
        return 6
    if opts.smoke:
        smoke = run([sys.executable,
                     str(REPO / "scripts" / "firetuner_smoke.py"), "--live"])
        print("[smoke]", smoke.stdout.strip().splitlines()[-1] if
              smoke.stdout.strip() else smoke.stderr.strip())
        if smoke.returncode != 0:
            return 7
    print(json.dumps({"ok": True, "note": "game on the map, ready to drive"},
                     sort_keys=True))
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--kill-first", action="store_true",
                    help="kill any running game and relaunch via URI")
    ap.add_argument("--from-menu", dest="kill_first", action="store_false",
                    help="the game is already at the main menu")
    ap.add_argument("--smoke", action="store_true",
                    help="run the live FireTuner smoke at the end")
    ap.set_defaults(kill_first=True)
    sys.exit(asyncio.run(main(ap.parse_args())))
