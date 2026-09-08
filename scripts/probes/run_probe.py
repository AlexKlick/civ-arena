"""M4 step 0 runner: send the read-only spectator accessor probe to a live tuner.

Sends ``scripts/probes/spectator_accessors.lua`` (default) to a parked game
and streams the ``PROBE|...`` rows to stdout and to ``--out``.

Context / transport (mirrors ``scripts/live_seat_check.py`` and the adapter's
M14d routing in ``src/civ_arena/game/civ6/firetuner.py`` — every builder's
ingame flag chooses the transport):

- ``--context gamecore`` → ``GameConnection.execute_read`` — the
  GameCore_Tuner VM. The same transport every state read uses
  (seat census, OVERVIEW/UNITS reads). The probe payload only reads.
- ``--context ingame`` → ``GameConnection.execute_write`` — the InGame VM.
  This is a TRANSPORT choice, not a mutation: the repo's own InGame READS
  (``cities_read``, ``current_production_read``) ride ``execute_write``
  because CityManager/UI live in that VM. The payload is the identical
  read-only probe Lua — no Request*, no Set*, no Broadcast, no UI action —
  so neither variant mutates the game in any way.

Single-tuner-client rule (docs/live-validation.md §5: "One tuner connection
at a time"): the runner refuses to start when any ESTABLISHED TCP connection
already touches the port (checked BEFORE dialing, via ``ss`` with a
/proc/net/tcp fallback), so it can never steal a second client slot from a
running driver or FireTuner GUI.

A final ``PROBE_WALL|<ms>`` row (stdout and log) records the wall time of
the whole exchange (dial → sentinel). Exits nonzero on transport error,
when ``PROBE_END|<n>`` never arrives, or when the declared row count does
not match the received PROBE rows.

    PYTHONPATH=src:tests python scripts/probes/run_probe.py --context gamecore
    PYTHONPATH=src:tests python scripts/probes/run_probe.py --context ingame
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import re
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "src"))

from civ_arena.game.civ6.vendor.connection import GameConnection, LuaError  # noqa: E402

_PROBE_END = re.compile(r"^PROBE_END\|([0-9]+)$")


def _established_on_port(port: int) -> list[str]:
    """ESTABLISHED connections already touching the tuner port.

    The game LISTENS on the port (expected); any ESTABLISHED connection
    before we dial means a client is already attached — the tuner serves a
    single client, so the runner must refuse instead of racing it.
    ``ss -tnH state established`` first (the host-evidence pattern from
    docs/live-validation.md), /proc/net/tcp + tcp6 as the dependency-free
    fallback (state 01 = ESTABLISHED, ports in hex).
    """
    lines: list[str] = []
    try:
        proc = subprocess.run(
            ["ss", "-tnH", "state", "established"],
            capture_output=True, text=True, timeout=5.0, check=False,
        )
        if proc.returncode == 0:
            for ln in proc.stdout.splitlines():
                fields = ln.split()
                if len(fields) >= 4 and any(
                    fields[i].rpartition(":")[2].isdigit()
                    and int(fields[i].rpartition(":")[2]) == port
                    for i in (2, 3)
                ):
                    lines.append(ln)
            return lines
    except (OSError, subprocess.TimeoutExpired):
        pass
    for path in ("/proc/net/tcp", "/proc/net/tcp6"):
        try:
            raw = Path(path).read_text(encoding="ascii").splitlines()[1:]
        except (OSError, UnicodeDecodeError):
            continue
        for ln in raw:
            fields = ln.split()
            if len(fields) < 4 or fields[3] != "01":
                continue
            if any(int(a.rsplit(":", 1)[1], 16) == port for a in fields[1:3]):
                lines.append(f"{path}: {fields[1]} -> {fields[2]}")
    return lines


async def run(opts: argparse.Namespace) -> int:
    busy = _established_on_port(opts.port)
    if busy:
        for ln in busy:
            print(f"PORT BUSY: {ln}", file=sys.stderr)
        print(
            f"refusing: {len(busy)} established connection(s) already on port "
            f"{opts.port} — the tuner serves a single client",
            file=sys.stderr,
        )
        return 3

    lua = opts.lua.read_text(encoding="utf-8")
    opts.out.parent.mkdir(parents=True, exist_ok=True)

    conn = GameConnection(opts.host, opts.port)
    started = time.perf_counter()
    transport_error: str | None = None
    lines: list[str] = []
    try:
        await conn.connect()
        if opts.context == "gamecore":
            # Same transport as every GameCore state read.
            lines = await conn.execute_read(lua, timeout=opts.timeout)
        else:
            # execute_write is the InGame TRANSPORT; the payload is read-only.
            lines = await conn.execute_write(lua, timeout=opts.timeout)
    except (LuaError, OSError) as exc:
        transport_error = f"{type(exc).__name__}: {exc}"
    finally:
        with contextlib.suppress(OSError):
            await conn.disconnect()
    wall_ms = int((time.perf_counter() - started) * 1000)

    probe_rows = 0
    with opts.out.open("w", encoding="utf-8") as fh:
        for ln in lines:
            print(ln)
            fh.write(ln + "\n")
            if ln.startswith("PROBE|"):
                probe_rows += 1
        fh.write(f"PROBE_WALL|{wall_ms}\n")
    print(f"PROBE_WALL|{wall_ms}")

    if transport_error is not None:
        print(f"TRANSPORT ERROR: {transport_error}", file=sys.stderr)
        return 4
    end_rows = [ln for ln in lines if _PROBE_END.match(ln)]
    if not end_rows:
        print("PROBE_END never arrived", file=sys.stderr)
        return 5
    declared = int(end_rows[-1].split("|", 1)[1])
    if declared != probe_rows:
        print(
            f"row count mismatch: PROBE_END declares {declared}, "
            f"received {probe_rows}",
            file=sys.stderr,
        )
        return 6
    print(f"-- {probe_rows} PROBE rows, wall {wall_ms} ms, log {opts.out}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=4318)
    ap.add_argument("--context", choices=("gamecore", "ingame"),
                    default="gamecore")
    ap.add_argument("--lua", type=Path,
                    default=REPO / "scripts" / "probes" / "spectator_accessors.lua")
    ap.add_argument("--out", type=Path, default=None,
                    help="output log (default runs/spectator-capture-probe-"
                         "<utc-ts>/probe-<context>.log)")
    ap.add_argument("--timeout", type=float, default=30.0)
    opts = ap.parse_args()
    if opts.out is None:
        ts = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        opts.out = (REPO / "runs" / f"spectator-capture-probe-{ts}"
                    / f"probe-{opts.context}.log")
    return asyncio.run(run(opts))


if __name__ == "__main__":
    sys.exit(main())
