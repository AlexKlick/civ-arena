"""FireTunerAdapter — the live Civ VI leg (M14d: action surface + dispatch).

Working: connect (vendored wire layer), the mod handshake gate, turn/lease
polling (D3: poll, never push — the wire drains unsolicited output around
every command), phase open/close over the declared ambient window, a
buffered mutation journal fed by the mod's ledgers, the whole-board digest
as the state-hash source, the six sim-shaped observes, and the seven
action tools routed GameCore/InGame per the live-probed table
(docs/live-validation.md §6).

Still NotImplementedError: ``snapshot``/``restore``, ``export_state``/
``import_state`` (live save/load is M14e), and ground-truth visibility
(``visibility_for`` returns EMPTY sets — the M14d declaration below).

Design notes that cost nothing to forget:

- ``current_phase``/``state_hash`` are SYNC by seam contract, so the
  adapter keeps a mirror (open player, engine turn) and a digest cache,
  refreshed after every state-relevant wire operation (setup, begin_phase,
  end_phase, act). Pre-hash of command N is exactly post-hash of command
  N-1 — the invariant the log's hash trail relies on (Codex P2-10: a
  landed act MUST end with a digest refresh).
- ``end_phase`` strategy is the D7 experiment (h1 UI ENDTURN / h2
  FinishMoves / h3 release-only); h1 is live-proven for the LOCAL seat.
- ``simulate_hook`` is REHEARSAL ONLY (FakeMod ``Simulate.*`` commands);
  never attached against a real game.
- Commanded-effect reconciliation (mod v0.3): every accepted act drains
  ``Puppeteer.DiffSinceLast`` and journals the SAME records as the
  command's mutations and as actuals — the watchdog's exact-key multiset
  diff then matches them. Rejected unit-acts re-freeze the unit so the
  restore can never book as undeclared movement at release.
"""

from __future__ import annotations

import asyncio
import contextlib
import re
import time
from collections.abc import Callable
from typing import Any

from civ_arena.canonical import state_hash as _sha
from civ_arena.game.adapter import (
    ActionCommand,
    ActionResult,
    AdapterCapabilities,
    MutationRecord,
    ObserveKind,
    ObserveRequest,
    RejectionReason,
)
from civ_arena.game.civ6 import lua_translator, response_parser
from civ_arena.game.civ6.vendor.connection import GameConnection, LuaError

_LIVE_POINTER = (
    "live FireTuner support is not implemented yet — see "
    "docs/live-validation.md §'extending the adapter'"
)

# agent-supplied ids/coords are INTERPOLATED into Lua source — only these
# spellings may cross the boundary (defense in depth beyond the referee's
# type checks; a hostile tech_id like "MINING'] Evil() --" dies here).
_UNIT_ID = re.compile(r"^u\d+\Z")
_CITY_ID = re.compile(r"^c\d+\Z")
_TOKEN = re.compile(r"^[A-Z0-9_]+\Z")
_COORD = re.compile(r"^-?\d+,-?\d+\Z")

# tools that operate on a unit: restore its movement before the command
# (the lease froze every unit at engagement), re-freeze on rejection so
# the release diff sees the frozen baseline.
_UNIT_TOOLS = frozenset({"move_unit", "attack", "fortify", "found_city"})

# Codex P1-5: attrs each tool may legitimately move (the mod's DiffSinceLast
# authorizes only rows in this scope for the command; drift on OTHER attrs
# inside the command window is booked as UNDECLARED actuals by the mod).
_TOOL_ATTRS = {
    "move_unit": "pos,moves,damage,exists",
    "attack": "pos,moves,damage,exists",
    "fortify": "moves",
    "found_city": "exists,population,moves",
    "set_research": "researching",
    "set_city_production": "none",   # production is outside recorder coverage
    "purchase": "gold,exists",
}


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


# --------------------------------------------------------------------------
# M14d action routing (live-probed 2026-08-30, docs/live-validation.md §6).
# Every builder returns (lua, ingame): ingame=True -> execute_write (the
# InGame VM: RequestOperation/RequestCommand/UI), False -> execute_read
# (GameCore: research setters — the InGame player-op silently no-ops).
# --------------------------------------------------------------------------


def _arg_violation(tool: str, args: dict[str, Any]) -> str | None:
    """Adapter-side arg re-check (defense in depth): these values are
    interpolated into Lua source, so only strict spellings cross."""
    checks = {
        "unit_id": _UNIT_ID, "target_id": _UNIT_ID, "city_id": _CITY_ID,
        "tech_id": _TOKEN, "item_id": _TOKEN, "dest": _COORD,
    }
    for key, pattern in checks.items():
        value = args.get(key)
        if value is not None and not pattern.match(str(value)):
            return f"{tool}.{key}={value!r} fails the canonical spelling"
    return None


_MOD_VERSION_RE = re.compile(r'Puppeteer\.version\s*=\s*"([^"]+)"')


def _mod_version(lua_text: str) -> str:
    """The version the MOD FILE declares (its source of truth)."""
    m = _MOD_VERSION_RE.search(lua_text)
    if m is None:
        raise RuntimeError("mod file declares no Puppeteer.version")
    return m.group(1)


def _rejection_value(token: str) -> str:
    """The act Lua emits UPPERCASE reason tokens; the seam contract is the
    RejectionReason VALUE (lowercase). An unknown token fails LOUD — a
    typo'd reason must never reach the event log as a free string."""
    try:
        return RejectionReason[token].value
    except KeyError as exc:
        raise RuntimeError(
            f"unknown rejection token on the wire: {token!r}") from exc


_ACT_BUILDERS: dict[str, Callable[..., tuple[str, bool]]] = {
    "move_unit": lambda _pid, a: (lua_translator.move_unit(
        a["unit_id"], a["dest"]), True),
    "attack": lambda _pid, a: (lua_translator.attack(
        a["unit_id"], a["target_id"]), True),
    "fortify": lambda _pid, a: (lua_translator.fortify(a["unit_id"]), True),
    "found_city": lambda _pid, a: (lua_translator.found_city(
        a["unit_id"], a.get("name")), True),
    "set_research": lambda pid, a: (lua_translator.set_research(
        pid, a["tech_id"]), False),
    "set_city_production": lambda _pid, a: (lua_translator.set_city_production(
        a["city_id"], a["item_id"]), True),
    "purchase": lambda _pid, a: (lua_translator.purchase(
        a["city_id"], a["item_id"]), True),
}

# (event, player_id) -> fake-side Lua; the driver attaches this ONLY in
# rehearsal mode. Hook prints are unsolicited and drained in the real wire,
# so engine-side events exist for the adapter only through their effect on
# the next Status/Digest poll.
SimulateHook = Callable[[str, int, int], str]


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
        self._diff_seq = 0
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
        """Raw pipe-rows for translator output (smoke/driver transcript use)
        — GameCore VM."""
        return await self._conn.execute_read(lua)

    async def write_raw(self, lua: str) -> list[str]:
        """Raw pipe-rows in the InGame VM (UI actions; the driver's
        bootstrap end-turn — UI is nil in GameCore, live-learned run 009)."""
        return await self._conn.execute_write(lua)

    async def mod_handshake(self) -> dict[str, Any]:
        """PuppeteerMod capability handshake — fail-closed (parse_handshake)."""
        lines = await self._conn.execute_read(lua_translator.mod_handshake())
        return response_parser.parse_handshake(lines)

    async def inject_mod(self, lua_text: str) -> dict[str, Any]:
        """D9 (live-proven 2026-08-30): gameplay-script globals are INVISIBLE
        to the tuner VM — a conventionally loaded mod can never answer
        FireTuner calls. But ``GameEvents`` subscriptions made FROM the
        tuner VM fire on the engine's own dispatch, so the mod is EXECUTED
        into the GameCore VM at attach instead. The mod file stays the
        source of truth; the .modinfo is a packaging artifact. Returns the
        post-injection handshake (the same fail-closed gate).

        Live-learned 2026-08-30 (run 004): if a capable mod instance is
        ALREADY live, this VERIFIES it instead of re-executing — the
        re-injection hygiene retires the old instance, whose ``lease``
        (and any ENGAGED lease on the engine's parked turn) dies with it.
        Re-inject only on absence, capability mismatch, or VERSION
        mismatch (the file is the source of truth; an older live instance
        lacks the current seam — e.g. turn-bound Release, seq diffs)."""
        file_version = _mod_version(lua_text)
        try:
            doc = await self.require_mod()
            if doc["mod_version"] == file_version:
                return doc
            # version drift: re-inject the file (no lease survives this —
            # the caller boots a parked, lease-free turn or accepts the loss)
        except RuntimeError:
            pass  # absent or incapable: (re-)inject below
        payload = lua_text + (
            "\nprint('MOD_LOADED|' .. tostring(Puppeteer ~= nil "
            "and Puppeteer.version or 'NIL'))\nprint('---END---')\n")
        lines = await self._conn.execute_read(payload, timeout=15.0)
        marker = next(
            (ln for ln in response_parser._split_lines(lines)
             if ln.startswith("MOD_LOADED|")), None)
        if marker is None or marker == "MOD_LOADED|NIL":
            raise RuntimeError(
                f"mod injection failed: {marker!r} from {lines[:3]!r}")
        doc = await self.require_mod()
        if doc["mod_version"] != file_version:
            raise RuntimeError(
                f"injected {file_version!r} but handshake reports "
                f"{doc['mod_version']!r} — a foreign mod instance answered")
        return doc

    async def require_mod(self) -> dict[str, Any]:
        """Phase-1 live gate (docs/live-validation.md §3.1): refuse to drive
        a live match unless the mod reports freeze AND ledger AND digest —
        state_hash cannot operate without the digest (Codex P2-8) — and,
        for dispatch, the v0.3 DiffSinceLast seam."""
        doc = await self.mod_handshake()
        if not (doc["supports_freeze"] and doc["supports_ledger"]
                and doc["supports_digest"]):
            raise RuntimeError(
                f"PuppeteerMod handshake gate failed: {doc} — "
                "freeze+ledger+digest required (docs/live-validation.md §3.1)"
            )
        if not doc["supports_command_diff"]:
            raise RuntimeError(
                f"PuppeteerMod handshake gate failed: {doc} — command_diff "
                "required for M14d dispatch (mod >= 0.3)")
        return doc

    def capabilities(self) -> AdapterCapabilities:
        return AdapterCapabilities(
            rollback=False, save_load=False, acts=True, state_hash=True,
            turn_events=True,
        )

    # -- turn lifecycle ------------------------------------------------------
    async def begin_phase(self, player_id: int, turn: int) -> dict[str, Any]:
        if self._phase_open != -1:
            raise RuntimeError(
                f"phase already open for player {self._phase_open}")
        # SetPuppet BEFORE any turn wait (live-learned 2026-08-30): attaching
        # while the target player's turn is already parked means this turn's
        # hook has FIRED — the lease engages at the player's NEXT natural
        # turn start, so the puppet must be configured first and the caller
        # targets the next turn. Engagement for anyone else, or a stale
        # turn, is a refusal (Codex P1-3). A timeout here IS the finding.
        await self._conn.execute_read(
            lua_translator.set_puppet(player_id, True))
        await self._fire_simulate("turn_start", player_id, turn)
        await self._await(
            lambda p: (p.get("PUPPET_ACTIVE") is True
                       and p.get("LEASE_PLAYER") == player_id
                       and p.get("LEASE_TURN") == turn),
            f"lease to engage for player {player_id} at turn {turn} "
            "(PlayerTurnStartComplete timing)",
            timeout_s=self._turn_wait_s)
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
        self._diff_seq = 0
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
        # SEAL the HELD state BEFORE issuing the end-turn (live-learned
        # 2026-08-30, run live-exclusive-004): the engine applies turn-end
        # effects AFTER the end-turn command — gold income (+5), completed
        # production, the next player's whole turn — and a post-command
        # digest poll races all of it. The sealed hash must bracket the
        # lease WE held (owner-scoped, Codex P1-4), not the engine's
        # post-processing.
        await self._refresh_digest()
        self._sealed_hash = self.state_hash()
        if self._strategy == "h1":
            await self._conn.execute_write(
                lua_translator.request_end_turn(player_id))
        elif self._strategy == "h2":
            await self._conn.execute_read(
                lua_translator.finish_all_moves(player_id))
        # h3: no explicit command — wait for the engine to release on its own
        await self._fire_simulate("turn_deactivated", player_id, turn)
        await self._await(
            lambda p: (p.get("PUPPET_ACTIVE") is False
                       or int(p.get("TURN", -1)) > turn),
            f"lease release for player {player_id} (D7-{self._strategy})",
            timeout_s=self._turn_wait_s)
        # Release books any lease-vs-close drift as UNDECLARED actuals —
        # the referee's next sweep flags them (the live make-or-break check)
        await self._conn.execute_read(lua_translator.release(player_id, turn))
        ledger = await self._conn.execute_read(lua_translator.dump_ledger())
        for doc in response_parser.parse_ledger_lines(ledger):
            self._journal.append(MutationRecord.from_doc(doc))
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

    def expect_turn(self, turn: int) -> None:
        """Advance the turn mirror to the turn the coordinator targets.
        Attach-while-parked: the engine sits one turn BEHIND the target and
        only advances through the lease engagement begin_phase waits for —
        the referee's pre-check would otherwise refuse on a stale mirror.
        Every Status poll re-syncs the mirror to the engine's truth."""
        if turn > self._turn_mirror:
            self._turn_mirror = turn

    async def read_trace(self) -> list[str]:
        """The mod's hook-event ring, flattened (driver targeting)."""
        lines = await self._conn.execute_read(lua_translator.mod_trace())
        return response_parser._split_lines(lines)  # noqa: SLF001

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

    async def _fire_simulate(self, event: str, player_id: int,
                             turn: int = 0) -> None:
        if self._simulate is None:
            return
        await self._conn.execute_read(
            self._simulate(event, player_id, turn))

    # -- observation ----------------------------------------------------------
    async def observe(self, req: ObserveRequest) -> Any:
        # OMNISCIENT by seam contract — the referee projects scope AFTER this
        # returns. Reads are GameCore (verified accessors), except
        # AVAILABLE_PRODUCTION whose CanStartOperation gate is InGame-only.
        if req.kind is ObserveKind.OVERVIEW:
            lines = await self._conn.execute_read(
                lua_translator.overview_read())
            return response_parser.parse_overview(lines)
        if req.kind is ObserveKind.UNITS:
            lines = await self._conn.execute_read(lua_translator.units_read())
            return response_parser.parse_units(lines)
        if req.kind is ObserveKind.CITIES:
            lines = await self._conn.execute_read(lua_translator.cities_read())
            return response_parser.parse_cities(lines)
        if req.kind is ObserveKind.VISIBLE_MAP:
            # M14d declaration: no per-tile read until M14c's revealed-tiles
            # query; under the empty visibility sets the projection emits no
            # tiles anyway — this preserves the shape contract only.
            lines = await self._conn.execute_read(
                lua_translator.visible_map_read())
            parsed = response_parser.parse_kv_lines(lines)
            return {"turn": int(parsed.get("TURN", self._turn_mirror)),
                    "tiles": {}}
        if req.kind is ObserveKind.AVAILABLE_RESEARCH:
            lines = await self._conn.execute_read(
                lua_translator.available_research_read(req.player_id))
            return response_parser.parse_available_research(lines)
        if req.kind is ObserveKind.AVAILABLE_PRODUCTION:
            if req.subject_id is None:
                raise ValueError(
                    "AVAILABLE_PRODUCTION requires subject_id (city_id)")
            lines = await self._conn.execute_write(
                lua_translator.available_production_read(
                    int(req.subject_id[1:])))
            return response_parser.parse_available_production(lines)
        raise ValueError(f"unknown observe kind: {req.kind}")

    def visibility_for(self, player_id: int) -> tuple[frozenset[str], frozenset[str]]:
        """M14d DECLARATION (docs/live-validation.md §6): until M14c's
        revealed-tiles read, the live adapter reports NO observable and NO
        remembered tiles. The projection therefore hides EVERY foreign
        entity — the safe side of the no-leak contract (under-visibility,
        never over-visibility); own entities are ownership-based and
        unaffected. A side effect the driver records: driven agents cannot
        see or attack the opponent's units in M14d games."""
        _ = player_id
        return frozenset(), frozenset()

    # -- action -----------------------------------------------------------------
    async def act(self, cmd: ActionCommand) -> ActionResult:
        if self._phase_open != cmd.player_id:
            return ActionResult(
                status="rejected", result=None, mutations=(),
                rejection="no_lease",
                error=f"phase belongs to player {self._phase_open}")
        builder = _ACT_BUILDERS.get(cmd.tool)
        if builder is None:
            return ActionResult(
                status="rejected", result=None, mutations=(),
                rejection="not_implemented",
                error=f"{cmd.tool} over FireTuner: {_LIVE_POINTER}")
        bad = _arg_violation(cmd.tool, cmd.args)
        if bad is not None:
            return ActionResult(
                status="rejected", result=None, mutations=(),
                rejection="args_invalid", error=bad)
        unit_id = cmd.args.get("unit_id")
        if cmd.tool in _UNIT_TOOLS:
            # unfreeze exactly this unit (the lease froze all of them at
            # engagement; NEVER bulk-restore; once per unit per lease —
            # mod-side, Codex P1-1)
            await self._conn.execute_read(
                lua_translator.restore_unit(unit_id))
        try:
            lua, ingame = builder(cmd.player_id, cmd.args)
            lines = await (self._conn.execute_write(lua) if ingame
                           else self._conn.execute_read(lua))
            verdict = response_parser.parse_act(lines)
        except BaseException:
            # Codex P1-7: a Lua error after the restore must not leak
            # restored movement into the release diff — freeze it back,
            # then let the failure propagate (the driver aborts the run)
            if cmd.tool in _UNIT_TOOLS:
                with contextlib.suppress(Exception):
                    await self._conn.execute_read(
                        lua_translator.freeze_unit(unit_id))
            raise
        if verdict["status"] == "rejected":
            if cmd.tool in _UNIT_TOOLS:
                # undo the restore: re-freeze so the restored-but-unused
                # movement never books as undeclared drift at release
                await self._conn.execute_read(
                    lua_translator.freeze_unit(unit_id))
            return ActionResult(
                status="rejected", result=verdict, mutations=(),
                rejection=_rejection_value(verdict["rejection"]),
                error=verdict["detail"])
        # Codex P2-10: a landed act MUST refresh the digest before returning
        # — otherwise execute()'s post-hash is the PRE-command digest and the
        # log's hash trail goes stale mid-lease.
        await self._refresh_digest()
        muts = await self._drain_command_diff(
            cmd.player_id, _TOOL_ATTRS.get(cmd.tool, ""),
            self._next_diff_seq())
        return ActionResult(
            status="accepted",
            result={"tool": cmd.tool, "detail": verdict["detail"]},
            mutations=tuple(muts))

    def _next_diff_seq(self) -> int:
        """Per-lease monotonically increasing diff sequence (Codex P1-6):
        a wire retry re-sends the same seq and re-serves the same rows; a
        fresh command always computes a fresh window."""
        self._diff_seq += 1
        return self._diff_seq

    async def _drain_command_diff(self, player_id: int,
                                  attrs: str = "",
                                  seq: int = 0) -> list[MutationRecord]:
        """The commanded-effects seam (mod v0.3): journal DiffSinceLast's
        rows as BOTH the command's mutations (allowed, via the returned
        records) and actuals (via the journal the referee drains) — the
        exact-key multiset diff then reconciles them. Rows the diff never
        covers (production, promotions — declared recorder limitations)
        simply book nothing, on both sides."""
        lines = await self._conn.execute_read(
            lua_translator.diff_since_last(attrs, seq))
        if any(ln.startswith("MOD_DIFF|unavailable") for ln in lines):
            raise RuntimeError(
                "mod has no DiffSinceLast (need >= 0.3) — commanded effects "
                "cannot be reconciled; refusing to continue this lease")
        records = [
            MutationRecord(
                kind=doc["kind"], entity_type=doc["entity_type"],
                entity_id=doc["entity_id"], attr=doc["attr"],
                before=doc["before"], after=doc["after"],
                origin="command")
            for doc in response_parser.parse_ledger_lines(lines)
        ]
        self._journal.extend(records)
        return records

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
