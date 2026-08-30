"""Staged FireTuner smoke — S1..S6, rehearsalable against FakeTunerServer.

Default (fake) mode exercises every stage game-free; ``--live`` probes a
real listener (docs/live-validation.md §2.5). Exit codes are stage-distinct
so a failure names its stage: S1=10 S2=20 S3=30 S4=40 S5=50 S6=60; 0 = all
stages ok (skips allowed). ``--json`` prints one machine-readable doc;
``--live`` also writes a transcript under ``runs/live-preflight-<ts>/``.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from civ_arena.game.adapter import ObserveKind, ObserveRequest
from civ_arena.game.civ6 import lua_translator, response_parser
from civ_arena.game.civ6.fake_tuner_server import FakeMod, FakeTunerServer
from civ_arena.game.civ6.firetuner import FireTunerAdapter

STAGE_EXIT = {"S1": 10, "S2": 20, "S3": 30, "S4": 40, "S5": 50, "S6": 60}


class Smoke:
    """Runs the six stages against one adapter, recording per-stage results.

    Stage methods return ``(status, detail)`` or raise; ``run`` owns all
    timing/recording so a failing stage can never corrupt another's row.
    """

    def __init__(self, adapter: FireTunerAdapter) -> None:
        self.adapter = adapter
        self.results: list[dict[str, Any]] = []
        self._mod_version: str | None = None

    async def s1_connect(self) -> tuple[str, str]:
        await self.adapter.setup({})
        conn_states = dict(self.adapter._conn.lua_states)  # noqa: SLF001
        if "GameCore_Tuner" not in conn_states.values():
            raise RuntimeError(f"no GameCore_Tuner state: {conn_states}")
        return "ok", f"connected; states={conn_states}"

    async def s2_turn_state(self) -> tuple[str, str]:
        phase = await self.adapter.current_phase()
        if not isinstance(phase.get("turn"), int) or phase["turn"] < 0:
            raise RuntimeError(f"turn not parsed: {phase}")
        return "ok", f"turn={phase['turn']}"

    async def s3_overview(self) -> tuple[str, str]:
        overview = await self.adapter.observe(
            ObserveRequest(kind=ObserveKind.OVERVIEW, player_id=0))
        alive = overview.get("ALIVE")
        if not isinstance(alive, int) or alive < 2:
            raise RuntimeError(f"expected >= 2 alive majors, got {alive}")
        return "ok", f"alive_majors={alive}"

    async def s4_mod_handshake(self) -> tuple[str, str]:
        doc = await self.adapter.mod_handshake()
        self._mod_version = doc["mod_version"]
        if not doc["supports_freeze"] or not doc["supports_ledger"]:
            raise RuntimeError(f"mod gate failed: {doc}")
        return "ok", f"mod={doc['mod_version']} freeze+ledger ok"

    async def s5_mod_digest(self) -> tuple[str, str]:
        lines = await self.adapter.read_raw(lua_translator.mod_digest())
        digest = next((ln for ln in lines if ln.startswith("DIGEST|")), None)
        if digest is None:
            raise RuntimeError(f"no DIGEST| row: {lines}")
        return "ok", f"digest_len={len(digest)}"

    async def s6_mod_status(self) -> tuple[str, str]:
        if self._mod_version is not None and self._mod_version < "0.2":
            return "skip", f"mod_version {self._mod_version} < 0.2 (no Status)"
        lines = await self.adapter.read_raw(lua_translator.mod_status())
        if lines and lines[0].startswith("MOD_STATUS|unavailable"):
            raise RuntimeError("mod reports no pollable Status (need >= 0.2)")
        parsed = response_parser.parse_kv_lines(lines)
        if "TURN" not in parsed or "PUPPET_ACTIVE" not in parsed:
            raise RuntimeError(f"status not parsed: {lines}")
        return "ok", (f"turn={parsed['TURN']} "
                      f"puppet_active={parsed['PUPPET_ACTIVE']}")

    async def run(self) -> int:
        stages = (self.s1_connect, self.s2_turn_state, self.s3_overview,
                  self.s4_mod_handshake, self.s5_mod_digest, self.s6_mod_status)
        for i, stage in enumerate(stages):
            name = f"S{i + 1}"
            t0 = time.monotonic()
            try:
                status, detail = await stage()
            except Exception as exc:  # noqa: BLE001 — the smoke reports, not raises
                status, detail = "fail", str(exc)
            self.results.append(
                {"stage": name, "status": status,
                 "ms": round((time.monotonic() - t0) * 1000, 1),
                 "detail": detail})
            if status == "fail":
                return STAGE_EXIT[name]
        return 0


async def fake_mode(as_json: bool) -> int:
    server = FakeTunerServer(
        responses=[
            (0, "TS|", ["TURN|1", "LOCAL|0", "PUPPET_ACTIVE|false"]),
            (0, "OV|", ["OV|1", "TURN|1", "ALIVE|2",
                        "PLAYER|0|CIVILIZATION_ROME",
                        "PLAYER|1|CIVILIZATION_KOREA"]),
        ],
        mod=FakeMod(),
    )
    port = await server.start()
    try:
        smoke = Smoke(FireTunerAdapter("127.0.0.1", port))
        rc = await smoke.run()
        report(smoke, rc, "fake", as_json)
        return rc
    finally:
        await server.stop()


async def live_mode(host: str, port: int, as_json: bool) -> int:
    smoke = Smoke(FireTunerAdapter(host, port))
    rc = await smoke.run()
    report(smoke, rc, "live", as_json)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    out_dir = Path("runs") / f"live-preflight-{stamp}"
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "transcript.json").write_text(
        json.dumps({"mode": "live", "rc": rc, "stages": smoke.results},
                   indent=2) + "\n")
    return rc


def report(smoke: Smoke, rc: int, mode: str, as_json: bool) -> None:
    if as_json:
        print(json.dumps({"mode": mode, "rc": rc, "stages": smoke.results}))
        return
    for row in smoke.results:
        print(f"[{row['stage']}] {row['status'].upper():4} {row['ms']:8.1f}ms "
              f"{row['detail']}")
    print(f"SMOKE {'OK' if rc == 0 else f'FAILED rc={rc}'} ({mode})")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--live", action="store_true",
                    help="probe a real FireTuner listener")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=4318)
    ap.add_argument("--json", action="store_true",
                    help="print one machine-readable summary doc")
    opts = ap.parse_args()
    if opts.live:
        rc = asyncio.run(live_mode(opts.host, opts.port, opts.json))
    else:
        rc = asyncio.run(fake_mode(opts.json))
    raise SystemExit(rc)


if __name__ == "__main__":
    main()
