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
    uv run python scripts/live_zero_touch.py --fresh-x  # bounce X first
                                                        # (the tuner only
                                                        # binds on a young
                                                        # X server, ~<35 min)
    uv run python scripts/live_zero_touch.py --session arch1 --fresh-x \
        --config configs/live-hotseat-001.yaml --rounds 3
    # the full Architecture-1 session (A1-proven 2026-09-03): bounce X ->
    # boot -> hotseat create (EMPTY passwords) -> enter -> UI quicksave ->
    # exit -> file-swap -> LoadGame(NONE) -> census -> reflag -> census
    # gate -> smoke -> dispatch-hotseat.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
DISPLAY = ":1"
STEAM_URI = "steam://rungameid/289070"
SAVES = Path("/home/alexk/.local/share/aspyr-media/"
             "Sid Meier's Civilization VI/Saves")

# Timing lessons from the 2026-08-31 runs, all live-learned:
COLD_BOOT_S = 600        # first launch after Steam start: ~8-10 min
WARM_BOOT_S = 420        # relaunch: states register in ~4-7 min
INTRO_SETTLE_S = 300     # host -> BEGIN GAME clickable (varies 80-300s)
MAP_LOAD_S = 300         # BEGIN GAME -> GameCore_Tuner


def bounce_x() -> bool:
    """M17e/A1: the tuner binds 4318 only on a YOUNG X server (the menu
    bind is gone by ~35 min). Killing the gaming session's xinit tree
    makes its supervisor respawn a fresh one in seconds."""
    out = run(["pgrep", "-f", "xinit.*headless-gaming-session"]).stdout
    pids = [int(x) for x in out.split()]
    if not pids:
        print("[fresh-x] no gaming xinit found — nothing to bounce")
        return False
    for pid in pids:
        run(["kill", "-TERM", str(pid)])
    for _ in range(24):            # respawn within ~2 min
        time.sleep(5)
        out = run(["pgrep", "-f",
                   "xinit.*headless-gaming-session"]).stdout
        new = [int(x) for x in out.split()]
        if any(p not in pids for p in new):
            fresh = next(p for p in new if p not in pids)
            print(f"[fresh-x] respawned xinit {fresh}")
            time.sleep(10)         # openbox/sunshine/steam settle
            return True
    print("[fresh-x] session never respawned")
    return False


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
    """The tuner binds 4318 unless it lingers from a dying instance, in
    which case the game silently takes 4319 (live-learned game five).
    Accept either; the actual port is re-resolved before dispatch."""
    out = run(["ss", "-tln"]).stdout
    return ":4318" in out or ":4319" in out


def tuner_port() -> int:
    out = run(["ss", "-tln"]).stdout
    for port in (4318, 4319):
        if f":{port}" in out:
            return port
    return 4318


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


async def tuner_states(port: int | None = None) -> list[str]:
    from civ_arena.game.civ6.vendor import tuner_client
    try:
        r, w = await tuner_client.connect("127.0.0.1",
                                          port or tuner_port(), timeout=4)
    except Exception:
        return []
    try:
        _, states = await tuner_client.handshake(r, w)
        return [s for s in states if s and not s.isdigit()]
    except Exception:
        # dead-tuner handshakes connect-then-drop (the hotseat session
        # kills the listener for the process) — that is "no states"
        return []
    finally:
        with contextlib.suppress(Exception):
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


def key(k: str) -> None:
    run([sys.executable, str(REPO / "scripts" / "x_click.py"), "--key", k])


def phase(args: list[str], settle: float = 0.0) -> subprocess.CompletedProcess:
    """Run a live-lane phase script with the single-client cooldown
    discipline baked in (the tuner refuses rapid reconnects)."""
    time.sleep(8)
    r = run([sys.executable, str(REPO / "scripts" / args[0]), *args[1:]])
    out = (r.stdout.strip() or r.stderr.strip())
    print(f"[phase {args[0]} {' '.join(args[1:])}] rc={r.returncode}")
    for ln in out.splitlines():
        print("   ", ln)
    if settle:
        time.sleep(settle)
    return r


def swap_save_into_load_slot() -> bool:
    """A1: the LoadGame params only reliably resolve
    Saves/Single/auto/AutoSave_0001 — copy the hotseat quicksave there
    (backing up whatever occupied the slot)."""
    src = SAVES / "Hotseat" / "quick" / "quicksave.Civ6Save"
    dst = SAVES / "Single" / "auto" / "AutoSave_0001.Civ6Save"
    if not src.exists():
        print(f"[swap] missing {src}")
        return False
    if dst.exists():
        dst.rename(dst.with_suffix(".Civ6Save.prev.bak"))
    dst.write_bytes(src.read_bytes())
    print(f"[swap] {src.name} -> {dst}")
    return True


async def run_arch1_session(opts) -> int:
    """The Architecture-1 session (A1-proven 2026-09-03), stop at the first
    failed gate. Exit codes continue the ladder's scheme from 20."""
    if opts.fresh_x and not bounce_x():
        return 20
    kill_game()
    launch()
    if not await wait_for(port_up, 240, "tuner-bind", 10.0):
        return 21

    async def menu_up() -> bool:
        return "StagingRoom" in await tuner_states()

    if not await wait_for(menu_up, COLD_BOOT_S, "menu"):
        return 22
    key("Escape")               # skip the intro movie if it is still up
    await asyncio.sleep(8)

    async def ingame_up() -> bool:
        return "GameCore_Tuner" in await tuner_states()

    # 1. hotseat create, EMPTY passwords (the launch's transition poll is
    #    expected to fail — rc 11 — the config+host have applied by then).
    #    Codex r1 P2-8: the password gate demands the EXACT empty rows —
    #    "P1PW|arena" or "P1PW|nil" must refuse (Return auto-OK needs "")
    r = phase(["live_hotseat_launch.py", "--full", "--empty",
               "--port", str(tuner_port())])
    if "InSession|true" not in r.stdout \
            or "\nP0PW|\n" not in f"\n{r.stdout}\n" \
            or "\nP1PW|\n" not in f"\n{r.stdout}\n":
        return 23
    # 2. enter: ReadyButton ORB, then the leader-intro banner + hotseat
    #    hand-off panels. The tuner is DEAD inside this game (the hotseat
    #    session killed it) — the reliable "turn 1 is live" signal is the
    #    engine writing the hotseat autosave.
    click(0.50, 0.888)
    session_start = time.time()
    autosave = SAVES / "Hotseat" / "auto" / "AutoSave_0001.Civ6Save"

    def turn1_autosaved() -> bool:
        try:
            return autosave.exists() \
                and autosave.stat().st_mtime > session_start
        except OSError:
            return False

    clicked_banner = False
    for _ in range(24):         # ~8 min of banner/panel alternation
        await asyncio.sleep(20)
        if turn1_autosaved():
            print("[enter] turn-1 autosave landed — game is in")
            break
        try:
            banner = find_teal_banner(capture_window())
        except Exception:
            banner = None
        if banner and not clicked_banner:
            click(*banner)
            clicked_banner = True
        else:
            key("Return")       # empty-password hand-off panel auto-OK
    if not turn1_autosaved():
        print("[enter] turn-1 autosave never appeared")
        return 24
    # 3. UI quicksave at turn 1 (the tuner is dead inside the hotseat
    #    session's game by design — the save must go through the menu).
    #    Codex r1 P1-4: a stale quicksave from an earlier run must NOT
    #    pass as evidence — the gate is the file's mtime moving past the
    #    session start, exactly like the turn-1 autosave gate.
    quicksave = SAVES / "Hotseat" / "quick" / "quicksave.Civ6Save"
    key("Escape")
    await asyncio.sleep(4)
    click(0.50, 0.383)
    quicksaved = False
    for _ in range(10):
        await asyncio.sleep(3)
        try:
            if quicksave.exists() \
                    and quicksave.stat().st_mtime > session_start:
                quicksaved = True
                break
        except OSError:
            pass
    if not quicksaved:
        print("[quicksave] file did not land (or is stale)")
        return 25
    if not swap_save_into_load_slot():
        return 26
    # 4. fresh process, then the load with the load-menu screen OPEN
    kill_game()
    launch()
    if not await wait_for(port_up, 240, "tuner-bind-2", 10.0):
        return 27
    if not await wait_for(menu_up, COLD_BOOT_S, "menu-2"):
        return 28
    key("Escape")
    await asyncio.sleep(8)
    click(0.459, 0.404)         # Single Player
    await asyncio.sleep(5)
    click(0.57, 0.55)           # Load Game
    await asyncio.sleep(5)
    r = phase(["live_hotseat_launch.py", "--load",
               "--port", str(tuner_port())], settle=25)
    if "loadgame-returned|true" not in r.stdout:
        return 29
    # the loaded game opens on the leader intro; the tuner binds only at
    # the map transition — click the banner through, then wait GameCore
    intro_clicked = False
    for _ in range(20):
        if await ingame_up():
            break
        try:
            banner = find_teal_banner(capture_window())
        except Exception:
            banner = None
        if banner and not intro_clicked:
            click(*banner)
            intro_clicked = True
        else:
            key("Return")
        await asyncio.sleep(15)
    if not await wait_for(ingame_up, MAP_LOAD_S, "ingame-2", 15.0):
        return 30
    # 5. census the demote, re-flag, gate the flip. Codex r1 P2-9: the
    #    gate requires BOTH the re-flag's own read-back (slot + cfg-human)
    #    and the GameCore census row — either alone can lie.
    r = run([sys.executable, str(REPO / "scripts" / "live_seat_check.py"),
             "--port", str(tuner_port())])
    print("[census-1]", r.stdout.strip())
    r = phase(["live_hotseat_launch.py", "--reflag",
               "--port", str(tuner_port())])
    if "REFLAG_SLOT|1|3" not in r.stdout \
            or "REFLAG_CFGHUMAN|1|true" not in r.stdout:
        print("[gate] re-flag read-back mismatch — refusing to dispatch")
        return 31
    time.sleep(5)
    r = run([sys.executable, str(REPO / "scripts" / "live_seat_check.py"),
             "--port", str(tuner_port())])
    print("[census-2]", r.stdout.strip())
    if not any(ln.startswith("P1|human=true|") and "|slot=3|" in ln
               for ln in r.stdout.splitlines()):
        print("[gate] census did not confirm P1 human slot=3 — refusing")
        return 31
    if not opts.no_smoke:
        smoke = run([sys.executable,
                     str(REPO / "scripts" / "firetuner_smoke.py"), "--live",
                     "--port", str(tuner_port())])
        last = (smoke.stdout.strip().splitlines() or ["<none>"])[-1]
        print("[smoke]", last)
        if smoke.returncode != 0:
            return 32
    if not opts.config:
        print(json.dumps({"ok": True,
                          "note": "arch1 session up: both seats human, "
                                  "mod attached, ready to dispatch"},
                         sort_keys=True))
        return 0
    # 6. dispatch both seats
    args = [sys.executable, "-m", "civ_arena.game.civ6.live_driver",
            str(REPO / opts.config), "--phase", "dispatch-hotseat",
            "--turns", str(opts.rounds), "--port", str(tuner_port())]
    if opts.run_id:
        args += ["--run-id", opts.run_id]
    print("[dispatch]", " ".join(args[2:]), flush=True)
    proc = subprocess.run(args, cwd=REPO)
    return proc.returncode


async def main(opts) -> int:
    if getattr(opts, "session", None) == "arch1":
        return await run_arch1_session(opts)
    if opts.fresh_x and not bounce_x():
        return 20
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
    # the tuner refuses rapid reconnects: this poll's own connection must
    # be long closed before live_newgame's subprocess dials in
    await asyncio.sleep(8)
    # configure-then-host (order proven live): values stick through hosting
    cfg = run([sys.executable, str(REPO / "scripts" / "live_newgame.py"),
               "config"])
    print("[config]", cfg.stdout.strip() or cfg.stderr.strip())
    if cfg.returncode != 0:
        return 3
    if not all(k in cfg.stdout for k in
               ("MapSize|-601637951", "MinMajor|2", "Participating|2")):
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
                     str(REPO / "scripts" / "firetuner_smoke.py"), "--live",
                     "--port", str(tuner_port())])
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
    ap.add_argument("--fresh-x", action="store_true",
                    help="bounce the gaming X session first (the tuner "
                         "binds only on a young X server)")
    ap.add_argument("--session", choices=["arch1"], default=None,
                    help="arch1: the full A1-proven both-seats-human "
                         "session, optionally dispatching at the end")
    ap.add_argument("--config", default=None,
                    help="with --session arch1: dispatch this config "
                         "(skip dispatch when omitted)")
    ap.add_argument("--rounds", type=int, default=3,
                    help="dispatch-hotseat --turns for --session arch1")
    ap.add_argument("--run-id", default=None,
                    help="explicit run id for the dispatch")
    ap.add_argument("--no-smoke", action="store_true",
                    help="skip the smoke stage in --session arch1")
    ap.set_defaults(kill_first=True)
    sys.exit(asyncio.run(main(ap.parse_args())))
