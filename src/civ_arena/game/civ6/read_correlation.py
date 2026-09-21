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
# The sentinel statement forms lua_translator and handoff emit.
_SENTINEL = re.compile(r"print\((['\"])---END---\1\)\s*$", re.M)

_ATTEMPTS = 3
_RETRY_SLEEP_S = 0.4


def decorate(lua: str, token: str) -> str | None:
    """Insert the marker print before the script's final ---END---.

    None when no sentinel statement is findable (caller runs the script
    undecorated and passes the response through).
    """
    matches = list(_SENTINEL.finditer(lua))
    if not matches:
        return None
    match = matches[-1]
    marker = f'print("{MARKER_PREFIX}{token}")'
    return lua[:match.start()] + marker + "\n" + lua[match.start():]


def echo_marker(rows: list[str], token: str) -> list[str]:
    """Fakes: where the injected marker row goes in a response.

    The real tuner executes the injected print BEFORE the script's
    sentinel, so the marker arrives as the last DATA row. The fakes'
    convention is a trailing embedded '---END---' row (the collector
    stops at the first sentinel — anything after it never arrives), so
    the marker is inserted just before a trailing sentinel and appended
    when there is none.
    """
    marker = f"{MARKER_PREFIX}{token}"
    if rows and rows[-1].strip() == "---END---":
        return [*rows[:-1], marker, rows[-1]]
    return [*rows, marker]


def extract_correlated(rows: list[str], token: str) -> list[str] | None:
    """The data rows belonging to THIS token, or None if its marker
    (the whole response) never arrived."""
    marker = f"{MARKER_PREFIX}{token}"
    if marker not in rows:
        return None
    end = rows.index(marker)
    start = 0
    for i in range(end):
        if rows[i].startswith(MARKER_PREFIX) and rows[i] != marker:
            start = i + 1  # a late foreign response — skip through it
    return rows[start:end]


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
