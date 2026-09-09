"""Field-family coverage matrix over a spectator_capture run (NEXT-05).

Streams a run's ``events.jsonl`` ONCE, reads every
``audit == "spectator_world"`` payload (the top-level ``world`` dict —
live_driver.py:834) and publishes the coverage table the sealed
recording qualification requires: which entity classes the world roster
actually NAMED (``world["roster"][i]["kind"]`` — the ONLY naming source),
and which field families each class is observed in. Unobserved classes
stay in the table as zero-count GAP rows — never inferred, never
omitted.

Usage:
    python scripts/capture_coverage.py <run_dir> [-o out.json] [--md out.md]

With neither -o nor --md the JSON goes to stdout. Deterministic: rows
emit in class order (major, city_state, barbarian, free_city) then
family order, and the JSON is dumped with sorted keys.

Attribution rules (mirrored in docs/capture-coverage-20260909.md):
- class-scoped families (roster_identity, economy_players, cities,
  owned_tiles, palette) attribute each member to a class through THAT
  event's own roster pid->kind map; a member whose pid the same event's
  roster does not name is attributed to NO class (never inferred).
- global families (game_era, grid, fog_audit) have no class dimension;
  every class row carries the same aggregation over the events where
  the field is present.
- ``unsupported_or_null`` counts admitted-but-absent fields inside the
  contributing events (the canonical key sets of world_capture.package,
  plus ``level`` — admitted by the PLAYERROW wire, never emitted — and
  ``palette_confirmed: false``).
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

SCHEMA = 1
ENTITY_CLASSES = ("major", "city_state", "barbarian", "free_city")
FIELD_FAMILIES = ("roster_identity", "economy_players", "cities",
                  "owned_tiles", "palette", "game_era", "grid", "fog_audit")
NAMED_FROM = "roster PLAYERROW kind"
RULESET_NOTE = "base_source_catalog — effective ruleset unverified"
UNRECORDED_CONTEXT = "unrecorded"

# The canonical key sets of world_capture.package (Amendment 2 closed
# sets) plus the two admitted-but-never-emitted fields the matrix must
# keep visible: roster ``level`` (UNREAD everywhere probed) and the
# palette confirm flag.
ROSTER_ADMITTED = ("player_id", "civ_name", "leader", "kind", "suzerain",
                   "is_major", "is_barbarian", "alive", "level")
PLAYER_ADMITTED = ("player_id", "civ_name", "gold", "gold_per_turn",
                   "science", "culture", "faith", "upkeep", "era",
                   "researching", "researched", "civics")
CITY_ADMITTED = ("city_id", "owner", "q", "r", "name", "population",
                 "is_capital", "is_major", "hp", "max_hp",
                 "production_queue", "buildings", "districts")
TILE_ADMITTED = ("q", "r", "terrain", "feature", "resource", "improvement",
                 "district", "river", "city")

# verifying_probe names the pinning test + the live read-check script per
# family (both must exist in the repo; test_capture_coverage.py pins that).
SW_PROBE = "tests/test_spectator_world.py; scripts/live_capture_check.py"
HOTSEAT_PROBE = "tests/test_live_hotseat.py; scripts/live_capture_check.py"

FAMILIES: dict[str, dict[str, Any]] = {
    "roster_identity": {
        "accessor": "SPECW|1 roster_read",
        "probe": SW_PROBE,
        "observed_vs_derived":
            "observed; kind DERIVED from IsMajor/IsBarbarian flags; "
            "level UNREAD (absent, never claimed)",
        "scope": "class",
        "context_key": "roster",
        "admitted": ROSTER_ADMITTED,
        "members": lambda world: [(row.get("player_id"), row.get("kind"), row)
                                  for row in world.get("roster", [])],
    },
    "economy_players": {
        "accessor": "OVX|2 overview_read",
        "probe": SW_PROBE,
        "observed_vs_derived":
            "observed (OVX|2 alive-major rows; Amendment-2 key set)",
        "scope": "class",
        "context_key": None,
        "admitted": PLAYER_ADMITTED,
        # kind left unresolved here on purpose: attribution goes through
        # THIS event's roster pid->kind (never an assumed class)
        "members": lambda world: [(row.get("player_id"), None, row)
                                  for row in world.get("players", [])],
    },
    "cities": {
        "accessor": "CITIES|2 cities_read",
        "probe": SW_PROBE,
        "observed_vs_derived": "observed (CITIES|2 extended omniscient read)",
        "scope": "class",
        "context_key": None,
        "admitted": CITY_ADMITTED,
        "members": lambda world: [(row.get("owner"), None, row)
                                  for row in world.get("cities", [])],
    },
    "owned_tiles": {
        "accessor": "SPECW|1 owned_tiles_read",
        "probe": SW_PROBE,
        "observed_vs_derived":
            "observed (spectator scope is omniscient; raw resource rides "
            "WITHOUT the seat tech gate)",
        "scope": "class",
        "context_key": "tiles",
        "admitted": TILE_ADMITTED,
        "members": lambda world: [
            (_pid_of(key), None, entry)
            for key, entries in (world.get("owned_tiles_columns") or {}).items()
            for entry in entries
        ],
    },
    "palette": {
        "accessor": "SPECW|1 palette_read",
        "probe": SW_PROBE,
        "observed_vs_derived":
            "observed ints; packing UNVERIFIED (palette_confirmed false — "
            "viewer keeps the owner-class fallback)",
        "scope": "class",
        "context_key": "palette",
        "admitted": ("primary", "secondary"),
        "members": lambda world: [
            (_pid_of(key), None, colors)
            for key, colors in (world.get("palette") or {}).items()
        ],
    },
    "game_era": {
        "accessor": "OVX|2 overview_read",
        "probe": SW_PROBE,
        "observed_vs_derived": "observed (OVX|2 game era string; global field)",
        "scope": "global",
        "context_key": None,
        "admitted": ("game_era",),
        "present": lambda world: world.get("game_era") is not None,
        "member": lambda world: world,
    },
    "grid": {
        "accessor": "SPECW|1 owned_tiles_read",
        "probe": SW_PROBE,
        "observed_vs_derived": "observed (GRID row of the tiles read; global field)",
        "scope": "global",
        "context_key": "tiles",
        "admitted": ("w", "h"),
        "present": lambda world: bool(world.get("grid")),
        "member": lambda world: world.get("grid") or {},
    },
    "fog_audit": {
        "accessor": "DRIVER fog_audit_for",
        "probe": HOTSEAT_PROBE,
        "observed_vs_derived":
            "derived (driver-side requested vs engine-visible comparison "
            "attached after capture; global field)",
        "scope": "global",
        "context_key": None,
        "admitted": ("requested", "engine_visible", "engine_not_visible",
                     "unavailable", "disagree_coords"),
        "present": lambda world: bool(world.get("fog_audit")),
        "member": lambda world: world.get("fog_audit") or {},
    },
}


def _pid_of(key: Any) -> int | None:
    """owned_tiles_columns/palette keys are stringified pids."""
    try:
        return int(key)
    except (TypeError, ValueError):
        return None


def _absent_count(admitted: tuple[str, ...], member: dict[str, Any]) -> int:
    return sum(1 for key in admitted if key not in (member or {}))


class _Accumulator:
    """Per (class, family) aggregation over one streaming pass."""

    def __init__(self) -> None:
        self.seqs: list[int] = []
        self.tss: list[str] = []
        self.read_ms: list[float] = []
        self.unsupported = 0
        self.context: str | None = None
        self.truncated: dict[str, Any] | None = None

    def add(self, *, seq: int, ts: str, read_ms: float | None,
            context: str | None, truncated: dict[str, Any] | None,
            unsupported: int) -> None:
        self.seqs.append(seq)
        self.tss.append(ts)
        if read_ms is not None:
            self.read_ms.append(read_ms)
        self.context = context if context is not None else self.context
        if truncated is not None:
            self.truncated = truncated
        self.unsupported += unsupported

    def row(self, cls: str, family: str) -> dict[str, Any]:
        desc = FAMILIES[family]
        if self.read_ms:
            stats = {"min": round(min(self.read_ms), 3),
                     "median": round(statistics.median(self.read_ms), 3),
                     "max": round(max(self.read_ms), 3)}
        else:
            stats = {"min": None, "median": None, "max": None}
        return {
            "class": cls,
            "field_family": family,
            "accessor": desc["accessor"],
            "context": self.context or UNRECORDED_CONTEXT,
            "observed_vs_derived": desc["observed_vs_derived"],
            "sampling_time": {"first_ts": self.tss[0] if self.tss else None,
                              "last_ts": self.tss[-1] if self.tss else None},
            "source_cursor": {"seq_min": self.seqs[0] if self.seqs else None,
                              "seq_max": self.seqs[-1] if self.seqs else None,
                              "events": len(self.seqs)},
            "read_ms": stats,
            "unsupported_or_null": self.unsupported,
            "truncated": self.truncated,
            "verifying_probe": desc["probe"],
        }


def build_matrix(run_dir: Path, records: Any) -> dict[str, Any]:
    """One pass over ``records`` (an iterable of parsed event dicts)."""
    classes: dict[str, dict[str, Any]] = {
        cls: {"members": set(), "worlds": 0} for cls in ENTITY_CLASSES}
    cells: dict[tuple[str, str], _Accumulator] = {}
    # board-global families have no class dimension: ONE accumulator per
    # family, reported below for every OBSERVED class only
    global_cells: dict[str, _Accumulator] = {}
    spectator_events = 0
    world_missing = 0

    for record in records:
        if record.get("audit") != "spectator_world":
            continue
        spectator_events += 1
        world = record.get("world")
        if not isinstance(world, dict):
            world_missing += 1
            continue
        seq = int(record.get("seq", 0))
        ts = str(record.get("ts", ""))
        read_ms = world.get("read_ms")
        read_ms = float(read_ms) if isinstance(read_ms, (int, float)) else None
        contexts = world.get("contexts")
        contexts = contexts if isinstance(contexts, dict) else {}
        truncated = world.get("truncated")
        truncated = truncated if isinstance(truncated, dict) else None
        # this event's OWN roster names every class on this board
        pid_kind: dict[int | None, str] = {}
        named_kinds: set[str] = set()
        for row in world.get("roster", []):
            pid, kind = row.get("player_id"), row.get("kind")
            pid_kind[pid] = kind
            if kind in classes:
                classes[kind]["members"].add(pid)
                named_kinds.add(kind)
        for kind in named_kinds:
            classes[kind]["worlds"] += 1

        for family in FIELD_FAMILIES:
            desc = FAMILIES[family]
            if desc["scope"] == "global":
                # present-field gate: an absent global field is a GAP
                # (zero contributing events), never a fabricated row value
                if not desc["present"](world):
                    continue
                global_cells.setdefault(family, _Accumulator()).add(
                    seq=seq, ts=ts, read_ms=read_ms,
                    context=contexts.get(desc["context_key"]),
                    truncated=truncated,
                    unsupported=_absent_count(desc["admitted"],
                                              desc["member"](world)))
                continue
            members = desc["members"](world)
            by_class: dict[str, list[tuple[int | None, dict[str, Any]]]] = {}
            for pid, kind, member in members:
                # attribution ONLY through this event's roster kind — a
                # member the roster does not name is attributed to no class
                resolved = kind if kind in classes else pid_kind.get(pid)
                if resolved in classes:
                    by_class.setdefault(resolved, []).append((pid, member))
            for cls, cls_members in by_class.items():
                unsupported = sum(_absent_count(desc["admitted"], member)
                                  for _pid, member in cls_members)
                if family == "palette":
                    # admitted confirm flag, currently false by design
                    unsupported += 0 if world.get("palette_confirmed") is True else 1
                cells.setdefault((cls, family), _Accumulator()).add(
                    seq=seq, ts=ts, read_ms=read_ms,
                    context=contexts.get(desc["context_key"]),
                    truncated=truncated, unsupported=unsupported)

    rows = []
    for cls in ENTITY_CLASSES:
        for family in FIELD_FAMILIES:
            if (cls, family) in cells:
                rows.append(cells[(cls, family)].row(cls, family))
            elif classes[cls]["members"] and family in global_cells:
                # a class the roster NAMED sees every board-global field
                rows.append(global_cells[family].row(cls, family))
            else:
                rows.append(_empty_row(cls, family))

    summary = _identity(run_dir)
    return {
        "schema": SCHEMA,
        "run_dir": str(run_dir),
        "generated_at": _now_iso(),
        "identity": summary,
        "spectator_world_events": spectator_events,
        "world_payload_missing": world_missing,
        "entity_classes": [
            {"class": cls, "named_from": NAMED_FROM,
             "observed_members": len(classes[cls]["members"]),
             "observed_in_worlds": classes[cls]["worlds"]}
            for cls in ENTITY_CLASSES
        ],
        "rows": rows,
    }


def _empty_row(cls: str, family: str) -> dict[str, Any]:
    """The GAP row: the family/class cell observed nowhere — present, zeroed."""
    desc = FAMILIES[family]
    return {
        "class": cls,
        "field_family": family,
        "accessor": desc["accessor"],
        "context": UNRECORDED_CONTEXT,
        "observed_vs_derived": desc["observed_vs_derived"],
        "sampling_time": {"first_ts": None, "last_ts": None},
        "source_cursor": {"seq_min": None, "seq_max": None, "events": 0},
        "read_ms": {"min": None, "median": None, "max": None},
        "unsupported_or_null": 0,
        "truncated": None,
        "verifying_probe": desc["probe"],
    }


def _identity(run_dir: Path) -> dict[str, Any]:
    """Provenance from the run's own summary (empty when absent — the
    publisher never derives git identity itself)."""
    identity = {"commit": "", "mod_sha256": "", "ruleset": RULESET_NOTE}
    summary_path = run_dir / "summary.json"
    if summary_path.exists():
        try:
            recorded = (json.loads(summary_path.read_text(encoding="utf-8"))
                        .get("identity") or {})
        except (OSError, ValueError):
            return identity
        identity["commit"] = str(recorded.get("commit") or "")
        identity["mod_sha256"] = str(recorded.get("mod_sha256") or "")
    return identity


def _now_iso() -> str:
    from datetime import UTC, datetime
    return datetime.now(UTC).isoformat(timespec="seconds")


# -- markdown ------------------------------------------------------------------


def render_markdown(matrix: dict[str, Any]) -> str:
    lines = [
        f"# Capture coverage — {matrix['run_dir']}",
        "",
        f"Generated {matrix['generated_at']} · "
        f"spectator_world events: {matrix['spectator_world_events']}"
        + (f" · world payload MISSING on {matrix['world_payload_missing']}"
           if matrix["world_payload_missing"] else ""),
        "",
        f"Identity: commit `{matrix['identity']['commit'] or '(absent)'}` · "
        f"mod_sha256 `{matrix['identity']['mod_sha256'] or '(absent)'}` · "
        f"ruleset: {matrix['identity']['ruleset']}",
        "",
        "## Entity classes (named from roster PLAYERROW kind)",
        "",
        "| class | named_from | observed_members | observed_in_worlds |",
        "|---|---|---|---|",
    ]
    for entry in matrix["entity_classes"]:
        lines.append(
            f"| {entry['class']} | {entry['named_from']} "
            f"| {entry['observed_members']} | {entry['observed_in_worlds']} |")
    lines += [
        "",
        "## Coverage matrix (class × field_family)",
        "",
        "| class | family | accessor | context | observed/derived | "
        "events (seq) | first_ts | last_ts | read_ms min/median/max | "
        "unsupported_or_null | truncated |",
        "|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for row in matrix["rows"]:
        cursor = row["source_cursor"]
        span = (f"{cursor['events']} "
                f"({cursor['seq_min']}–{cursor['seq_max']})"
                if cursor["events"] else "0")
        times = row["sampling_time"]
        stats = row["read_ms"]
        lines.append(
            f"| {row['class']} | {row['field_family']} | {row['accessor']} "
            f"| {row['context']} | {row['observed_vs_derived']} | {span} "
            f"| {times['first_ts'] or '—'} | {times['last_ts'] or '—'} "
            f"| {stats['min']}/{stats['median']}/{stats['max']} "
            f"| {row['unsupported_or_null']} "
            f"| {_short_truncated(row['truncated'])} |")
    lines += ["", "## GAPS", ""]
    lines += _gap_lines(matrix)
    return "\n".join(lines) + "\n"


def _short_truncated(truncated: dict[str, Any] | None) -> str:
    if not truncated:
        return "—"
    return "; ".join(f"{key}={value}" for key, value in sorted(truncated.items()))


def _gap_lines(matrix: dict[str, Any]) -> list[str]:
    gaps = []
    unobserved = [e["class"] for e in matrix["entity_classes"]
                  if not e["observed_members"]]
    if unobserved:
        gaps.append(
            "- unobserved entity classes (zero-count GAP rows above, never "
            f"inferred): {', '.join(unobserved)}")
    else:
        gaps.append("- unobserved entity classes: none — every class named "
                    "by at least one roster")
    palette_rows = [r for r in matrix["rows"]
                    if r["field_family"] == "palette" and r["source_cursor"]["events"]]
    if any(r["unsupported_or_null"] for r in palette_rows):
        gaps.append(
            "- `palette_confirmed: false` — standing gap until the "
            "live_capture_check follow-up verifies the ABGR/ARGB packing "
            "against a known leader colour; the viewer keeps the owner-class "
            "fallback")
    gaps.append(
        "- spectate-phase world carrier UNVERIFIED — this matrix covers the "
        "hotseat `spectator_world` audit only; the spectate snapshot's "
        "world block (read-transport-only, palette-less) has no live proof")
    gaps.append(f"- ruleset: {RULESET_NOTE}")
    return gaps


# -- CLI -----------------------------------------------------------------------


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("run_dir", type=Path)
    ap.add_argument("-o", "--out", type=Path, default=None,
                    help="coverage_matrix.json path (default: stdout)")
    ap.add_argument("--md", type=Path, default=None,
                    help="coverage-matrix.md path")
    opts = ap.parse_args()
    with (opts.run_dir / "events.jsonl").open(encoding="utf-8") as fh:
        matrix = build_matrix(opts.run_dir,
                              (json.loads(line) for line in fh if line.strip()))
    payload = json.dumps(matrix, sort_keys=True, indent=2)
    if opts.out is None:
        print(payload)
    else:
        opts.out.write_text(payload + "\n", encoding="utf-8")
    if opts.md is not None:
        opts.md.write_text(render_markdown(matrix), encoding="utf-8")


if __name__ == "__main__":
    main()
