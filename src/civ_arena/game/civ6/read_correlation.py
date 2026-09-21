"""Response correlation for tuner reads: a unique end-marker per command.

hundred-20260920b died at round 7 when a lost UNITS read (empty at 2311ms
on the Moonlight jitter band) was delivered LATE into the next command's
CITIES read — the response stream desynced by one and the cities parser
correctly failed closed on ``non-city row: 'UNITS|1'``. The connection's
0.1s stale-drain cannot catch a response that arrives seconds later.

Every read script is decorated with ``print("ARENA_READ|<token>")``
inserted BEFORE the script's own ``---END---`` sentinel (the collector
stops at the sentinel — a marker after it would never be read). The
extractor then:

- drops every row through the LAST foreign marker before mine (a late
  previous response arrives carrying ITS token and is skipped, however
  late, however many piled up);
- returns the rows between that point and MY marker;
- returns None when MY marker never arrived (a lost read) — the proxy
  retries with a fresh token, bounded, then reports [] (the "read lost"
  signal every caller already handles).

Scripts without a findable sentinel pass through undecorated — their
responses return unchanged (fail-soft, pre-correlation behavior).
"""

from __future__ import annotations

import asyncio
import re
import uuid
from typing import Any

MARKER_PREFIX = "ARENA_READ|"
# The marker print decorate() injects (also the test-fake detector).
_MARKER_IN_LUA = re.compile(r'print\("ARENA_READ\|([0-9a-f]{32})"\)')


def echoed_response(lua: str, rows: list[str]) -> list[str]:
    """What a correlation-aware endpoint returns for ``lua``.

    Shared by the fakes and the test stubs so every canned-response
    stand-in mirrors the real tuner: echo the injected marker on real
    rows; a lost read stays empty (no marker ever arrives).
    """
    marker = _MARKER_IN_LUA.search(lua)
    if marker is None or not rows:
        return rows
    return echo_marker(rows, marker.group(1))
# The sentinel statement forms lua_translator and handoff emit.
_SENTINEL = re.compile(r"print\((['\"])---END---\1\)\s*$", re.M)

_ATTEMPTS = 3
_RETRY_SLEEP_S = 0.4


def decorate(lua: str, token: str) -> str | None:
    """Prefix the script with the marker print.

    v2 (overnight-20260920's lesson): the mod's own functions print
    ``---END---`` rows INSIDE their output (Puppeteer.Handshake, SetPuppet,
    Status...), and the collector stops at the first sentinel row — an
    END-placed marker never arrives on exactly the reads that matter most
    (the S4 handshake died present:False off a correlation-exhausted []).
    The marker goes FIRST: data = rows after it up to the first sentinel.

    None when the script has no sentinel statement (caller runs it
    undecorated and passes the response through).
    """
    if not _SENTINEL.search(lua):
        return None
    marker = f'print("{MARKER_PREFIX}{token}")'
    body = lua.lstrip("\n")
    if body.startswith("--"):  # keep leading comments above the print
        first_code = next((i for i, line in enumerate(lua.splitlines(), 1)
                           if line.strip() and not line.lstrip().startswith("--")), 1)
        lines = lua.splitlines(keepends=True)
        return "".join(lines[:first_code - 1]) + marker + "\n" + "".join(lines[first_code - 1:])
    return marker + "\n" + lua


def echo_marker(rows: list[str], token: str) -> list[str]:
    """Fakes: where the injected marker row goes in a response.

    v2: the marker arrives as the FIRST data row (the print leads the
    script), before any embedded sentinel the fake's rows carry.
    """
    return [f"{MARKER_PREFIX}{token}", *rows]


def extract_correlated(rows: list[str], token: str) -> list[str] | None:
    """The data rows belonging to THIS token, or None if its marker
    (the whole response) never arrived.

    rows-after-my-marker, stopping at the first ``---END---`` row (the
    collector's own stop — the mod's embedded sentinels end the DATA
    legitimately). Rows before my marker are skipped: they belong to a
    late previous response.
    """
    marker = f"{MARKER_PREFIX}{token}"
    if marker not in rows:
        return None
    start = rows.index(marker) + 1
    data: list[str] = []
    for row in rows[start:]:
        if row.strip() == "---END---":
            break
        data.append(row)
    return data


class CorrelatedConnection:
    """Proxy over GameConnection decorating every read/write response.

    Same surface the adapter already uses (execute_read / execute_write /
    attributes forwarded via __getattr__), so it drops in at the one
    construction point. human_handoff bypasses this layer deliberately —
    it carries its own token receipts.
    """

    def __init__(self, inner: Any) -> None:
        self._inner = inner

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    async def execute_read(self, lua_code: str, timeout: float = 5.0) -> list[str]:
        return await self._correlated(self._inner.execute_read, lua_code, timeout)

    async def execute_write(self, lua_code: str, timeout: float = 5.0) -> list[str]:
        return await self._correlated(self._inner.execute_write, lua_code, timeout)

    async def _correlated(self, execute, lua_code: str, timeout: float) -> list[str]:
        for attempt in range(_ATTEMPTS):
            token = uuid.uuid4().hex
            decorated = decorate(lua_code, token)
            if decorated is None:  # no sentinel — run exactly as before
                return await execute(lua_code, timeout)
            rows = await execute(decorated, timeout)
            data = extract_correlated(list(rows), token)
            if data is not None:
                return data
            if attempt + 1 < _ATTEMPTS:
                # A late original may land during the retry's window; the
                # next extract skips it by its foreign marker.
                await asyncio.sleep(_RETRY_SLEEP_S)
        # Deeply lost — report the empty read, never foreign rows.
        return []
