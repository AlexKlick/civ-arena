"""Pure Lua-output→semantic parsing (pipe-delimited KEY|value lines)."""

from __future__ import annotations

import re
from typing import Any

# canonical ints only: a token that LOOKS numeric by any other spelling
# (12.5, .5, 1., 1e3, nan, inf, +7) fails closed (Codex P2-9)
_PLAIN_INT = re.compile(r"^-?\d+$")
_NUMERICISH = re.compile(r"^[-+0-9.eE_]+$")


def _split_lines(lines: list[str]) -> list[str]:
    """One print() of a multi-line string arrives as ONE payload with
    embedded newlines (live-learned on the first attach) — flatten every
    incoming element before parsing."""
    out: list[str] = []
    for raw in lines:
        out.extend(raw.splitlines())
    return out


def parse_kv_lines(lines: list[str]) -> dict[str, Any]:
    """Parse ``KEY|value`` lines into a dict (last write wins).

    ``KEY|a|b`` rows (e.g. ``PLAYER|0|CIVILIZATION_ROME``) are collected
    under ``KEY`` as a list of tuples-as-lists.
    """
    out: dict[str, Any] = {}
    for line in _split_lines(lines):
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


def _coerce_strict(value: str) -> Any:
    """Canonical-int tripwire: any numeric-looking token that is not a
    plain integer fails LOUDLY here, before it can ride an event log
    canonical() would reject — or worse, slip through as a string."""
    if _PLAIN_INT.match(value):
        return int(value)
    if _NUMERICISH.match(value) or value.lower() in ("nan", "inf", "-inf"):
        raise ValueError(f"non-canonical number on the wire: {value!r}")
    return _coerce(value)


def parse_ledger_lines(lines: list[str]) -> list[dict[str, Any]]:
    """LEDGER|/AMBIENT| rows -> MutationRecord docs.

    Row shape (both ledgers): ``<PREFIX>|kind|entity_type|entity_id|attr|
    before|after``; the prefix sets ``origin`` ("ledger"=undeclared actual,
    "ambient"=declared manifest). Malformed rows raise — a torn row must
    never silently shrink the watchdog's actual multiset.
    """
    docs: list[dict[str, Any]] = []
    for line in _split_lines(lines):
        line = line.strip()
        if not line or line.startswith("---END---"):
            continue
        prefix, _, rest = line.partition("|")
        if prefix not in ("LEDGER", "AMBIENT"):
            raise ValueError(f"non-ledger row in ledger dump: {line!r}")
        parts = rest.split("|")
        if len(parts) != 6:
            raise ValueError(f"malformed {prefix} row (want 6 fields): {line!r}")
        kind, entity_type, entity_id, attr, before, after = parts
        docs.append({
            "kind": kind,
            "entity_type": entity_type,
            "entity_id": entity_id,
            "attr": attr,
            "before": _coerce_strict(before),
            "after": _coerce_strict(after),
            "origin": prefix.lower(),
        })
    return docs


def parse_digest(lines: list[str]) -> str:
    """The DIGEST| payload — the live state-hash source text."""
    for line in _split_lines(lines):
        line = line.strip()
        if line.startswith("DIGEST|"):
            return line[len("DIGEST|"):]
    raise ValueError(f"no DIGEST| row in {lines!r}")
