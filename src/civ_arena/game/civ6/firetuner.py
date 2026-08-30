"""FireTunerAdapter — the live Civ VI leg (M14b: phase surface + watchdog).

Working: connect (vendored wire layer), the mod handshake gate, turn/lease
polling (D3: poll, never push — the wire drains unsolicited output around
every command), phase open/close over the declared ambient window, a
buffered mutation journal fed by the mod's ledgers, and the whole-board
digest as the state-hash source.

Still NotImplementedError: the 5 non-OVERVIEW observes, ``visibility_for``,
``act``, ``snapshot``/``restore``, ``export_state``/``import_state`` — each
lands per docs/live-validation.md §4 (one translator entry + one parser
entry + one fake test; zero changes in ``arena/``, ``session/``, or
``agents/``).

Design notes that cost nothing to forget:

- ``current_phase``/``state_hash`` are SYNC by seam contract, so the
  adapter keeps a mirror (open player, engine turn) and a digest cache,
  refreshed after every state-relevant wire operation (setup, begin_phase,
  end_phase). Pre-hash of command N is exactly post-hash of command N-1 —
  the invariant the log's hash trail relies on.
- ``end_phase`` strategy is the D7 experiment (h1 UI ENDTURN / h2
  FinishMoves / h3 release-only); outcomes are recorded dated in
  docs/live-validation.md §6 and the winner becomes the default.
- ``simulate_hook`` is REHEARSAL ONLY (FakeMod ``Simulate.*`` commands);
  never attached against a real game.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from typing import Any

from civ_arena.canonical import state_hash as _sha
from civ_arena.game.adapter import (
    AdapterCapabilities,
    MutationRecord,
    ObserveKind,
    ObserveRequest,
)
from civ_arena.game.civ6 import lua_translator, response_parser
from civ_arena.game.civ6.vendor.connection import GameConnection, LuaError

_LIVE_POINTER = (
    "live FireTuner support is not implemented yet — see "
    "docs/live-validation.md §'extending the adapter'"
)


def _row_owner(row: str) -> int | None:
    """Owner player of one digest row (u<uid>|<pid>|.., c<cid>|<pid>|..,
    p<pid>|..) — None for unattributed rows."""
    parts = row.split("|")
    head = parts[0]
    if head[:1] in ("u", "c") and len(parts) > 1:
        try:
            return int(parts[1])
        except ValueError:
            return None
    if head[:1] == "p":
        try:
            return int(head[1:])
        except ValueError:
            return None
    return None


def filter_digest_rows(digest: str, owner: int) -> list[str]:
    """The phase owner's slice of a whole-board digest. Live play is
    asynchronous: after our release the next player mutates the board
    immediately, so a phase's hashes must not cover foreign activity
    (Codex P1-4) — the whole board hashes only when no phase is open."""
    return sorted(r for r in digest.split(";") if r and _row_owner(r) == owner)

# (event, player_id) -> fake-side Lua; the driver attaches this ONLY in
# rehearsal mode. Hook prints are unsolicited and drained in the real wire,
# so engine-side events exist for the adapter only through their effect on
# the next Status/Digest poll.
SimulateHook = Callable[[str, int], str]


class _LiveStateView:
    """Minimal sim-state shim: referee.abort_cleanup reads
    ``adapter.state.phase_player``/``.turn`` directly, and ``arena/`` is
    untouchable — so the adapter exposes the mirror under those names."""

    def __init__(self, adapter: FireTunerAdapter) -> None:
        self._adapter = adapter

    @property
    def phase_player(self) -> int:
        return self._adapter._phase_open

    @property
    def turn(self) -> int:
        return self._adapter._turn_mirror


class FireTunerAdapter:
    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 4318,
        conn: GameConnection | None = None,
        *,
        end_phase_strategy: str = "h1",
        poll_interval_s: float = 0.2,
        poll_timeout_s: float = 10.0,
        turn_wait_s: float = 120.0,
        simulate_hook: SimulateHook | None = None,
    ) -> None:
        if end_phase_strategy not in ("h1", "h2", "h3"):
            raise ValueError(
                f"end_phase_strategy must be h1|h2|h3, got "
                f"{end_phase_strategy!r}")
        self._conn = conn if conn is not None else GameConnection(host, port)
        self._strategy = end_phase_strategy
        self._poll_interval_s = poll_interval_s
        self._poll_timeout_s = poll_timeout_s
        self._turn_wait_s = turn_wait_s
        self._simulate = simulate_hook
        self._phase_open = -1
        self._turn_mirror = -1
        self._journal: list[MutationRecord] = []
        self._digest_text: str | None = None
        # hash scoping (Codex P1-4): while a phase is open (and for the
        # sealed phase-end hash) state_hash covers ONLY the phase owner's
        # digest rows — the next player acts immediately after release and
        # must not leak into this phase's hash trail
        self._hash_owner: int | None = None
        self._sealed_hash: str | None = None
        self.state = _LiveStateView(self)

    # -- lifecycle ---------------------------------------------------------
    async def setup(self, cfg: dict[str, Any]) -> None:
        await self._conn.connect()  # raises ConnectionError with the EnableTuner hint
        # seed the mirror: the mod's pollable Status when present, else the
        # plain turn-state probe (pre-0.2 mods / probe-only sessions)
        try:
            parsed = await self.poll_status()
        except (RuntimeError, LuaError):
            lines = await self._conn.execute_read(
                lua_translator.poll_turn_state())
            parsed = response_parser.parse_kv_lines(lines)
        self._turn_mirror = int(parsed.get("TURN", -1))
        # digest seeding is best-effort: a mod-absent session still probes
        # (require_mod refuses later); state_hash stays unavailable until a
        # digest exists — nothing drives a match without the mod anyway.
        # ValueError = no DIGEST row; LuaError = broken Digest() — both must
        # stay out of setup so the SMOKE can blame the right stage (S1 must
        # not absorb an S5 failure; Codex P2-11).
        try:
            await self._refresh_digest()
        except (ValueError, LuaError):
            self._digest_text = None

    async def current_phase(self) -> dict[str, Any]:
        # async by seam contract (the referee awaits it); the mirror itself
        # is sync state — no wire traffic happens here
        return {
            "turn": self._turn_mirror,
            "phase_player": self._phase_open,
            # the live engine has no exposed per-player phase index; the
            # referee only consumes turn + phase_player
            "phase_index": 0,
        }

    async def teardown(self) -> None:
        await self._conn.disconnect()

    async def read_raw(self, lua: str) -> list[str]:
        """Raw pipe-rows for translator output (smoke/driver transcript use)."""
        return await self._conn.execute_read(lua)

    async def mod_handshake(self) -> dict[str, Any]:
        """PuppeteerMod capability handshake — fail-closed (parse_handshake)."""
        lines = await self._conn.execute_read(lua_translator.mod_handshake())
        return response_parser.parse_handshake(lines)

    async def require_mod(self) -> dict[str, Any]:
        """Phase-1 live gate (docs/live-validation.md §3.1): refuse to drive
        a live match unless the mod reports freeze AND ledger AND digest —
        state_hash cannot operate without the digest (Codex P2-8)."""
        doc = await self.mod_handshake()
        if not (doc["supports_freeze"] and doc["supports_ledger"]
                and doc["supports_digest"]):
            raise RuntimeError(
                f"PuppeteerMod handshake gate failed: {doc} — "
                "freeze+ledger+digest required (docs/live-validation.md §3.1)"
            )
        return doc

    def capabilities(self) -> AdapterCapabilities:
        return AdapterCapabilities(
            rollback=False, save_load=False, acts=False, state_hash=True,
            turn_events=True,
        )

    # -- turn lifecycle ------------------------------------------------------
    async def begin_phase(self, player_id: int, turn: int) -> dict[str, Any]:
        if self._phase_open != -1:
            raise RuntimeError(
                f"phase already open for player {self._phase_open}")
        await self._await(
            lambda p: int(p.get("TURN", -1)) == turn,
            f"engine to reach turn {turn}")
        await self._conn.execute_read(
            lua_translator.set_puppet(player_id, True))
        await self._fire_simulate("turn_start", player_id)
        # The make-or-break wait (upstream open question 1): the hook must
        # engage the freeze BEFORE the stock AI acts, for THIS player at THIS
        # turn — an engaged lease for anyone else is a refusal, not a pass
        # (Codex P1-3). A timeout here IS the finding — record it, stop.
        await self._await(
            lambda p: (p.get("PUPPET_ACTIVE") is True
                       and p.get("LEASE_PLAYER") == player_id
                       and p.get("LEASE_TURN") == turn),
            f"lease to engage for player {player_id} at turn {turn} "
            "(PlayerTurnStartComplete timing)")
        # Declared ambient window: snapshot -> diff -> manifest rows
        await self._conn.execute_read(
            lua_translator.begin_ambient_window(player_id))
        await self._conn.execute_read(
            lua_translator.end_ambient_window(player_id))
        ambient = await self._conn.execute_read(lua_translator.dump_ambient())
        manifest = response_parser.parse_ledger_lines(ambient)
        self._phase_open = player_id
        self._hash_owner = player_id
        self._sealed_hash = None
        self._turn_mirror = turn
        await self._refresh_digest()
        return {"manifest": manifest}

    async def end_phase(self, player_id: int, turn: int) -> dict[str, Any]:
        if self._phase_open != player_id:
            raise RuntimeError(
                f"end_phase: phase open for {self._phase_open}, not {player_id}")
        if self._turn_mirror != turn:
            raise RuntimeError(
                f"end_phase turn mismatch: mirror {self._turn_mirror}, "
                f"asked {turn}")
        if self._strategy == "h1":
            await self._conn.execute_write(
                lua_translator.request_end_turn(player_id))
        elif self._strategy == "h2":
            await self._conn.execute_read(
                lua_translator.finish_all_moves(player_id))
        # h3: no explicit command — wait for the engine to release on its own
        await self._fire_simulate("turn_deactivated", player_id)
        await self._await(
            lambda p: (p.get("PUPPET_ACTIVE") is False
                       or int(p.get("TURN", -1)) > turn),
            f"lease release for player {player_id} (D7-{self._strategy})",
            timeout_s=self._turn_wait_s)
        # Release books any lease-vs-close drift as UNDECLARED actuals —
        # the referee's next sweep flags them (the live make-or-break check)
        await self._conn.execute_read(lua_translator.release(player_id))
        ledger = await self._conn.execute_read(lua_translator.dump_ledger())
        for doc in response_parser.parse_ledger_lines(ledger):
            self._journal.append(MutationRecord.from_doc(doc))
        # SEAL the phase-end hash while the owner scope is still set: the
        # digest refresh races the next player's first mutations in live
        # play, so the hash is fixed here — owner-scoped, immune to foreign
        # activity (Codex P1-4) — and served to the referee's post-hash call
        await self._refresh_digest()
        self._sealed_hash = self.state_hash()
        self._hash_owner = None
        self._phase_open = -1
        # rehearsal-only: the real engine advances on its own after release
        await self._fire_simulate("advance_turn", player_id)
        return {}

    # -- polling (D3: poll, never push) --------------------------------------
    async def poll_status(self) -> dict[str, Any]:
        lines = await self._conn.execute_read(lua_translator.mod_status())
        if any(ln.startswith("MOD_STATUS|unavailable") for ln in lines):
            raise RuntimeError("mod has no pollable Status (need >= 0.2)")
        parsed = response_parser.parse_kv_lines(lines)
        if "TURN" in parsed:
            self._turn_mirror = int(parsed["TURN"])
        return parsed

    async def refresh_digest(self) -> str:
        """Poll the whole-board digest and return the new state hash."""
        await self._refresh_digest()
        return self.state_hash()

    async def _refresh_digest(self) -> None:
        lines = await self._conn.execute_read(lua_translator.mod_digest())
        self._digest_text = response_parser.parse_digest(lines)

    async def _await(
        self, predicate: Callable[[dict[str, Any]], bool], what: str,
        timeout_s: float | None = None,
    ) -> dict[str, Any]:
        deadline = time.monotonic() + (timeout_s or self._poll_timeout_s)
        while True:
            parsed = await self.poll_status()
            if predicate(parsed):
                return parsed
            if time.monotonic() >= deadline:
                raise RuntimeError(f"timed out waiting for {what}: {parsed}")
            await asyncio.sleep(self._poll_interval_s)

    async def _fire_simulate(self, event: str, player_id: int) -> None:
        if self._simulate is None:
            return
        await self._conn.execute_read(self._simulate(event, player_id))

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
        # M14d contract: a landed act MUST end with await self._refresh_digest()
        # (Codex P2-10) — otherwise execute()'s post-hash is the PRE-command
        # digest and the log's hash trail goes stale mid-lease.
        raise NotImplementedError(f"act over FireTuner: {_LIVE_POINTER}")

    # -- watchdog / persistence ----------------------------------------------
    def snapshot(self) -> Any:
        raise NotImplementedError(f"snapshot over FireTuner: {_LIVE_POINTER}")

    def restore(self, snap: Any) -> None:
        raise NotImplementedError(f"restore over FireTuner: {_LIVE_POINTER}")

    def drain_mutations(self) -> list[MutationRecord]:
        out, self._journal = self._journal, []
        return out

    def state_hash(self) -> str:
        if self._sealed_hash is not None:
            return self._sealed_hash
        if self._digest_text is None:
            raise RuntimeError("state_hash before setup — no digest cached")
        if self._hash_owner is not None:
            rows = filter_digest_rows(self._digest_text, self._hash_owner)
            return _sha({"live_digest": ";".join(rows)})
        return _sha({"live_digest": self._digest_text})

    def export_state(self) -> dict[str, Any]:
        raise NotImplementedError(f"export_state over FireTuner: {_LIVE_POINTER}")

    def import_state(self, doc: dict[str, Any]) -> None:
        raise NotImplementedError(f"import_state over FireTuner: {_LIVE_POINTER}")
