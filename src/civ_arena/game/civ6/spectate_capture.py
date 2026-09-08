"""Spectator-capture primitives: turn-boundary watch + census.

The spectate phase ONLY observes: polls (Status/Trace/Digest), the
sim-shaped observe reads, and the mod's lease-free ambient-window
RECORDER commands (Begin/EndAmbientWindow/DumpAmbient — mod-local Lua
state, no engine effect). Nothing here sends a game-affecting command,
switches the local player, or touches a lease.

``TurnWatch`` is a cursor over the mod's bounded hook-trace ring that
survives ring wrap: each poll's flattened ring is matched against the
previous poll by longest-suffix/longest-prefix overlap; entries past the
overlap are new. A total overlap miss means the ring wrapped past the
cursor between polls — the gap is REPORTED (``gaps`` counter), never
fabricated over.

``SpectatorCensus`` brackets its reads with whole-board digests: the
board can legitimately move between consecutive Lua executions (engine
turn-end processing), so a changed bracket marks the snapshot
``consistent=False`` and the caller retries once. Human and AI per-turn
deltas come from ambient windows (per-player, arbitrary-length, lease
free by construction).
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any

from civ_arena.game.adapter import ObserveKind, ObserveRequest
from civ_arena.game.civ6 import lua_translator

# a snapshot event is bounded; the digest/counts always survive truncation
MAX_SNAPSHOT_BYTES = 256 * 1024


@dataclass(frozen=True)
class SpectateLimits:
    """Spectate pacing (the human-turn budget lives on SpectateSpec —
    AUDIT-ONLY there, never acted on; one source of truth)."""

    poll_s: float
    heartbeat_s: float
    match_s: float


class TurnWatch:
    """Cursor over the mod's bounded hook-trace ring (wrap-safe). ``gaps``
    counts wraps past the cursor; ``generation`` counts ring EPOCH resets
    (the ring restarting at an earlier turn — a mod reload or engine
    transition). Both are reported, never fabricated over."""

    def __init__(self) -> None:
        self._prev: list[str] = []
        self.gaps = 0
        self.generation = 0
        self._prev_first_turn: int | None = None

    def new_entries(self, lines: list[str]) -> tuple[list[str], bool]:
        """Returns (new entries, gap). On a gap every current entry is
        returned as new (some history was lost to the wrap)."""
        current = [ln for ln in lines if ln.strip()]
        if current:
            first = TurnWatch.parse(current[0])
            first_turn = first[0] if first else None
            if (self._prev_first_turn is not None
                    and first_turn is not None
                    and first_turn < self._prev_first_turn):
                # the ring restarted at an earlier turn: new epoch, and
                # continuity across it is void — report BOTH a new
                # generation and a gap (entries between epochs unknowable)
                self.generation += 1
                self.gaps += 1
                self._prev_first_turn = first_turn
                self._prev = current
                return current, True
            self._prev_first_turn = first_turn
        overlap = 0
        if self._prev:
            for k in range(min(len(self._prev), len(current)), 0, -1):
                if self._prev[-k:] == current[:k]:
                    overlap = k
                    break
            if overlap == 0 and current:
                self.gaps += 1
                self._prev = current
                return current, True
        new = current[overlap:]
        self._prev = current
        return new, False

    @staticmethod
    def parse(entry: str) -> tuple[int, str, int] | None:
        """``<turn>|<EVENT>|<pid>`` -> (turn, event, pid); None for noise."""
        parts = entry.split("|")
        if len(parts) != 3:
            return None
        try:
            return int(parts[0]), parts[1], int(parts[2])
        except ValueError:
            return None


def _owner_of(entity_id: str) -> int | None:
    """Owner encoded in a qualified id (``u<owner>:<id>`` / ``c<owner>:<id>``
    / ``p<owner>``) — the entity's owner AT OBSERVATION, which is evidence
    about OWNERSHIP, never about WHO ACTED."""
    head = entity_id.split(":", 1)[0]
    if len(head) > 1 and head[0] in "ucp" and head[1:].isdigit():
        return int(head[1:])
    return None


def parse_ambient_rows(lines: list[str]) -> list[dict[str, Any]]:
    """``AMBIENT|kind|entity_type|entity_id|attr|before|after`` rows from
    DumpAmbient -> compact docs (values stay strings: canonical JSON).
    Every row is a NET state-interval difference: ``actor_id`` is null by
    construction (owner != actor — an owner's-unit change can be caused by
    any participant or an automatic effect) and ``evidence_kind`` says so."""
    rows: list[dict[str, Any]] = []
    for line in lines:
        parts = line.split("|")
        if len(parts) == 7 and parts[0] == "AMBIENT":
            rows.append({"kind": parts[1], "entity_type": parts[2],
                         "entity_id": parts[3], "attr": parts[4],
                         "before": parts[5], "after": parts[6],
                         "entity_owner_at_observation":
                             _owner_of(parts[3]),
                         "actor_id": None,
                         "evidence_kind": "state_interval_diff"})
    return rows


def _compact_units(units: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{k: u[k] for k in ("unit_id", "owner", "type", "q", "r",
                               "hp", "movement", "fortified") if k in u}
            for u in units]


def _compact_cities(cities: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{k: c[k] for k in ("city_id", "owner", "name", "q", "r",
                               "population") if k in c}
            for c in cities]


class SpectatorCensus:
    """Lease-free per-round capture: ambient windows + digest-bracketed
    census reads. ``snapshot`` is called at human turn START (all AI turns
    of the previous game turn are complete by construction) — but the
    reads themselves happen at READ TIME: every snapshot doc carries
    ``census_phase="read_time"`` and an explicit ``atomic`` flag (True
    only when the digest bracket held). A snapshot NEVER claims to be the
    historical state at the boundary hook that triggered it."""

    def __init__(self, adapter: Any, spec: Any) -> None:
        self._adapter = adapter
        self._spec = spec
        self.retries = 0

    async def open_window(self, pid: int) -> None:
        await self._adapter.read_raw(lua_translator.begin_ambient_window(pid))

    async def close_window(self, pid: int, *,
                           actor_class: str | None = None) -> list[dict[str, Any]]:
        """Close the window and drain its AMBIENT rows. ``actor_class``
        annotates WINDOW PROVENANCE (e.g. "human_seat") — it never sets
        ``actor_id``: a window diff is net state change, and who caused it
        is not observable through this seam."""
        await self._adapter.read_raw(lua_translator.end_ambient_window(pid))
        dumped = await self._adapter.read_raw(lua_translator.dump_ambient())
        rows = parse_ambient_rows(dumped)
        if actor_class is not None:
            for row in rows:
                row["actor_class"] = actor_class
        return rows

    async def snapshot(self) -> dict[str, Any]:
        """One census read, digest-bracketed. One retry on drift; a still-
        moving board is reported ``consistent=False`` (never fabricated).

        M4 (§4b): at ``snapshot_scope == "full"`` the snapshot gains an
        ADDITIVE ``world`` key — roster + owned tiles through the READ
        transport ONLY (palette needs the write transport and the
        spectate phase never writes), once per round (here, never per
        poll), bracketed by its OWN digest pair with a
        ``world.digest_consistent`` flag. A failing world read degrades to
        a world doc carrying the error class — the census itself never
        fails on the world block."""
        started = time.monotonic()
        for attempt in (1, 2):
            if attempt == 2:
                self.retries += 1
            before = await self._adapter.refresh_digest()
            overview = await self._adapter.observe(
                ObserveRequest(kind=ObserveKind.OVERVIEW,
                               player_id=self._spec.human_seat))
            doc: dict[str, Any] = {
                "overview": overview,
                "census_phase": "read_time",
                "counts": {
                    "units": {str(p): 0 for p in self._spec.observed_players},
                    "cities": {str(p): 0 for p in self._spec.observed_players},
                },
            }
            units = cities = None
            if self._spec.snapshot_scope == "full":
                units = await self._adapter.observe(
                    ObserveRequest(kind=ObserveKind.UNITS,
                                   player_id=self._spec.human_seat))
                cities = await self._adapter.observe(
                    ObserveRequest(kind=ObserveKind.CITIES,
                                   player_id=self._spec.human_seat))
                doc["units"] = _compact_units(units)
                doc["cities"] = _compact_cities(cities)
                world_before = await self._adapter.refresh_digest()
                try:
                    from civ_arena.game.civ6 import world_capture
                    world = await world_capture.spectate_world(
                        self._adapter, turn=int(overview.get("turn") or 0))
                except Exception as exc:  # noqa: BLE001 — additive, never fatal
                    world = {"schema": 1, "after_seat": -1, "error":
                             type(exc).__name__}
                world_after = await self._adapter.refresh_digest()
                world["digest_consistent"] = world_before == world_after
                doc["world"] = world
            after = await self._adapter.refresh_digest()
            if units is not None:
                for u in units:
                    key = str(u.get("owner"))
                    if key in doc["counts"]["units"]:
                        doc["counts"]["units"][key] += 1
            if cities is not None:
                for c in cities:
                    key = str(c.get("owner"))
                    if key in doc["counts"]["cities"]:
                        doc["counts"]["cities"][key] += 1
            consistent = before == after
            doc["digest"] = {"before": before, "after": after,
                             "consistent": consistent}
            doc["atomic"] = consistent
            doc["census_ms"] = int((time.monotonic() - started) * 1000)
            if consistent:
                return self._capped(doc)
        return self._capped(doc)

    def _capped(self, doc: dict[str, Any]) -> dict[str, Any]:
        """Bound the event payload; digests/counts always survive. M4: the
        ``world`` block drops FIRST (before units/cities), recorded as
        ``truncated.world: true`` — the spectator territory layer is the
        most regenerable part of the payload."""
        if len(json.dumps(doc, sort_keys=True).encode()) <= MAX_SNAPSHOT_BYTES:
            return doc
        if "world" in doc:
            doc.pop("world", None)
            doc["truncated"] = {"world": True}
            if len(json.dumps(doc, sort_keys=True).encode()) <= MAX_SNAPSHOT_BYTES:
                return doc
        doc.pop("units", None)
        doc.pop("cities", None)
        truncated = doc.setdefault("truncated", {})
        if isinstance(truncated, dict):
            truncated["census"] = True
        else:
            doc["truncated"] = True  # legacy marker shape (pre-M4 docs)
        if len(json.dumps(doc, sort_keys=True).encode()) > MAX_SNAPSHOT_BYTES:
            doc.pop("overview", None)
        return doc


class RecorderCapabilityError(RuntimeError):
    """The spectator transport refused an operation outside its capability
    allowlist — raised BEFORE dispatch, so the operation never reaches the
    wire (Exchange-2 F-03: enforcement at the dispatch boundary, not a
    declared constant)."""


class SpectateTransport:
    """Capability wrapper around the adapter the spectate phase uses.

    Allowlisted: the observer operations (poll_status / read_trace /
    refresh_digest / observe) and the mod's lease-free ambient-window
    RECORDER commands via read_raw. Lifecycle (setup / inject_mod /
    teardown) is explicitly permitted and counted separately as
    ``recorder_lifecycle`` — mod injection executes recorder-local Lua
    source, it is not a gameplay mutation. EVERYTHING else — act,
    write_raw, begin/end_phase, set_puppet, and any attribute not listed
    here — raises :class:`RecorderCapabilityError` before dispatch.

    ``census`` counts what actually went out: per-op sent counts, the
    lifecycle count, and ``rejected`` (blocked attempts). The summary's
    command_census is derived from this, not from a literal.

    M4 (§4b): ``read_raw`` additionally admits the two SPECW world reads
    as EXACT full-command matches (see ``_allowed_world_reads``) — a
    one-character drift in the world Lua re-tightens the allowlist by
    construction (CAP-R1 finding 5's fix direction). The legacy
    ambient-window entries keep their original PREFIX matching, untouched."""

    _ALLOWED_READ_RAW = ("Puppeteer.BeginAmbientWindow",
                         "Puppeteer.EndAmbientWindow",
                         "Puppeteer.DumpAmbient")

    @staticmethod
    def _allowed_world_reads() -> frozenset[str]:
        """M4 §4b: the two GameCore SPECW reads the spectator world needs,
        allowed as EXACT full-command matches (CAP-R1 finding 5's fix
        direction: equality, not prefix — a one-character edit to the
        world Lua re-tightens the allowlist by construction). Computed
        lazily: world_capture imports this module for
        MAX_SNAPSHOT_BYTES, so a module-level import would cycle."""
        from civ_arena.game.civ6 import world_capture
        return frozenset((world_capture.roster_read(),
                          world_capture.owned_tiles_read()))

    def __init__(self, adapter: Any) -> None:
        self._adapter = adapter
        self.census: dict[str, int] = {
            "status_polls": 0, "trace_polls": 0, "digest_reads": 0,
            "observe_reads": 0, "recorder_commands": 0,
            "recorder_lifecycle": 0, "rejected": 0,
        }

    # -- lifecycle (permitted, audited separately) -------------------------
    async def setup(self, cfg: dict[str, Any]) -> Any:
        self.census["recorder_lifecycle"] += 1
        return await self._adapter.setup(cfg)

    async def inject_mod(self, lua_text: str) -> Any:
        self.census["recorder_lifecycle"] += 1
        return await self._adapter.inject_mod(lua_text)

    async def teardown(self) -> None:
        self.census["recorder_lifecycle"] += 1
        await self._adapter.teardown()

    # -- observer operations -------------------------------------------------
    async def poll_status(self) -> dict[str, Any]:
        self.census["status_polls"] += 1
        return await self._adapter.poll_status()

    async def read_trace(self) -> list[str]:
        self.census["trace_polls"] += 1
        return await self._adapter.read_trace()

    async def refresh_digest(self) -> str:
        self.census["digest_reads"] += 1
        return await self._adapter.refresh_digest()

    async def observe(self, req: Any) -> Any:
        self.census["observe_reads"] += 1
        return await self._adapter.observe(req)

    async def read_raw(self, lua: str) -> list[str]:
        if not (lua.startswith(self._ALLOWED_READ_RAW)
                or lua in self._allowed_world_reads()):
            self.census["rejected"] += 1
            raise RecorderCapabilityError(
                f"spectator read_raw is restricted to the ambient-window "
                f"recorder commands and the exact SPECW world reads, "
                f"refusing: {lua[:80]!r}")
        self.census["recorder_commands"] += 1
        return await self._adapter.read_raw(lua)

    # -- everything else is refused BEFORE dispatch --------------------------
    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            raise AttributeError(name)
        self.census["rejected"] += 1
        raise RecorderCapabilityError(
            f"spectator transport does not expose {name!r} — the recorder "
            "has no gameplay-mutating path")
