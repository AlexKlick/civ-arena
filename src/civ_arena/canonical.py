"""Canonical serialization + hashing — the determinism foundation.

Rules (design decision 6):
- One canonical JSON form: sorted keys, compact separators, ensure_ascii.
- Allowed state types: int, bool, str, list, dict. Floats are rejected with
  TypeError (the sim uses integer arithmetic only); tuples/sets are rejected
  (convert to list first). None is allowed in *records* (event envelopes,
  args) but never appears inside sim state documents.
- ``schema != 1`` hard-errors — forward-compat tripwire.
- ``ts``/``duration_ms`` live only in the event envelope and are excluded
  from every replay-relevant hash.
"""

from __future__ import annotations

import hashlib
import json
import os
import random
import tempfile
from pathlib import Path
from typing import Any

SCHEMA = 1


class CanonicalError(TypeError):
    """Raised when a document cannot be canonically serialized."""


def _validate(node: Any, path: str) -> None:
    if node is None or isinstance(node, (bool, int, str)):
        return
    if isinstance(node, float):
        raise CanonicalError(f"float at {path}: state must use integer arithmetic")
    if isinstance(node, (tuple, set, frozenset)):
        raise CanonicalError(f"{type(node).__name__} at {path}: convert to list")
    if isinstance(node, list):
        for i, item in enumerate(node):
            _validate(item, f"{path}[{i}]")
        return
    if isinstance(node, dict):
        for key, value in node.items():
            if not isinstance(key, str):
                raise CanonicalError(f"non-str dict key at {path}: {key!r}")
            _validate(value, f"{path}.{key}")
        return
    raise CanonicalError(f"unsupported type {type(node).__name__} at {path}")


def canonical(doc: Any) -> str:
    """Canonical JSON text. Deterministic across runs and processes."""
    _validate(doc, "$")
    return json.dumps(doc, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def args_digest(args: Any) -> str:
    return sha256_hex(canonical(args))


def atomic_write_text(path: Path | str, text: str, *,
                      fsync: bool = False) -> None:
    """Atomic tmp+replace that cannot follow a planted link (Codex M19c
    C1, class fix): the temp file comes from tempfile.mkstemp, which is
    O_EXCL — a symlink or hardlink planted at any PREDICTABLE tmp name is
    never opened for writing, because mkstemp only succeeds on a fresh
    name it invented; the destination is then swapped in atomically by
    os.replace. The pre-fix idiom (fixed ``<out>.tmp`` + write_text)
    opened the planted path itself and truncated the link target BEFORE
    the replace ever ran. ``fsync=True`` keeps the journal's durability
    semantics (flush + fsync before the replace)."""
    dest = Path(path)
    fd, tmp_name = tempfile.mkstemp(dir=dest.parent, prefix=dest.name + ".",
                                    suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            if fsync:
                fh.flush()
                os.fsync(fh.fileno())
        os.replace(tmp, dest)
    except BaseException:
        tmp.unlink(missing_ok=True)  # never leave the temp behind on failure
        raise


def assert_schema(n: int) -> None:
    if n != SCHEMA:
        raise CanonicalError(f"schema version {n} != {SCHEMA}: refusing to proceed")


def state_hash(sim_doc: Any) -> str:
    """Hash of the sim state document (used for before/after records and diffs)."""
    assert_schema(sim_doc.get("schema", SCHEMA) if isinstance(sim_doc, dict) else SCHEMA)
    return sha256_hex(canonical({"schema": SCHEMA, "sim": sim_doc}))


def checkpoint_hash(sim_doc: Any, rng_states: dict[str, list], coordinator_state: Any) -> str:
    """Hash over sim + RNG + coordinator — the resume-equality assertion basis."""
    return sha256_hex(
        canonical(
            {
                "schema": SCHEMA,
                "sim": sim_doc,
                "rng": rng_states,
                "coordinator": coordinator_state,
            }
        )
    )


def log_prefix_hash(records: list[dict[str, Any]]) -> str:
    """Hash over the canonical form of the first N event records (order = seq).

    Envelope fields (ts, duration_ms, game_instance_id) are stripped before
    hashing — the log prefix identity must hold across processes and restarts.
    """
    envelope = ("ts", "duration_ms", "game_instance_id")
    stripped = [
        {k: v for k, v in rec.items() if k not in envelope} for rec in records
    ]
    return sha256_hex(canonical(stripped))


def rng_to_doc(rng: random.Random) -> list[Any]:
    """JSON-able form of a random.Random state tuple (which contains None)."""
    version, inner, gauss = rng.getstate()
    return [version, list(inner), gauss if gauss is not None else 0]


def rng_from_doc(doc: list[Any]) -> random.Random:
    rng = random.Random()
    version, inner, gauss = doc[0], tuple(doc[1]), doc[2] or None
    rng.setstate((version, inner, gauss))
    return rng
