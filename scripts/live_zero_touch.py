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
import os
import re
import shutil
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path

from civ_arena.game.civ6 import ui_control

REPO = Path(__file__).resolve().parents[1]
DISPLAY = ":1"
STEAM_URI = "steam://rungameid/289070"
SAVES = Path("/home/alexk/.local/share/aspyr-media/"
             "Sid Meier's Civilization VI/Saves")

# Timing lessons from the 2026-08-31 runs, all live-learned:
COLD_BOOT_S = 600        # first launch after Steam start: ~8-10 min
WARM_BOOT_S = 420        # relaunch: states register in ~4-7 min
INTRO_SETTLE_S = 300     # host -> BEGIN GAME clickable (varies 80-300s)
TUNER_COOLDOWN_S = 8
MAP_LOAD_S = 300         # BEGIN GAME -> GameCore_Tuner


async def bounce_x() -> bool:
    """M17e/A1: the tuner binds 4318 only on a YOUNG X server (the menu
    bind is gone by ~35 min). Killing the gaming session's xinit tree
    makes its supervisor respawn a fresh one in seconds."""
    out = (await run(["pgrep", "-f", "xinit.*headless-gaming-session"])).stdout
    pids = [int(x) for x in out.split()]
    if not pids:
        print("[fresh-x] no gaming xinit found — nothing to bounce")
        return False
    for pid in pids:
        await run(["kill", "-TERM", str(pid)])
    for _ in range(24):            # respawn within ~2 min
        await asyncio.sleep(5)
        out = (await run(["pgrep", "-f",
                   "xinit.*headless-gaming-session"])).stdout
        new = [int(x) for x in out.split()]
        if any(p not in pids for p in new):
            fresh = next(p for p in new if p not in pids)
            print(f"[fresh-x] respawned xinit {fresh}")
            await asyncio.sleep(10)         # openbox/sunshine/steam settle
            return True
    print("[fresh-x] session never respawned")
    return False


async def run(cmd: list[str], *, timeout=240, **kw) -> subprocess.CompletedProcess:
    proc = await asyncio.create_subprocess_exec(
        *cmd, cwd=REPO, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        start_new_session=True, **kw)
    try:
        async with asyncio.timeout(timeout):
            out, err = await proc.communicate()
        return subprocess.CompletedProcess(cmd, proc.returncode, out.decode(), err.decode())
    finally:
        if proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(proc.pid, signal.SIGKILL)
            await proc.communicate()


async def civ6_pids() -> list[int]:
    out = (await run(["pgrep", "-f", r"Civilization VI/./Civ6"])).stdout
    return [int(x) for x in out.split()]


async def require_active_display(artifacts: Path | None = None) -> None:
    """An X server without an active output cannot create Steam windows.

    In particular, a persisted local gaming mode can survive unplugging the
    monitor. Merely finding xinit/Steam processes does not establish readiness.
    This probe sends no desktop input and never changes the host display mode.
    """
    try:
        result = await run(["xrandr", "--display", DISPLAY, "--query"], timeout=10)
        active = re.findall(
            r"^(\S+) connected(?: primary)? [1-9]\d*x[1-9]\d*[+-]\d+[+-]\d+\b",
            result.stdout, re.MULTILINE)
        diagnostic = dict(display=DISPLAY, returncode=result.returncode,
                          active_outputs=active,
                          stdout=ui_control.redact(result.stdout),
                          stderr=ui_control.redact(result.stderr))
    except (OSError, TimeoutError) as exc:
        diagnostic = dict(display=DISPLAY, returncode=None, active_outputs=[],
                          error=ui_control.redact(f"{type(exc).__name__}: {exc}"))
    if artifacts is not None:
        path = artifacts / f"display-preflight-{uuid.uuid4().hex}.json"
        path.write_text(json.dumps(diagnostic, sort_keys=True) + "\n")
    if diagnostic["returncode"] != 0 or not diagnostic["active_outputs"]:
        raise RuntimeError(
            f"display preflight failed: {DISPLAY} has no verified active output; "
            "restore the gaming session display mode before launching Steam/Civ6")
    print(f"[display] {DISPLAY} active outputs: {', '.join(diagnostic['active_outputs'])}")


async def kill_game() -> None:
    for _ in range(3):
        pids = await civ6_pids()
        if not pids:
            break
        await run(["pkill", "-f", r"Civilization VI/./Civ6"])
        await asyncio.sleep(3)
    await run(["pkill", "-f", "SteamLaunch AppId=289070"])
    await asyncio.sleep(3)
    print(f"[kill] game clear: {await civ6_pids() or 'none'}")


def launch(artifacts: Path | None = None) -> None:
    path = (artifacts or Path("/tmp")) / f"civ6-launch-{uuid.uuid4().hex}.log"
    with path.open("x") as log:
        subprocess.Popen(
            ["/usr/games/steam", STEAM_URI], env={**os.environ, "DISPLAY": DISPLAY},
            stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True)
    print(f"[launch] URI handoff fired; log={path}", flush=True)


async def port_up() -> bool:
    """The tuner binds 4318 — and ONLY 4318. 4319 is NOT a tuner fallback
    (the old docstring's game-five theory is wrong, M18-live disproven):
    it is the EOS/net service, up from boot, and sending it FireTuner
    handshakes correlates with game-process death within ~2 min (twice
    reproduced). Accepting 4319 here let boot gates pass on EOS alone."""
    out = (await run(["ss", "-tln"])).stdout
    return "127.0.0.1:4318" in out


def tuner_port() -> int:
    return 4318


def capture_window() -> bytes:
    return ui_control.capture(ui_control.select_window(DISPLAY))


def find_teal_banner(png: bytes):
    return ui_control.find_teal_banner(png)


def dump_screen(label: str) -> None:
    try:
        path = Path(f"/tmp/arch1-{label}-{time.time_ns()}.png")
        path.write_bytes(capture_window())
        print(f"[screen] {path}")
    except Exception as exc:
        print(f"[screen] failed: {ui_control.redact(str(exc))}")


async def click(fx: float, fy: float) -> None:
    result = await ui_control.Controller(DISPLAY).action(at=(fx, fy))
    print("[ui]", result)
    if result.status != "sent":
        raise RuntimeError(f"required click: {result}")


async def click_banner() -> None:
    result = await ui_control.Controller(DISPLAY).action(banner=True)
    print("[ui]", result)
    if result.status == "failed":
        raise RuntimeError(f"banner helper: {result}")


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


async def key(k: str) -> None:
    result = await ui_control.Controller(DISPLAY).action(key=k)
    print("[ui]", result)
    if result.status != "sent":
        raise RuntimeError(f"required key: {result}")


async def phase(args: list[str], settle: float = 0.0) -> subprocess.CompletedProcess:
    """Run a live-lane phase script with the single-client cooldown
    discipline baked in (the tuner refuses rapid reconnects)."""
    await asyncio.sleep(8)
    r = await run([sys.executable, str(REPO / "scripts" / args[0]), *args[1:]])
    out = (r.stdout.strip() or r.stderr.strip())
    print(f"[phase {args[0]} {' '.join(args[1:])}] rc={r.returncode}")
    for ln in out.splitlines():
        print("   ", ln)
    if settle:
        await asyncio.sleep(settle)
    return r


def swap_save_into_load_slot(backup_dir: Path) -> bool:
    """A1: the LoadGame params only reliably resolve
    Saves/Single/auto/AutoSave_0001 — copy the hotseat quicksave there
    (backing up whatever occupied the slot)."""
    src = SAVES / "Hotseat" / "quick" / "quicksave.Civ6Save"
    dst = SAVES / "Single" / "auto" / "AutoSave_0001.Civ6Save"
    if not src.exists():
        print(f"[swap] missing {src}")
        return False
    if dst.exists():
        backup_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(dst, backup_dir / dst.name)
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)
    print(f"[swap] {src.name} -> {dst}")
    return True


async def run_arch1_session(opts) -> int:
    """The Architecture-1 session (A1-proven 2026-09-03), stop at the first
    failed gate. Exit codes continue the ladder's scheme from 20."""
    await require_active_display(opts.artifacts)
    if opts.fresh_x and not await bounce_x():
        return 20
    if opts.fresh_x:
        await require_active_display(opts.artifacts)
    await kill_game()
    launch(opts.artifacts)
    if not await wait_for(port_up, 240, "tuner-bind", 10.0):
        return 21

    async def menu_up() -> bool:
        return "StagingRoom" in await tuner_states()

    if not await wait_for(menu_up, COLD_BOOT_S + 300, "menu"):
        return 22
    await key("Escape")               # skip the intro movie if it is still up
    await asyncio.sleep(8)

    async def ingame_up() -> bool:
        return "GameCore_Tuner" in await tuner_states()

    # 1. hotseat create, EMPTY passwords (the launch's transition poll is
    #    expected to fail — rc 11 — the config+host have applied by then).
    #    Codex r1 P2-8: the password gate demands the EXACT empty rows —
    #    "P1PW|arena" or "P1PW|nil" must refuse (Return auto-OK needs "")
    r = await phase(["live_hotseat_launch.py", "--full", "--empty",
               "--port", str(tuner_port())])
    if "InSession|true" not in r.stdout \
            or "\nP0PW|\n" not in f"\n{r.stdout}\n" \
            or "\nP1PW|\n" not in f"\n{r.stdout}\n":
        return 23
    # 2. enter: ReadyButton ORB, then the leader-intro banner + hotseat
    #    hand-off panels. The tuner is DEAD inside this game (the hotseat
    #    session killed it) — the reliable "turn 1 is live" signal is the
    #    engine writing the hotseat autosave.
    await click(0.50, 0.888)
    session_start = time.time()
    autosave = SAVES / "Hotseat" / "auto" / "AutoSave_0001.Civ6Save"

    def turn1_autosaved() -> bool:
        try:
            return autosave.exists() \
                and autosave.stat().st_mtime > session_start
        except OSError:
            return False

    clicked_banner = 0
    for _ in range(24):         # ~8 min of banner/panel alternation
        await asyncio.sleep(20)
        if turn1_autosaved():
            print("[enter] turn-1 autosave landed — game is in")
            break
        try:
            banner = await asyncio.to_thread(lambda: find_teal_banner(capture_window()))
        except Exception:
            banner = None
        if banner and clicked_banner < 3:
            await click_banner()
            clicked_banner += 1
            print(f"[enter] banner click #{clicked_banner} "
                  f"({banner[0]:.3f}, {banner[1]:.3f})")
        else:
            await key("Return")       # empty-password hand-off panel auto-OK
    if not turn1_autosaved():
        print("[enter] turn-1 autosave never appeared")
        dump_screen("no-autosave")
        return 24
    # 3. UI quicksave at turn 1 (the tuner is dead inside the hotseat
    #    session's game by design — the save must go through the menu).
    #    Codex r1 P1-4: a stale quicksave from an earlier run must NOT
    #    pass as evidence — the gate is the file's mtime moving past the
    #    session start, exactly like the turn-1 autosave gate.
    quicksave = SAVES / "Hotseat" / "quick" / "quicksave.Civ6Save"
    await key("Escape")
    await asyncio.sleep(4)
    await click(0.50, 0.383)
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
        dump_screen("no-quicksave")
        return 25
    if not swap_save_into_load_slot(opts.artifacts / "load-slot-backup"):
        return 26
    # 4. fresh process, then the load with the load-menu screen OPEN
    await kill_game()
    launch(opts.artifacts)
    if not await wait_for(port_up, 240, "tuner-bind-2", 10.0):
        return 27
    if not await wait_for(menu_up, COLD_BOOT_S + 300, "menu-2"):
        return 28
    await key("Escape")
    await asyncio.sleep(8)
    await click(0.459, 0.404)         # Single Player
    await asyncio.sleep(5)
    await click(0.57, 0.55)           # Load Game
    await asyncio.sleep(5)
    r = await phase(["live_hotseat_launch.py", "--load",
               "--port", str(tuner_port())], settle=25)
    if "loadgame-returned|true" not in r.stdout:
        dump_screen("load-failed")
        return 29
    await asyncio.sleep(15)     # settle: the map begins forming before
    # the intro panel is drawn — poll only after the engine settles.
    # The loaded game opens on the civ-intro screen (attempt 6: ribbon
    # at window (0.22, 0.935), pixel-located); the tuner binds only at
    # the map transition after CONTINUE GAME — click the banner through
    # (RETRY: one water-diluted click missed it live), then wait GameCore
    intro_clicks = 0
    for _ in range(20):
        if await ingame_up():
            break
        try:
            banner = await asyncio.to_thread(lambda: find_teal_banner(capture_window()))
        except Exception:
            banner = None
        if banner and intro_clicks < 3:
            await click_banner()
            intro_clicks += 1
            print(f"[intro-2] click #{intro_clicks} "
                  f"({banner[0]:.3f}, {banner[1]:.3f})")
        else:
            await key("Return")
        await asyncio.sleep(15)
    # attempt-7 lesson: this boot's tuner registered ~15-17 min after the
    # load (600s missed it by <=2 min) — "binds late or never" skews LATE
    if not await wait_for(ingame_up, MAP_LOAD_S * 4, "ingame-2", 15.0):
        dump_screen("ingame-2")
        return 30
    # 5. census the demote, re-flag, gate the flip. Codex r1 P2-9: the
    #    gate requires BOTH the re-flag's own read-back (slot + cfg-human)
    #    and the GameCore census row — either alone can lie.
    # census-1 needs the tuner reconnect cooldown too (the pivot run
    # died here: the census ran right after the ingame-2 poll's
    # connection closed and the single-client refusal returned EMPTY)
    await asyncio.sleep(8)
    r = await run([sys.executable, str(REPO / "scripts" / "live_seat_check.py"),
             "--port", str(tuner_port())])
    print("[census-1]", r.stdout.strip())
    r = await phase(["live_hotseat_launch.py", "--reflag",
               "--port", str(tuner_port())])
    if "REFLAG_SLOT|1|3" not in r.stdout \
            or "REFLAG_CFGHUMAN|1|true" not in r.stdout:
        print("[gate] re-flag read-back mismatch — refusing to dispatch")
        dump_screen("reflag-gate")
        return 31
    await asyncio.sleep(5)
    r = await run([sys.executable, str(REPO / "scripts" / "live_seat_check.py"),
             "--port", str(tuner_port())])
    print("[census-2]", r.stdout.strip())
    if r.returncode or not all(any(ln.startswith(f"P{pid}|human=true|")
                                      and "|slot=3|" in ln
                                      for ln in r.stdout.splitlines()) for pid in (0, 1)):
        print("[gate] census did not confirm both human seats slot=3 — refusing")
        dump_screen("census-gate")
        return 31
    if not opts.no_smoke:
        smoke = await run([sys.executable,
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
    return 0


async def dispatch(opts) -> int:
    args = [sys.executable, "-m", "civ_arena.game.civ6.live_driver",
            str(REPO / opts.config), "--phase", "dispatch-hotseat",
            "--turns", str(opts.rounds), "--port", str(tuner_port()),
            "--run-id", opts.run_id, "--runs-root", str(opts.runs_root)]
    startup_budget = getattr(opts, "driver_startup_timeout", opts.startup_timeout)
    for flag, value in (("startup-timeout", startup_budget),
                        ("match-timeout", opts.match_timeout),
                        ("agent-turn-timeout", opts.agent_turn_timeout),
                        ("recovery-timeout", opts.recovery_timeout),
                        ("recovery-sweeps", opts.recovery_sweeps)):
        args += ["--" + flag, str(value)]
    print("[dispatch]", " ".join(args), flush=True)
    # Keep complete driver output in its own supporting log. The driver handles
    # its deadlines and SIGTERM; launcher cancellation gives it 20s to seal.
    with (opts.artifacts / "dispatch.log").open("w") as log:
        proc = await asyncio.create_subprocess_exec(*args, cwd=REPO, stdout=log, stderr=log)
        try:
            return await proc.wait()
        finally:
            if proc.returncode is None:
                proc.terminate()
                try:
                    await asyncio.wait_for(proc.wait(), 22)
                except TimeoutError:
                    proc.kill()
                    await proc.wait()
                    print("[dispatch] hard kill: run INCOMPLETE")


async def controlled_arch1(opts) -> int:
    from civ_arena.arena.events import EventLog

    opts.run_id = opts.run_id or f"arch1-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}"
    # Allocate exclusively BEFORE any game input or save mutation.
    opts.artifacts = opts.runs_root / (opts.run_id + "-startup")
    opts.artifacts.mkdir(parents=True, exist_ok=False)
    if (opts.runs_root / opts.run_id).exists():
        raise RuntimeError("dispatch run id already exists")
    log = EventLog(opts.artifacts / "events.jsonl")
    namespace = dict(match_id=opts.run_id + "-startup", game_instance_id=opts.run_id,
                     turn=0, phase_player_id=-1, player_id=None, agent_id=None,
                     visibility_scope="referee")
    log.write("MATCH_START", **namespace, startup_timeout_s=opts.startup_timeout)
    started = time.monotonic()
    code, failure = 2, None
    try:
        async with asyncio.timeout(opts.startup_timeout):
            # Copy the entire existing save inventory before the fresh game
            # can rotate autosaves or overwrite quicksave. Never remove it.
            if SAVES.exists():
                await asyncio.to_thread(shutil.copytree, SAVES, opts.artifacts / "saves-before")
            code = await run_arch1_session(opts)
            if code:
                failure = f"startup gate failed: exit {code}"
            elif opts.config:
                await asyncio.sleep(TUNER_COOLDOWN_S)
                opts.driver_startup_timeout = opts.startup_timeout - (time.monotonic() - started)
                if opts.driver_startup_timeout <= 0:
                    raise TimeoutError("startup budget exhausted before driver setup")
    except (Exception, asyncio.CancelledError, KeyboardInterrupt) as exc:
        failure = ui_control.redact(f"{type(exc).__name__}: {exc}")
    finally:
        summary = dict(clean=code == 0 and failure is None, aborted=failure,
                       phase="arch1-startup", elapsed_s=time.monotonic() - started,
                       startup_timeout_s=opts.startup_timeout,
                       driver_startup_timeout_s=getattr(opts, "driver_startup_timeout", None),
                       final_observation="unavailable; game preserved",
                       cleanup="helper processes stopped; game preserved")
        log.write("MATCH_END", **namespace, summary=summary)
        (opts.artifacts / "summary.json").write_text(json.dumps(summary, sort_keys=True))
        log.close()
    if not summary["clean"]:
        print("[startup] stopped and preserved:", failure, flush=True)
        return code or 2
    if opts.config:
        return await dispatch(opts)
    return 0


async def main(opts) -> int:
    if getattr(opts, "session", None) == "arch1":
        return await controlled_arch1(opts)
    await require_active_display()
    if opts.fresh_x and not await bounce_x():
        return 20
    if opts.fresh_x:
        await require_active_display()
    if opts.kill_first:
        await kill_game()
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
    cfg = await run([sys.executable, str(REPO / "scripts" / "live_newgame.py"),
               "config"])
    print("[config]", cfg.stdout.strip() or cfg.stderr.strip())
    if cfg.returncode != 0:
        return 3
    if not all(k in cfg.stdout for k in
               ("MapSize|-601637951", "MinMajor|2", "Participating|2")):
        print("[config] read-back mismatch — refusing to host")
        return 3
    host = await run([sys.executable, str(REPO / "scripts" / "live_newgame.py"),
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
        await click_banner()
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
        smoke = await run([sys.executable,
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
    ap.add_argument("--startup-timeout", type=float, default=2700)
    ap.add_argument("--match-timeout", type=float, default=7200)
    ap.add_argument("--agent-turn-timeout", type=float, default=600)
    ap.add_argument("--recovery-timeout", type=float, default=180)
    ap.add_argument("--recovery-sweeps", type=int, default=8)
    ap.add_argument("--runs-root", type=Path, default=REPO / "runs")
    ap.set_defaults(kill_first=True)
    async def entry():
        loop = asyncio.get_running_loop()
        task = asyncio.current_task()
        loop.add_signal_handler(signal.SIGTERM, task.cancel)
        return await main(ap.parse_args())
    sys.exit(asyncio.run(entry()))
