"""Smoke: drive FireTunerAdapter.setup + current_phase end-to-end.

Default mode runs against an in-process FakeTunerServer (no game needed):

    uv run python scripts/firetuner_smoke.py

Live mode probes a real FireTuner listener (requires Civ VI running with
EnableTuner=1 — see docs/live-validation.md):

    uv run python scripts/firetuner_smoke.py --live --host 127.0.0.1 --port 4318
"""

from __future__ import annotations

import argparse
import asyncio

from civ_arena.game.civ6.fake_tuner_server import FakeTunerServer
from civ_arena.game.civ6.firetuner import FireTunerAdapter


async def fake_mode() -> int:
    server = FakeTunerServer([(0, "TURN|", ["TURN|42", "LOCAL|0",
                                             "PUPPET_ACTIVE|false"])])
    port = await server.start()
    try:
        adapter = FireTunerAdapter("127.0.0.1", port)
        await adapter.setup({})
        phase = await adapter.current_phase()
        print(f"FAKE tuner: connected, handshake ok, current_phase={phase}")
        await adapter.teardown()
        return 0
    finally:
        await server.stop()


async def live_mode(host: str, port: int) -> int:
    adapter = FireTunerAdapter(host, port)
    await adapter.setup({})
    phase = await adapter.current_phase()
    print(f"LIVE tuner: connected, current_phase={phase}")
    await adapter.teardown()
    return 0


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--live", action="store_true")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=4318)
    opts = ap.parse_args()
    if opts.live:
        raise SystemExit(asyncio.run(live_mode(opts.host, opts.port)))
    raise SystemExit(asyncio.run(fake_mode()))


if __name__ == "__main__":
    main()
