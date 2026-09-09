"""Recording qualification harness — dataset evidence card §6 steps 2-4 + summary.

Turns one captured run directory into ONE machine verdict by running, unchanged,
the capture audit, the viewer, and the exporter, then assembling the counters
the evidence card's result table needs. Targets the DRIVEN dispatch-hotseat
recording path (the shape §6 qualifies: driven seats plus spectator_world
captures); a spectate-phase run fails the audit's row contract.

Stages (each appends human-readable failures to one shared list; a stage never
raises past its boundary except usage/IO errors):

- stage_audit      validate_run.validate + spectator_world row contract
                   (after_seat == [-1] + [0, 1] * rounds over the captures
                   whose world ENVELOPE holds up — schema/roster/read_ms,
                   inner-vs-outer after_seat, turn order and a preceding
                   completed_seat_turn row), the spectator_world_failed
                   census, the roster-kind census (a kind the wire parser
                   never admits fails the verdict) and the run identity (a
                   dirty tree fails).
- stage_viewer     in-process DashboardStore.load — status must be 'completed'
                   and no warning may fall outside the benign size-cap
                   baseline.
- stage_export     export_dataset.export/.write UNCHANGED (output outside the
                   run dir), then ref integrity over the exported samples, the
                   manifest's source-digest binding and the flag histogram.
- stage_summarize  qualification.json (schema 1) and, optionally, a markdown
                   report skeleton whose human cells stay for the operator.

Usage:
    python scripts/qualify_recording.py <run_dir> --rounds N [--allow-fake]
        [--baseline-warnings configs/qual-warning-baseline.json]
        [--export-out PATH] [--coverage PATH] [--report PATH] [--json PATH]

Exit codes:
    0  verdict PASS
    1  any stage failure (verdict FAIL; qualification.json is still written)
    2  usage/IO error: missing run dir, unreadable artifact, malformed
       baseline file (or one naming a warning outside the code-owned
       benign-cap allowlist), bad --rounds, an output path inside the run
       dir / aliasing its evidence / colliding with another output, or a
       failed publication

The harness NEVER mutates the run dir: every output defaults to a sibling
"<run>.qual/" directory, and any explicit output inside the run dir — or
merely sharing an inode with one of its artifacts — is refused before any
stage runs. No HTTP server is started; the viewer runs in-process.

Threat model (be precise about what is and is not guaranteed):

    ASSUMED: a single, non-adversarial invocation by one operator, over
    stable output directories that contain no symlinks on their parent
    chains. This is a single-user research harness, not a multi-tenant
    service.

    GUARANTEED under that model: the run directory is never written; an
    output that aliases run evidence, the coverage input or another output
    is refused before any stage runs; every artifact is generated into
    invocation-owned staging and nothing reaches a destination until all of
    them exist; the previous generation's success documents are invalidated
    before any artifact they reference is replaced, so an interrupted
    publication is DETECTABLE (missing verdict) rather than a stale PASS
    over swapped bytes; and no traceback escapes — usage/IO faults exit 2.

    NOT GUARANTEED: publication is four os.replace calls, not one
    transaction (CAR-003 contract §5.3) — an interruption between members
    leaves a detectable partial generation, never a success. A local actor
    who can mutate the output tree DURING a run is out of scope: the
    exclusive publication lock (keyed on the resolved verdict path) and the
    refusal of symlinked output parents are mitigations for the accidental
    cases, not defences against a deliberate mid-run retarget. Two
    invocations that share only SOME outputs while naming different verdict
    documents are not serialized. A lockfile left by a killed process is
    stale and must be removed by the operator; the error names it.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import shutil
import sys
import tempfile
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from civ_arena import dashboard_compare  # noqa: E402
from civ_arena.canonical import atomic_write_text  # noqa: E402
from civ_arena.dashboard import DashboardStore  # noqa: E402
from civ_arena.game.civ6.validate_run import validate  # noqa: E402
from civ_arena.minimap import validate_world  # noqa: E402
from civ_arena.research.export_dataset import export, write  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
DEFAULT_BASELINE = REPO / "configs" / "qual-warning-baseline.json"
ENTITY_CLASS_SOURCE = "roster PLAYERROW kind — named, never inferred"
# dataset evidence card §6: the result table's human cells stay human.
PENDING = "*(pending operator fill)*"
MAX_FAILED_REFS = 10
VIEWER_CHECK = "in-process DashboardStore.load"
# the consumed artifacts a derived output must never alias (the exporter
# guards its own two targets; these are ours) plus the run dir itself
RUN_EVIDENCE = ("events.jsonl", "summary.json", "llm_costs.jsonl")
# The roster kinds world_capture.parse_roster admits off the PLAYERROW wire.
# Anything else is a wire violation — never a class to infer.
WIRE_ROSTER_KINDS = ("major", "city_state", "barbarian")
# The producer's own envelope for a capture (live_driver.py:831-834): a
# HEARTBEAT routed to the spectator scope. Codex r2 finding 2: a capture
# routed anywhere else is SKIPPED by dashboard_compare.spectator_world_
# records(), so the viewer never validates it — the audit must therefore
# refuse it rather than count a world nothing ever inspected.
SPECTATOR_EVENT_KIND = "HEARTBEAT"
SPECTATOR_SCOPE = "spectator"
# The keys world_capture.package emits UNCONDITIONALLY, with the type each
# carries. Codex r2 finding 2: checking only the keys that happen to be
# present admits a four-key stub; completeness is the contract.
REQUIRED_WORLD_TYPES = (
    ("schema", int), ("after_seat", int), ("contexts", dict), ("grid", dict),
    ("roster", list), ("players", list), ("cities", list), ("fog_audit", dict),
    ("palette_confirmed", bool), ("truncated", dict), ("read_ms", (int, float)))
# Emitted conditionally: `palette` only with an ingame palette, `game_era`
# only when read, `owned_tiles_columns` unless _bounded_json dropped the
# territory block — and that drop is RECORDED as truncated.world
# (world_capture.py:491-508), so absence without the record is a violation.
OPTIONAL_WORLD_TYPES = (("palette", dict), ("game_era", str),
                        ("owned_tiles_columns", dict))
# The closed top-level key set of world_capture.package — the hotseat world
# carrier this harness qualifies.
WORLD_PACKAGE_KEYS = frozenset(
    {key for key, _ in REQUIRED_WORLD_TYPES}
    | {key for key, _ in OPTIONAL_WORLD_TYPES})
WORLD_CONTEXT_KEYS = frozenset({"roster", "tiles", "palette"})
WORLD_GRID_KEYS = frozenset({"w", "h"})
MAX_REPORTED_ROSTER_FAULTS = 5
# The ONLY warnings a baseline may whitelist: the size-cap constants
# dashboard_compare owns plus the cap strings dashboard.py emits inline. A
# supplied baseline cannot whitelist a real warning by naming it — the
# allowlist is CODE-owned; configs/qual-warning-baseline.json is only the
# shipped default spelling of the same set.
COMPARE_CAP_WARNINGS = (
    dashboard_compare.WORLD_RECORDS_CAPPED,
    dashboard_compare.WORLD_PLAYERS_CAPPED,
    dashboard_compare.RESEARCH_CAPPED,
    dashboard_compare.HISTORY_CAPPED,
    dashboard_compare.OPTIONS_CAPPED,
    dashboard_compare.ROWS_CAPPED,
    dashboard_compare.MARKS_CAPPED,
    dashboard_compare.ECONOMY_CAPPED,
    dashboard_compare.ROLES_CAPPED,
    dashboard_compare.DIRECTIVE_CAPPED)
# The six cap strings dashboard.py emits inline (no module constant to
# import; every one is asserted present in that source by the tests).
DASHBOARD_CAP_WARNINGS = (
    "Event log exceeds the read limit; this view is incomplete.",
    "Event count limit reached; this view is incomplete.",
    "Per-turn display limit reached; older items omitted.",
    "Turn display limit reached; older turns omitted.",
    "Response size limit reached; older turns omitted.",
    "Response size limit reached; older comparison rows omitted.")
ALLOWED_BASELINE_WARNINGS = frozenset(COMPARE_CAP_WARNINGS
                                      + DASHBOARD_CAP_WARNINGS)


class QualificationError(Exception):
    """Usage/IO failure: the harness cannot even read its inputs."""


def diff_warnings(warnings: list, baseline: list[str]) -> tuple[list, list]:
    """Split observed viewer warnings into (baseline_matched, new)."""
    matched = [w for w in warnings if w in baseline]
    new = [w for w in warnings if w not in baseline]
    return matched, new


# -- stage 1: capture audit -----------------------------------------------------


def _typed(value: object, types: type | tuple[type, ...]) -> bool:
    """isinstance with the bool/int trap closed in both directions."""
    if types is bool:
        return type(value) is bool
    return isinstance(value, types) and not isinstance(value, bool)


def world_envelope_reasons(record: dict, prev_turn: int | None) -> list[str]:
    """Every way ONE spectator_world capture's envelope violates the producer
    contract, checked COMPLETELY here and independently of what the viewer
    chooses to look at.

    Codex r1 finding 1: neither validate_run nor the viewer inspects these
    payloads, so the row contract must not be satisfiable by an envelope the
    producer could never have emitted. Codex r2 finding 2: the viewer's
    inspection is not even reachable for a capture routed off the spectator
    scope (dashboard_compare.spectator_world_records() skips it silently), and
    a guard that only type-checks the keys it happens to find admits a
    four-key stub. So: the producer's event kind and routing, then EVERY key
    world_capture.package emits unconditionally, then the payload's own
    after_seat and the capture's turn order.
    """
    reasons: list[str] = []
    kind = record.get("kind")
    if kind != SPECTATOR_EVENT_KIND:
        reasons.append(f"event kind {kind!r} is not {SPECTATOR_EVENT_KIND!r}; "
                       "the producer writes a capture as a HEARTBEAT")
    scope = record.get("visibility_scope")
    if scope != SPECTATOR_SCOPE:
        reasons.append(f"visibility_scope {scope!r} is not {SPECTATOR_SCOPE!r}; "
                       "the viewer never inspects a non-spectator capture, so "
                       "it can never satisfy the row contract")
    world = record.get("world")
    if not isinstance(world, dict):
        reasons.append(f"world payload is not a dict ({type(world).__name__})")
        return reasons
    # -- the three r1 payload checks keep their diagnoses ----------------------
    schema = world.get("schema")
    if not _typed(schema, int) or schema != 1:
        reasons.append(f"world schema is not 1 (got {schema!r})")
    if not isinstance(world.get("roster"), list):
        reasons.append("world roster is not a list "
                       f"({type(world.get('roster')).__name__})")
    if "read_ms" not in world:
        reasons.append("world read_ms is absent")
    elif not _typed(world["read_ms"], (int, float)):
        reasons.append("world read_ms is not a number "
                       f"({type(world['read_ms']).__name__})")
    # -- r2: completeness over every unconditional package key -----------------
    diagnosed = {"schema", "roster", "read_ms"}
    missing, mistyped = [], []
    for key, types in REQUIRED_WORLD_TYPES:
        if key in diagnosed:
            continue
        if key not in world:
            missing.append(key)
        elif not _typed(world[key], types):
            mistyped.append(f"{key} is {type(world[key]).__name__}")
    truncated = world.get("truncated")
    world_truncated = isinstance(truncated, dict) and truncated.get("world") is True
    if "owned_tiles_columns" not in world:
        if not world_truncated:
            missing.append("owned_tiles_columns")
    elif not _typed(world["owned_tiles_columns"], dict):
        mistyped.append("owned_tiles_columns is "
                        f"{type(world['owned_tiles_columns']).__name__}")
    for key, types in OPTIONAL_WORLD_TYPES:
        if key != "owned_tiles_columns" and key in world \
                and not _typed(world[key], types):
            mistyped.append(f"{key} is {type(world[key]).__name__}")
    if missing:
        reasons.append("world payload is missing key(s) world_capture.package "
                       f"emits unconditionally: {sorted(missing)}")
    if mistyped:
        reasons.append(f"world payload key(s) mistyped: {sorted(mistyped)}")
    contexts = world.get("contexts")
    if isinstance(contexts, dict) and set(contexts) != WORLD_CONTEXT_KEYS:
        reasons.append(f"world contexts keys {sorted(contexts)} are not "
                       f"{sorted(WORLD_CONTEXT_KEYS)}")
    grid = world.get("grid")
    if isinstance(grid, dict) and set(grid) != WORLD_GRID_KEYS:
        reasons.append(f"world grid keys {sorted(grid)} are not "
                       f"{sorted(WORLD_GRID_KEYS)}")
    unknown = sorted(set(world) - WORLD_PACKAGE_KEYS)
    if unknown:
        reasons.append("world payload carries key(s) outside the world package "
                       f"key set: {unknown}")
    # -- r2 finding 5: roster rows are typed before anything indexes them ------
    roster = world.get("roster")
    if isinstance(roster, list):
        faults: list[str] = []
        for index, row in enumerate(roster):
            if not isinstance(row, dict):
                faults.append(f"[{index}] is {type(row).__name__}")
                continue
            pid = row.get("player_id")
            if type(pid) is not int or pid < 0:
                faults.append(f"[{index}] player_id {pid!r}")
            row_kind = row.get("kind")
            if row_kind is not None and not isinstance(row_kind, str):
                faults.append(f"[{index}] kind {row_kind!r}")
        if faults:
            reasons.append("roster row(s) malformed: "
                           f"{faults[:MAX_REPORTED_ROSTER_FAULTS]}")
    # Codex r3 finding 3: the LAST viewer-routing bypass. Route and turn are
    # both filters on dashboard_compare's side, so the audit runs the viewer's
    # own validator here, directly, over every world it is about to count —
    # never relying on the viewer having chosen to look.
    try:
        validate_world(world)
    except (ValueError, TypeError, RecursionError) as exc:
        reasons.append(f"validate_world rejects the payload: "
                       f"{type(exc).__name__}: {exc}")
    outer, turn = record.get("after_seat"), record.get("turn")
    inner = world.get("after_seat")
    if inner != outer:
        reasons.append(f"world payload after_seat {inner!r} does not match the "
                       f"event after_seat {outer!r}")
    if type(turn) is not int:
        reasons.append(f"capture turn {turn!r} is not an integer")
    elif turn < 0:
        reasons.append(f"capture turn {turn} is negative; dashboard_compare "
                       "drops turn < 0 before validate_world (:561), so a "
                       "negative-turn capture can never be viewer-validated "
                       "and must not count. NOTE: the baseline's -1 sentinel "
                       "is its after_seat, not its turn (live_driver.py:995 "
                       "passes the engine turn mirror)")
    elif prev_turn is not None and turn < prev_turn:
        reasons.append(f"capture turn {turn} precedes the previous capture's "
                       f"turn {prev_turn}")
    return reasons


def bind_capture_reasons(record: dict, completed: list[dict],
                         bound: dict[int, int]) -> tuple[list[str], int | None]:
    """Bind ONE capture to the ordered completed-seat rows, one-to-one.

    Codex r2 finding 1: an existence test ("some earlier completed_seat_turn
    row names this seat and turn") lets every round-2 capture re-use round 1's
    rows — retarget their `turn` to 1 and the run PASSes having never captured
    turn 2. The producer emits CST then its capture, in lockstep
    (live_driver.py:1054, :1071), so a capture belongs to the completed-seat
    row it FOLLOWS: the last one before it in the log. That row must name the
    same seat and turn, and it backs exactly one capture. The baseline
    (after_seat -1) is emitted before play begins (:995), so it must precede
    every completed-seat row.

    Returns (reasons, index of the row this capture consumed or None).
    """
    outer, seq = record.get("after_seat"), record.get("seq")
    if type(outer) is not int:
        return [f"event after_seat {outer!r} is not an integer"], None
    if type(seq) is not int:
        return [f"event seq {seq!r} is not an integer"], None
    turn = record.get("turn")
    owner = None
    for index, row in enumerate(completed):
        if row["seq"] < seq:
            owner = index
        else:
            break
    if outer == -1:
        if owner is not None:
            return ([f"baseline capture (after_seat -1) follows the "
                     f"completed_seat_turn row at seq {completed[owner]['seq']}; "
                     "the baseline binds before play begins"], None)
        return [], None
    if owner is None:
        later = [row for row in completed
                 if row["player"] == outer and row["turn"] == turn]
        where = (f" (the matching row is at seq {later[0]['seq']}, after this "
                 "capture)" if later else "")
        return ([f"no completed_seat_turn row for seat {outer} at turn {turn} "
                 f"before this capture{where}"], None)
    row = completed[owner]
    if owner in bound:
        return ([f"the completed_seat_turn row at seq {row['seq']} is already "
                 f"bound to the capture at seq {bound[owner]}; a completed seat "
                 "turn backs exactly one capture"], None)
    if row["player"] != outer or row["turn"] != turn:
        return ([f"capture (seat {outer}, turn {turn}) does not bind to the "
                 f"completed_seat_turn row it follows (seq {row['seq']}: seat "
                 f"{row['player']}, turn {row['turn']})"], None)
    return [], owner


def stage_audit(run_dir: Path, rounds: int, require_live: bool,
                failures: list[str]) -> dict:
    """validate_run + the spectator_world contract + identity, in one pass."""
    result = {"validate": {}, "spectator_world_rows": 0,
              "expected_rows": 1 + 2 * rounds, "after_seat_sequence": [],
              "spectator_world_failed": 0, "spectator_world_rejected": [],
              "roster_kinds_observed": {},
              "entity_class_source": ENTITY_CLASS_SOURCE, "identity": {}}
    try:
        verdict = validate(run_dir, rounds, require_live=require_live)
    except (OSError, ValueError, KeyError, TypeError, IndexError) as exc:
        verdict = {"status": "INCOMPLETE",
                   "errors": [f"{type(exc).__name__}: {exc}"]}
    result["validate"] = verdict
    for message in verdict.get("errors") or []:
        failures.append(f"capture audit: {message}")

    worlds, failed, kinds, identity_events = [], [], Counter(), []
    completed: list[dict] = []
    try:
        with open(run_dir / "events.jsonl", encoding="utf-8") as stream:
            for number, line in enumerate(stream, start=1):
                if not line.strip():
                    continue
                try:
                    record = json.loads(line)
                except ValueError as exc:
                    raise QualificationError(
                        f"events.jsonl line {number} is not valid JSON: {exc}") from exc
                if not isinstance(record, dict):
                    raise QualificationError(
                        f"events.jsonl line {number} is not a JSON object")
                audit = record.get("audit")
                if audit == "spectator_world":
                    worlds.append(record)
                elif audit == "spectator_world_failed":
                    failed.append(record)
                elif audit == "run_identity":
                    identity_events.append(record)
                elif audit == "completed_seat_turn":
                    row = record.get("row")
                    seq = record.get("seq")
                    if (isinstance(row, dict) and type(row.get("player")) is int
                            and type(row.get("turn")) is int and type(seq) is int):
                        completed.append({"seq": seq, "player": row["player"],
                                          "turn": row["turn"]})
    except OSError as exc:
        raise QualificationError(f"events.jsonl is unreadable: {exc}") from exc
    completed.sort(key=lambda row: row["seq"])

    # Codex r1 findings 1+3: the census counts what the log CLAIMS (a novel
    # kind must never disappear), but only an envelope the producer could
    # have emitted, bound to a completed seat turn of its own, may take a
    # place in the row-contract sequence. Codex r2 finding 5: every value
    # read out of the log here is hostile input — a roster row is not
    # necessarily a dict and a player_id is not necessarily hashable, so the
    # census keys on a rendered label and the whole pass normalizes its
    # exceptions into the harness's own usage/IO channel.
    after_seats: list = []
    rejected: list[dict] = []
    novel: dict[tuple[str, str], int] = {}
    bound: dict[int, int] = {}
    prev_turn: int | None = None
    try:
        for record in worlds:
            world = record.get("world")
            if isinstance(world, dict) and isinstance(world.get("roster"), list):
                for row in world["roster"]:
                    kind = row.get("kind") if isinstance(row, dict) else None
                    name = kind if isinstance(kind, str) else "<absent-kind>"
                    kinds[name] += 1
                    if name not in WIRE_ROSTER_KINDS:
                        pid = row.get("player_id") if isinstance(row, dict) else None
                        label = str(pid) if type(pid) in (int, str) else repr(pid)
                        novel[(name, label)] = novel.get((name, label), 0) + 1
            reasons = world_envelope_reasons(record, prev_turn)
            binding, owner = bind_capture_reasons(record, completed, bound)
            reasons += binding
            if reasons:
                rejected.append({"seq": record.get("seq"),
                                 "after_seat": record.get("after_seat"),
                                 "turn": record.get("turn"),
                                 "reason": "; ".join(reasons)})
                continue
            if owner is not None:
                bound[owner] = record.get("seq")
            after_seats.append(record.get("after_seat"))
            prev_turn = record.get("turn")
    except (TypeError, ValueError, KeyError, AttributeError, IndexError) as exc:
        raise QualificationError(
            f"events.jsonl spectator_world records are malformed: "
            f"{type(exc).__name__}: {exc}") from exc

    expected = [-1] + [0, 1] * rounds
    result["spectator_world_rows"] = len(after_seats)
    result["after_seat_sequence"] = after_seats
    result["spectator_world_rejected"] = rejected
    result["spectator_world_failed"] = len(failed)
    result["roster_kinds_observed"] = dict(sorted(kinds.items()))
    for entry in rejected:
        failures.append(
            f"spectator_world capture at seq {entry['seq']} "
            f"(after_seat={entry['after_seat']}, turn={entry['turn']}) is not a "
            "valid world capture and does not count toward the row contract: "
            f"{entry['reason']}")
    for (kind, label), count in sorted(novel.items()):
        failures.append(
            f"roster kind {kind!r} (pid {label}) is not wire-admitted: "
            f"world_capture.parse_roster admits only "
            f"{', '.join(WIRE_ROSTER_KINDS)} ({count} row(s))")
    if failed:
        seats = [row.get("after_seat") for row in failed]
        failures.append(f"spectator_world_failed capture(s) present: {len(failed)} "
                        f"(after_seat={seats}) — the spectator capture never killed "
                        "the match, but the run is not qualified")
    if after_seats != expected:
        failures.append(f"spectator_world row contract violated: observed after_seat "
                        f"{after_seats}, expected {expected}")
    if not worlds:
        failures.append("no spectator_world capture in the log — the run was "
                        "recorded without spectator_capture or predates it")

    try:
        summary = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise QualificationError(f"summary.json is unreadable: {exc}") from exc
    identity = summary.get("identity") or {} if isinstance(summary, dict) else {}
    # fake lives on the run_identity audit event (live_driver phase_dispatch_hotseat);
    # the summary identity carries commit/tree/dirty/config/mod_sha256.
    fake = identity_events[0].get("fake") if identity_events else identity.get("fake")
    result["identity"] = {"commit": identity.get("commit"), "dirty": identity.get("dirty"),
                          "mod_sha256": identity.get("mod_sha256"), "fake": fake}
    if identity.get("dirty") is True:
        failures.append("run recorded on a dirty tree")
    return result


# -- stage 2: viewer ------------------------------------------------------------


def stage_viewer(run_dir: Path, baseline: list[str], failures: list[str]) -> dict:
    """The match room projection, in-process: completed and warning-clean."""
    result = {"status": None, "warnings": [], "baseline_matched": [],
              "new_warnings": [], "check": VIEWER_CHECK}
    try:
        payload = DashboardStore(run_dir.parent).load(run_dir.name)
    except (OSError, ValueError, TypeError, KeyError, AttributeError,
            RecursionError) as exc:
        failures.append(f"viewer load failed: {type(exc).__name__}: {exc}")
        return result
    warnings = payload.get("warnings") or []
    matched, new = diff_warnings(warnings, baseline)
    result.update(status=payload.get("status"), warnings=list(warnings),
                  baseline_matched=matched, new_warnings=new)
    if payload.get("status") != "completed":
        failures.append(f"viewer status is {payload.get('status')!r}, "
                        "expected 'completed'")
    if new:
        failures.append(f"viewer reported {len(new)} warning(s) outside the benign "
                        f"size-cap baseline: {new}")
    return result


# -- stage 3: exporter ----------------------------------------------------------


def _collect_refs(node, path: str, out: list) -> None:
    """Every events.jsonl reference object: exactly the exporter's _ref() shape
    ({"artifact": ..., "seq": ...}) wherever it sits — list-valued *_refs keys
    and the nested observed_interval_start/end singles alike."""
    if isinstance(node, dict):
        if "seq" in node and "artifact" in node:
            out.append((path, node))
            return
        for key in sorted(node):
            _collect_refs(node[key], f"{path}.{key}", out)
    elif isinstance(node, list):
        for index, item in enumerate(node):
            _collect_refs(item, f"{path}[{index}]", out)


def _read_samples(path: Path) -> tuple[bytes, list]:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise QualificationError(f"{path} is unreadable: {exc}") from exc
    samples = []
    for number, line in enumerate(raw.decode().splitlines(), start=1):
        if not line.strip():
            continue
        try:
            samples.append(json.loads(line))
        except ValueError as exc:
            raise QualificationError(f"{path} line {number} is not valid JSON: "
                                     f"{exc}") from exc
    return raw, samples


def _valid_event_seqs(events_bytes: bytes) -> set[int]:
    valid = set()
    for line in events_bytes.decode().splitlines():
        if not line.strip():
            continue
        seq = json.loads(line).get("seq")
        if type(seq) is int:
            valid.add(seq)
    return valid


def check_ref_integrity(exported_path: Path, events_bytes: bytes) -> dict:
    """Walk every exported sample and verify each reference resolves into the
    events.jsonl bytes the export consumed."""
    _, samples = _read_samples(exported_path)
    return check_sample_refs(samples, events_bytes)


def check_sample_refs(samples: list, events_bytes: bytes) -> dict:
    """Ref integrity over already-parsed exported samples (one disk read)."""
    valid = _valid_event_seqs(events_bytes)
    checked, failed = 0, []
    failures = 0
    for index, sample in enumerate(samples, start=1):
        found: list = []
        _collect_refs(sample, f"line {index}", found)
        for path, ref in found:
            checked += 1
            seq = ref.get("seq")
            problem = None
            if type(seq) is not int:
                problem = f"seq {seq!r} is not an integer"
            elif ref.get("artifact") != "events.jsonl":
                problem = f"artifact {ref.get('artifact')!r} is not events.jsonl"
            elif seq not in valid:
                problem = f"seq {seq} is not present in events.jsonl"
            if problem is None:
                continue
            failures += 1
            if len(failed) < MAX_FAILED_REFS:
                label = sample.get("segment_id")
                failed.append(f"{path} sample {label!r} ref {ref!r}: {problem}")
    return {"refs_checked": checked, "failures": failures, "failed_refs": failed}


def _flag_histogram(samples: list) -> dict:
    histogram: dict[str, Counter] = {}
    for sample in samples:
        flags = sample.get("quality_flags") or []
        histogram.setdefault(str(sample.get("sample_class")), Counter()).update(
            str(flag) for flag in flags)
    return {name: dict(sorted(counts.items()))
            for name, counts in sorted(histogram.items())}


def stage_export(run_dir: Path, staged_out: Path, export_out: Path,
                 failures: list[str]) -> dict:
    """The exporter, unchanged, plus ref integrity and digest binding.

    Codex r2 findings 3+4: the exporter writes into this invocation's OWN
    staging area (`staged_out`); `export_out` is only the destination the
    verdict document names, and nothing reaches it until every artifact is
    staged. The recorded digest is of the staged bytes — the same bytes
    publication commits."""
    empty = {"output": "", "output_sha256": "", "manifest": {}, "sample_counts": {},
             "ref_integrity": {"refs_checked": 0, "failures": 0, "failed_refs": []},
             "flag_histogram": {}}
    try:
        events_bytes = (run_dir / "events.jsonl").read_bytes()
    except OSError as exc:
        raise QualificationError(f"events.jsonl is unreadable: {exc}") from exc
    try:
        samples, manifest = export(run_dir)
    except (OSError, ValueError, KeyError, TypeError, AssertionError) as exc:
        failures.append(f"export failed: {type(exc).__name__}: {exc}")
        return empty
    result = dict(empty, manifest=manifest,
                  sample_counts=manifest.get("sample_counts") or {})
    try:
        write(samples, manifest, staged_out)
    except (OSError, ValueError) as exc:
        failures.append(f"export write failed: {type(exc).__name__}: {exc}")
        return result
    exported_bytes, on_disk = _read_samples(staged_out)
    result["output"] = str(export_out)
    result["output_sha256"] = hashlib.sha256(exported_bytes).hexdigest()
    source_sha = hashlib.sha256(events_bytes).hexdigest()
    if manifest.get("source", {}).get("events_sha256") != source_sha:
        failures.append("digest binding failed: manifest source.events_sha256 does not "
                        "match the events.jsonl bytes on disk")
    integrity = check_sample_refs(on_disk, events_bytes)
    result["ref_integrity"] = integrity
    if integrity["failures"]:
        failures.append(f"ref integrity: {integrity['failures']} broken reference(s) in "
                        f"{integrity['refs_checked']} checked: {integrity['failed_refs']}")
    result["flag_histogram"] = _flag_histogram(on_disk)
    return result


# -- stage 4: summary -----------------------------------------------------------


def stage_summarize(run_dir: Path, rounds: int, allow_fake: bool, sections: dict,
                    failures: list[str], coverage: dict | None) -> dict:
    """The machine verdict document (qualification.json schema 1)."""
    return {"schema": 1, "run_dir": str(run_dir), "rounds": rounds,
            "generated_at": datetime.now(UTC).isoformat(), "allow_fake": allow_fake,
            "audit": sections["audit"], "viewer": sections["viewer"],
            "export": sections["export"], "coverage": coverage,
            "verdict": "PASS" if not failures else "FAIL", "failures": list(failures)}


def coverage_section(coverage_path: Path) -> dict:
    """Lane-B coverage matrix, read as bytes: path, digest, non-blank row count.
    The file is consumed as data — no lane-B code is imported."""
    try:
        raw = coverage_path.read_bytes()
    except OSError as exc:
        raise QualificationError(f"coverage matrix is unreadable: {exc}") from exc
    return {"matrix_path": str(coverage_path),
            "matrix_sha256": hashlib.sha256(raw).hexdigest(),
            "matrix_rows": sum(1 for line in raw.decode().splitlines() if line.strip())}


def _row_line(*cells: object) -> str:
    """One markdown table row. Codex r1 finding 8: a raw `|` inside a cell
    (`SPECW|1 roster_read`, a run id, a path) silently shifts every column
    to its right, so the delimiter is escaped at render time. The JSON
    verdict document carries the unescaped values."""
    return "| " + " | ".join(str(cell).replace("|", "\\|") for cell in cells) + " |"


def render_report(qualification: dict, export_out: Path, json_path: Path,
                  report_path: Path | None) -> str:
    """Markdown skeleton for evidence card §6: machine cells filled, human
    cells left to the operator."""
    audit, viewer, export_section = (qualification["audit"], qualification["viewer"],
                                     qualification["export"])
    counts = ", ".join(f"{name}={count}" for name, count
                       in sorted((export_section.get("sample_counts") or {}).items()))
    histogram = json.dumps(export_section.get("flag_histogram") or {}, sort_keys=True)
    coverage = qualification["coverage"]
    lines = [
        f"# Recording qualification report — {Path(qualification['run_dir']).name}",
        "",
        f"Generated {qualification['generated_at']} by "
        "scripts/qualify_recording.py (dataset evidence card §6 steps 2-4).",
        "",
        _row_line("Field", "Value"),
        "|---|---|",
        _row_line("Run id", f"`{Path(qualification['run_dir']).name}`"),
        _row_line("Mod version", PENDING),
        _row_line("Samples by class", counts or "none"),
        _row_line("Ref-integrity failures",
                  f"{export_section['ref_integrity']['failures']} "
                  f"(of {export_section['ref_integrity']['refs_checked']} "
                  "refs checked)"),
        _row_line("Flag histogram", f"`{histogram}`"),
        _row_line("Allowed uses for THIS recording", PENDING),
        "",
        f"Verdict: **{qualification['verdict']}**",
        "",
        "## Capture audit",
        "",
        f"- validate status: {audit['validate'].get('status')!r} "
        f"(errors: {audit['validate'].get('errors')})",
        f"- spectator_world rows: {audit['spectator_world_rows']} of "
        f"{audit['expected_rows']} expected; after_seat={audit['after_seat_sequence']}",
        f"- spectator_world_failed: {audit['spectator_world_failed']}",
        f"- spectator_world captures rejected by the envelope contract: "
        f"{len(audit['spectator_world_rejected'])}",
        f"- roster kinds observed: {json.dumps(audit['roster_kinds_observed'],
                                               sort_keys=True)} "
        f"({audit['entity_class_source']})",
        f"- identity: commit={audit['identity'].get('commit')!r} "
        f"dirty={audit['identity'].get('dirty')!r} "
        f"mod_sha256={audit['identity'].get('mod_sha256')!r} "
        f"fake={audit['identity'].get('fake')!r}",
        "",
        "## Viewer",
        "",
        f"- {viewer['check']}; status: {viewer['status']!r}",
        f"- warnings: {json.dumps(viewer['warnings'])}",
        f"- baseline matched: {json.dumps(viewer['baseline_matched'])}",
        f"- new warnings: {json.dumps(viewer['new_warnings'])}",
        "",
        "## Exporter",
        "",
        f"- output: `{export_section['output']}` "
        f"(sha256 {export_section['output_sha256']})",
        f"- sample counts: {json.dumps(export_section.get('sample_counts') or {},
                                       sort_keys=True)}",
        f"- ref integrity: {export_section['ref_integrity']['refs_checked']} checked, "
        f"{export_section['ref_integrity']['failures']} failed",
        f"- flag histogram: `{histogram}`",
        "",
        "## Coverage",
        "",
        (f"- matrix: `{coverage['matrix_path']}` sha256 {coverage['matrix_sha256']} "
         f"rows {coverage['matrix_rows']}" if coverage else "- not provided"),
        "",
        "## Failures",
        "",
    ]
    lines += [f"- {failure}" for failure in qualification["failures"]] \
        or ["- none"]
    lines += ["", "## Operator fill-in",
              "",
              f"- Mod version and allowed uses: {PENDING} (evidence card §5 minus "
              "whatever this recording's flags remove).",
              f"- Machine document: `{json_path}`; exported samples: "
              f"`{export_out}` (manifest sibling).",
              f"- Report path: `{report_path}`." if report_path else ""]
    return "\n".join(line for line in lines if line is not None) + "\n"


# -- CLI ------------------------------------------------------------------------


def _parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="qualify_recording.py", description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Exit codes: 0 verdict PASS; 1 any stage failure (verdict FAIL, "
               "qualification.json still written); 2 usage/IO error (missing run "
               "dir, unreadable artifact, malformed baseline file, bad --rounds, "
               "an output path inside the run dir, an output aliasing the run's "
               "evidence or another output, or a failed publication).")
    ap.add_argument("run_dir", type=Path, help="captured run directory (read-only)")
    ap.add_argument("--rounds", type=int, required=True,
                    help="requested/completed round count the run was captured for")
    ap.add_argument("--allow-fake", action="store_true",
                    help="pass require_live=False to the capture audit (fake-engine "
                         "rehearsals); a real qualification omits this")
    ap.add_argument("--baseline-warnings", type=Path, default=DEFAULT_BASELINE,
                    help=f"benign size-cap warning baseline "
                         f"(default: {DEFAULT_BASELINE})")
    ap.add_argument("--export-out", type=Path, default=None,
                    help="exported samples path (default: <run>.qual/samples.jsonl "
                         "next to the run dir, never inside it)")
    ap.add_argument("--coverage", type=Path, default=None,
                    help="lane-B coverage matrix file; recorded by path, sha256 and "
                         "row count only")
    ap.add_argument("--report", type=Path, default=None,
                    help="markdown report path (default: "
                         "<run>.qual/qualification-report.md)")
    ap.add_argument("--json", type=Path, default=None,
                    help="machine verdict path (default: <run>.qual/qualification.json)")
    return ap


def _refuse_inside_run(path: Path, run_dir: Path, label: str) -> None:
    if path.resolve().is_relative_to(run_dir.resolve()):
        raise QualificationError(f"--{label} {path} is inside the run dir; the "
                                 "harness never writes into the run it qualifies")


def _resolve(path: Path) -> Path:
    """Canonical identity of an output target (symlinks, `..` and duplicate
    separators folded) — export_dataset._resolve_target's shape."""
    return Path(path).resolve(strict=False)


def _manifest_sibling(export_out: Path) -> Path:
    """The exporter's own manifest naming (export_dataset.write:224), so the
    guard, the staging area and publication all name the same file."""
    return export_out.with_suffix(export_out.suffix + ".manifest.json")


def _symlinked_ancestor(path: Path) -> Path | None:
    """The first symlinked directory on an output's parent chain, if any."""
    return next((parent for parent in path.parents if parent.is_symlink()), None)


@contextlib.contextmanager
def _publication_lock(verdict_path: Path):
    """Serialize publication across invocations sharing a verdict document.

    Bounded mitigation for Codex r3 finding 2 (see the module docstring's
    threat model): an O_EXCL lockfile keyed on the RESOLVED verdict path
    means two invocations cannot interleave their members and leave one
    generation's PASS attached to another's samples. The loser is refused,
    not queued. This does not serialize two invocations that share only
    SOME outputs while naming different verdict documents.
    """
    key = hashlib.sha256(str(_resolve(verdict_path)).encode()).hexdigest()[:32]
    lock = Path(tempfile.gettempdir()) / f"qualify-recording-{key}.lock"
    try:
        handle = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as exc:
        raise QualificationError(
            f"another publication holds the lock for {verdict_path} ({lock}); "
            "two invocations must not interleave their artifacts. If no other "
            "qualification is running, that lockfile is stale — remove it"
        ) from exc
    except OSError as exc:
        raise QualificationError(
            f"cannot take the publication lock {lock}: "
            f"{type(exc).__name__}: {exc}") from exc
    try:
        with os.fdopen(handle, "w") as stream:
            stream.write(f"{os.getpid()}\n")
        yield
    finally:
        with contextlib.suppress(OSError):
            lock.unlink(missing_ok=True)


def guard_outputs(outputs: list[tuple[str, Path]], run_dir: Path,
                  coverage: Path | None = None) -> None:
    """Codex r1 findings 2+5 and r2 finding 6, applied ONCE up front to EVERY
    output.

    Containment by resolution is not enough: an existing external HARDLINK
    to run/events.jsonl resolves outside the run dir yet shares its inode,
    so write_text() would truncate the sealed source. Mirrors the guard
    export_dataset.write() already applies to its own two targets
    (export_dataset.py:214-245): resolve, then os.path.samefile against
    each consumed artifact and the run dir. The same pass rejects two
    outputs that would land on one file — including the exporter's
    `<export-out>.manifest.json` sibling — so no verified artifact is
    overwritten after it was hashed.

    The supplied `--coverage` matrix is a protected INPUT, not an output:
    coverage_section() hashes it into the verdict, so an output allowed to
    land on it would leave the published `matrix_sha256` describing bytes
    that no longer exist at `matrix_path`.
    """
    sources = {_resolve(run_dir / name): run_dir / name for name in RUN_EVIDENCE}
    sources[_resolve(run_dir)] = run_dir
    for label, path in outputs:
        _refuse_inside_run(path, run_dir, label)
        # Bounded mitigation for Codex r3 finding 4: a symlinked output parent
        # can be retargeted between this guard and the write, so the resolved
        # path checked here is not the path written later. We refuse the shape
        # outright rather than pretend to win that race (module docstring).
        linked = _symlinked_ancestor(path)
        if linked is not None:
            raise QualificationError(
                f"--{label} {path} has a symlink on its parent chain "
                f"({linked}); an output directory that can be retargeted "
                "mid-run cannot be guarded, so it is refused")
        alias = sources.get(_resolve(path))
        if alias is None and path.exists():
            alias = next((source for source in sources.values()
                          if source.exists() and os.path.samefile(path, source)),
                         None)
        if alias is not None:
            raise QualificationError(
                f"--{label} {path} aliases run evidence {alias} (hardlink or "
                "path alias); the harness never overwrites the run it qualifies")
        if coverage is not None and (
                _resolve(path) == _resolve(coverage)
                or (path.exists() and coverage.exists()
                    and os.path.samefile(path, coverage))):
            raise QualificationError(
                f"--{label} {path} aliases the --coverage input {coverage} "
                "(hardlink or path alias); the matrix is hashed into the "
                "verdict and must outlive it unchanged")
    for index, (label_a, path_a) in enumerate(outputs):
        for label_b, path_b in outputs[index + 1:]:
            if _resolve(path_a) == _resolve(path_b):
                raise QualificationError(
                    f"--{label_a} and --{label_b} resolve to the same path "
                    f"({path_a}); every output needs its own file")
            if (path_a.exists() and path_b.exists()
                    and os.path.samefile(path_a, path_b)):
                raise QualificationError(
                    f"--{label_a} and --{label_b} are the same file ({path_a} "
                    f"and {path_b} share an inode); every output needs its own file")


def publish(members: list[tuple[str, Path, Path]]) -> list[str]:
    """Commit a fully staged generation to its destinations.

    Codex r2 findings 3+4: "absent when I started" is not ownership. The
    round-2 model wrote outputs in place and, on failure, unlinked whatever
    had appeared at a path it had once seen empty — which deletes a
    CONCURRENT invocation's artifacts, and which cannot restore a
    pre-existing document it had already overwritten. Nothing is written to
    a destination until every member exists in this invocation's staging
    area, destinations that cannot receive their member are refused before
    the first commit, and the verdict document is committed LAST.

    Per CAR-003 contract §5.3, four `os.replace` calls are four atomic
    operations, not one transaction: each member lands via
    canonical.atomic_write_text (mkstemp + os.replace, which cannot follow a
    planted link), and an interruption between members is reported with the
    members already committed — never as a success. Because the verdict
    document is last, an interrupted publication can never expose a PASS.
    """
    for label, _staged, dest in members:
        if dest.is_dir():
            raise QualificationError(
                f"--{label} {dest} is an existing directory; nothing was "
                "published")
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise QualificationError(
                f"--{label} parent directory cannot be created: "
                f"{type(exc).__name__}: {exc}; nothing was published") from exc
    # Codex r3 finding 1: committing the verdict last does NOT stop an
    # interrupted publication from leaving the PREVIOUS generation's PASS in
    # place while the samples and manifest it names have already been
    # replaced. The outgoing success documents are therefore invalidated
    # first, under the publication lock, before any artifact they reference
    # is touched: after this point a partial publication is missing its
    # verdict, which every reader treats as an interrupted publication.
    for label, _staged, dest in reversed(members):
        if label not in ("json", "report"):
            continue
        try:
            dest.unlink(missing_ok=True)
        except OSError as exc:
            raise QualificationError(
                f"the previous --{label} at {dest} cannot be invalidated: "
                f"{type(exc).__name__}: {exc}; nothing was published") from exc
    committed: list[str] = []
    for label, staged, dest in members:
        try:
            atomic_write_text(dest, staged.read_text(encoding="utf-8"))
        except OSError as exc:
            raise QualificationError(
                f"publication interrupted at --{label} {dest}: "
                f"{type(exc).__name__}: {exc}; committed before the "
                f"interruption: {committed or 'nothing'} — the verdict document "
                "is committed last, so no success document was exposed") from exc
        committed.append(str(dest))
    return committed


def main(argv: list[str] | None = None) -> int:
    opts = _parser().parse_args(argv)
    failures: list[str] = []
    try:
        run_dir = opts.run_dir
        if not run_dir.is_dir():
            raise QualificationError(f"run dir does not exist: {run_dir}")
        for name in ("events.jsonl", "summary.json"):
            artifact = run_dir / name
            if not artifact.is_file():
                raise QualificationError(f"missing run artifact: {artifact}")
        if opts.rounds < 1:
            raise QualificationError(f"--rounds must be >= 1, got {opts.rounds}")
        try:
            baseline = json.loads(opts.baseline_warnings.read_text(encoding="utf-8"))
        except OSError as exc:
            raise QualificationError(f"baseline file is unreadable: {exc}") from exc
        except ValueError as exc:
            raise QualificationError(f"baseline file is not valid JSON: {exc}") from exc
        if not isinstance(baseline, list) or not all(
                isinstance(item, str) for item in baseline):
            raise QualificationError("baseline file must be a JSON array of strings")
        # Codex r1 finding 4: a supplied baseline is a SPELLING of the
        # code-owned benign-cap set, never a way to whitelist a real warning
        # (WORLD_INVALID and friends can never be benign).
        foreign = [item for item in baseline
                   if item not in ALLOWED_BASELINE_WARNINGS]
        if foreign:
            raise QualificationError(
                "baseline file names warning(s) outside the code-owned "
                f"benign-cap allowlist: {foreign}")
        out_dir = run_dir.parent / f"{run_dir.name}.qual"
        export_out = opts.export_out or out_dir / "samples.jsonl"
        json_path = opts.json or out_dir / "qualification.json"
        report_path = opts.report or out_dir / "qualification-report.md"
        manifest_out = _manifest_sibling(export_out)
        outputs = [("export-out", export_out),
                   ("export-out manifest sibling", manifest_out),
                   ("json", json_path), ("report", report_path)]
        guard_outputs(outputs, run_dir, coverage=opts.coverage)
        coverage = None
        if opts.coverage is not None:
            coverage = coverage_section(opts.coverage)
    except QualificationError as exc:
        print(f"qualification: {exc}", file=sys.stderr)
        return 2

    # Codex r1 finding 6 + r2 findings 3-5: QualificationError is the
    # harness's usage/IO channel — nothing escapes as a traceback. Every
    # artifact is generated into staging THIS invocation owns; a failure
    # anywhere before publication discards the staging area and leaves every
    # destination exactly as it was found (no snapshot, no unlink, so no
    # concurrent invocation's artifacts and no previous generation are ever
    # destroyed).
    staging = None
    try:
        # Codex r3 finding 5: mkdtemp itself can fail (ENOSPC, EACCES), so it
        # lives INSIDE the boundary and cleanup is conditional on it existing.
        try:
            staging = Path(tempfile.mkdtemp(prefix="qualify-recording-"))
        except OSError as exc:
            raise QualificationError(
                f"cannot create the staging area: {type(exc).__name__}: "
                f"{exc}") from exc
        staged_export = staging / "export" / export_out.name
        staged_export.parent.mkdir(parents=True, exist_ok=True)
        staged_json, staged_report = staging / "verdict.json", staging / "report.md"
        audit = stage_audit(run_dir, opts.rounds, require_live=not opts.allow_fake,
                            failures=failures)
        viewer = stage_viewer(run_dir, baseline, failures=failures)
        export_section = stage_export(run_dir, staged_export, export_out,
                                      failures=failures)
        qualification = stage_summarize(run_dir, opts.rounds, opts.allow_fake,
                                        {"audit": audit, "viewer": viewer,
                                         "export": export_section},
                                        failures, coverage)
        staged_report.write_text(
            render_report(qualification, export_out, json_path, report_path),
            encoding="utf-8")
        staged_json.write_text(
            json.dumps(qualification, indent=2, sort_keys=True) + "\n",
            encoding="utf-8")
        members = []
        staged_manifest = _manifest_sibling(staged_export)
        if staged_export.is_file():
            members.append(("export-out", staged_export, export_out))
        if staged_manifest.is_file():
            members.append(("export-out manifest sibling", staged_manifest,
                            manifest_out))
        members.append(("report", staged_report, report_path))
        # the verdict document is the success document: committed LAST
        members.append(("json", staged_json, json_path))
        with _publication_lock(json_path):
            publish(members)
    except (QualificationError, OSError, TypeError, ValueError, KeyError,
            AttributeError, IndexError) as exc:
        detail = (str(exc) if isinstance(exc, QualificationError)
                  else f"unexpected {type(exc).__name__} while qualifying "
                       f"{run_dir}: {exc}")
        print(f"qualification: {detail}", file=sys.stderr)
        return 2
    finally:
        if staging is not None:
            shutil.rmtree(staging, ignore_errors=True)
    print(f"qualification: {qualification['verdict']} — {len(failures)} failure(s); "
          f"verdict {json_path}")
    for failure in failures:
        print(f"  - {failure}")
    return 0 if qualification["verdict"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
