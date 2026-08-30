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


def parse_handshake(lines: list[str]) -> dict[str, Any]:
    """Typed PuppeteerMod handshake doc, FAIL-CLOSED: a missing or false
    capability is False, never defaulted True — the adapter refuses to
    drive a live match unless freeze AND ledger are explicitly true."""
    parsed = parse_kv_lines(lines)
    present = parsed.get("MOD_PRESENT") is True
    return {
        "present": present,
        "mod_version": parsed.get("MOD_VERSION") if present else None,
        "supports_freeze": present and parsed.get("SUPPORTS_FREEZE") is True,
        "supports_ledger": present and parsed.get("SUPPORTS_LEDGER") is True,
        "supports_digest": present and parsed.get("SUPPORTS_DIGEST") is True,
    }
