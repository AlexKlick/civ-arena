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
    """Cursor over the mod's bounded hook-trace ring (wrap-safe)."""

    def __init__(self) -> None:
        self._prev: list[str] = []
        self.gaps = 0

    def new_entries(self, lines: list[str]) -> tuple[list[str], bool]:
        """Returns (new entries, gap). On a gap every current entry is
        returned as new (some history was lost to the wrap)."""
        current = [ln for ln in lines if ln.strip()]
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


def parse_ambient_rows(lines: list[str]) -> list[dict[str, Any]]:
    """``AMBIENT|kind|entity_type|entity_id|attr|before|after`` rows from
    DumpAmbient -> compact docs (values stay strings: canonical JSON)."""
    rows: list[dict[str, Any]] = []
    for line in lines:
        parts = line.split("|")
        if len(parts) == 7 and parts[0] == "AMBIENT":
            rows.append({"kind": parts[1], "entity_type": parts[2],
                         "entity_id": parts[3], "attr": parts[4],
                         "before": parts[5], "after": parts[6]})
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
    of the previous game turn are complete by construction)."""

    def __init__(self, adapter: Any, spec: Any) -> None:
        self._adapter = adapter
        self._spec = spec
        self.retries = 0

    async def open_window(self, pid: int) -> None:
        await self._adapter.read_raw(lua_translator.begin_ambient_window(pid))

    async def close_window(self, pid: int) -> list[dict[str, Any]]:
        await self._adapter.read_raw(lua_translator.end_ambient_window(pid))
        dumped = await self._adapter.read_raw(lua_translator.dump_ambient())
        return parse_ambient_rows(dumped)

    async def snapshot(self) -> dict[str, Any]:
        """One census read, digest-bracketed. One retry on drift; a still-
        moving board is reported ``consistent=False`` (never fabricated)."""
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
            doc["census_ms"] = int((time.monotonic() - started) * 1000)
            if consistent:
                return self._capped(doc)
        return self._capped(doc)

    def _capped(self, doc: dict[str, Any]) -> dict[str, Any]:
        """Bound the event payload; digests/counts always survive."""
        if len(json.dumps(doc, sort_keys=True).encode()) <= MAX_SNAPSHOT_BYTES:
            return doc
        doc.pop("units", None)
        doc.pop("cities", None)
        doc["truncated"] = True
        if len(json.dumps(doc, sort_keys=True).encode()) > MAX_SNAPSHOT_BYTES:
            doc.pop("overview", None)
        return doc
