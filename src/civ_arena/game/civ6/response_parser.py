"""Pure Lua-output→semantic parsing (pipe-delimited KEY|value lines)."""

from __future__ import annotations

from typing import Any


def parse_kv_lines(lines: list[str]) -> dict[str, Any]:
    """Parse ``KEY|value`` lines into a dict (last write wins).

    ``KEY|a|b`` rows (e.g. ``PLAYER|0|CIVILIZATION_ROME``) are collected
    under ``KEY`` as a list of tuples-as-lists.
    """
    out: dict[str, Any] = {}
    for line in lines:
        line = line.strip()
        if not line or line == "---END---":
            continue
        parts = line.split("|")
        if len(parts) == 2:
            out[parts[0]] = _coerce(parts[1])
        elif len(parts) > 2:
            out.setdefault(parts[0], []).append(parts[1:])
    return out


def _coerce(value: str) -> Any:
    if value == "true":
        return True
    if value == "false":
        return False
    try:
        return int(value)
    except ValueError:
        return value
