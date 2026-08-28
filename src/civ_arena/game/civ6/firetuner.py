"""FireTunerAdapter — the live Civ VI leg, skeleton scope.

Working today: connect (vendored wire layer), poll turn state, read an
omniscient overview in referee scope. Everything else raises
NotImplementedError naming the missing piece and where to add it — adding
live support for an arena tool is one lua_translator entry + one
response_parser entry + one FakeTunerServer canned test, with zero changes
in arena/, session/, or agents/. See docs/live-validation.md.
"""

from __future__ import annotations

from typing import Any

from civ_arena.game.adapter import (
    AdapterCapabilities,
    ObserveKind,
    ObserveRequest,
)
from civ_arena.game.civ6 import lua_translator, response_parser
from civ_arena.game.civ6.vendor.connection import GameConnection

_LIVE_POINTER = (
    "live FireTuner support is not implemented yet — see "
    "docs/live-validation.md §'extending the adapter'"
)


class FireTunerAdapter:
    def __init__(self, host: str = "127.0.0.1", port: int = 4318) -> None:
        self._conn = GameConnection(host, port)

    # -- lifecycle ---------------------------------------------------------
    async def setup(self, cfg: dict[str, Any]) -> None:
        await self._conn.connect()  # raises ConnectionError with the EnableTuner hint

    async def teardown(self) -> None:
        await self._conn.disconnect()

    def capabilities(self) -> AdapterCapabilities:
        return AdapterCapabilities(
            rollback=False, save_load=False, acts=False, state_hash=False,
            turn_events=True,
        )

    # -- turn lifecycle ------------------------------------------------------
    async def begin_phase(self, player_id: int, turn: int) -> dict[str, Any]:
        raise NotImplementedError(
            f"begin_phase over FireTuner: {_LIVE_POINTER}. The turn-interception "
            "mod (mods/PuppeteerMod) supplies the freeze/lease/restore handshake."
        )

    async def end_phase(self, player_id: int, turn: int) -> dict[str, Any]:
        raise NotImplementedError(f"end_phase over FireTuner: {_LIVE_POINTER}")

    async def current_phase(self) -> dict[str, Any]:
        lines = await self._conn.execute_read(lua_translator.poll_turn_state())
        parsed = response_parser.parse_kv_lines(lines)
        return {
            "turn": parsed.get("TURN", -1),
            "phase_player": -1,  # live engine does not expose the acting player
            "raw": parsed,
        }

    # -- observation ----------------------------------------------------------
    async def observe(self, req: ObserveRequest) -> Any:
        if req.kind is ObserveKind.OVERVIEW:
            lines = await self._conn.execute_read(lua_translator.overview_read())
            return response_parser.parse_kv_lines(lines)
        raise NotImplementedError(
            f"observe {req.kind.value} over FireTuner: {_LIVE_POINTER}"
        )

    def visibility_for(self, player_id: int) -> tuple[frozenset[str], frozenset[str]]:
        raise NotImplementedError(
            f"visibility_for over FireTuner: {_LIVE_POINTER}. The mod's "
            "revealed-tiles query supplies remembered/observable sets."
        )

    # -- action -----------------------------------------------------------------
    async def act(self, cmd: Any) -> Any:
        raise NotImplementedError(f"act over FireTuner: {_LIVE_POINTER}")

    # -- watchdog / persistence ----------------------------------------------
    def snapshot(self) -> Any:
        raise NotImplementedError(f"snapshot over FireTuner: {_LIVE_POINTER}")

    def restore(self, snap: Any) -> None:
        raise NotImplementedError(f"restore over FireTuner: {_LIVE_POINTER}")

    def drain_mutations(self) -> list[Any]:
        raise NotImplementedError(
            f"drain_mutations over FireTuner: {_LIVE_POINTER}. The mod's command "
            "ledger (Puppeteer.DumpLedger) is the live mutation journal."
        )

    def state_hash(self) -> str:
        raise NotImplementedError(
            f"state_hash over FireTuner: {_LIVE_POINTER}. Puppeteer.Digest is the "
            "live before/after 'hash'."
        )

    def export_state(self) -> dict[str, Any]:
        raise NotImplementedError(f"export_state over FireTuner: {_LIVE_POINTER}")

    def import_state(self, doc: dict[str, Any]) -> None:
        raise NotImplementedError(f"import_state over FireTuner: {_LIVE_POINTER}")
