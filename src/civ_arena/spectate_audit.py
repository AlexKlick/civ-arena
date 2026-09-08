"""The ONE spectate structural-audit core (CAP-02).

Used by BOTH validation entrypoints — the replay CLI's structural audit
and the run validator — so they can never disagree by carrying two
copies of the rules (Exchange-2 F-05: the old certificate skipped
snapshot presence, so deleting every snapshot + renumbering passed).

Scope honesty: this core proves properties of the SUPPLIED records
(order, presence, shape, cross-agreement with the summary when given).
It does NOT prove the absence of unobserved engine events, and it never
claims re-execution — spectate runs have zero driven tool calls by
construction.

Outcome classes (the summary may declare one; otherwise inferred):
``completed | operator_stopped | timed_out | interrupted | crashed |
running_prefix``. Open-prefix outcomes (everything except ``completed``)
tolerate a trailing unpaired HUMAN_TURN_START — the operator quit or the
connection dropped mid-turn — without fabricating a matching END.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

SPECTATE_FORBIDDEN_KINDS = frozenset({
    "TOOL_CALL", "TOOL_RESULT", "LEASE_GRANT", "LEASE_RELEASE",
    "LEASE_EXPIRED", "VIOLATION", "UNAUTHORIZED_TOOL_CALL",
})

SPECTATE_ROUND_KINDS = ("HUMAN_TURN_START", "SPECTATOR_SNAPSHOT",
                        "HUMAN_TURN_END")

OPEN_PREFIX_OUTCOMES = frozenset({
    "operator_stopped", "timed_out", "interrupted", "crashed",
    "running_prefix",
})

_SCHEMA_NOTE = (
    "structural audit only — no re-execution and no state comparison "
    "were performed; legacy keys identical/replayed_events are retained "
    "for compatibility and derive from audit_valid, not from any replay"
)


def classify_outcome(summary: dict[str, Any] | None) -> str:
    """Outcome class from the summary; honest inference for legacy runs
    that predate the explicit ``outcome`` key."""
    if not isinstance(summary, dict) or not summary:
        return "running_prefix"
    outcome = summary.get("outcome")
    if isinstance(outcome, str) and outcome:
        return outcome
    if summary.get("phase") != "spectate":
        return "running_prefix"
    failure = summary.get("failure_reason") or summary.get("aborted")
    if summary.get("clean") is True and failure is None:
        return "completed"
    if failure is None:
        return "running_prefix"
    failure = str(failure)
    if failure == "cancelled":
        return "operator_stopped"
    if failure == "match timeout":
        return "timed_out"
    if "ConnectionError" in failure or "TimeoutError" in failure:
        return "interrupted"
    return "crashed" if "during" not in failure else "interrupted"


def structural_problems(records: list[dict[str, Any]], *,
                        summary: dict[str, Any] | None = None,
                        allow_open_prefix: bool = False) -> list[str]:
    """The shared structural rule set. Every problem found is reported;
    nothing is fabricated and no rule is outcome-conditional except the
    trailing-open-round tolerance, which the caller gates via
    ``allow_open_prefix`` (true for open-prefix outcome classes)."""
    problems: list[str] = []
    if not records:
        return ["empty event log"]
    if records[0].get("kind") != "MATCH_START":
        problems.append("first record is not MATCH_START")
    ends = [r for r in records if r.get("kind") == "MATCH_END"]
    if len(ends) > 1:
        problems.append("more than one MATCH_END")
    elif ends:
        if records[-1].get("kind") != "MATCH_END":
            problems.append("MATCH_END is not the last record")
    elif not allow_open_prefix:
        problems.append("no MATCH_END (open prefix, but the declared "
                        "outcome does not allow one)")
    seqs = [r.get("seq") for r in records]
    if seqs != list(range(len(records))):
        problems.append(
            "event sequence not contiguous from 0 (tampered or torn log)")
    for rec in records:
        if rec.get("kind") in SPECTATE_FORBIDDEN_KINDS:
            problems.append(
                f"{rec.get('kind')} at seq {rec.get('seq')} — a spectator "
                "never acts")
            break

    round_events = [r for r in records if r.get("kind") in SPECTATE_ROUND_KINDS]
    kinds = [r["kind"] for r in round_events]
    # expected: (START, SNAPSHOT, END) triples, optionally one trailing
    # open round (START [+ SNAPSHOT]) when open prefixes are allowed
    n_complete = len(kinds) // 3
    expected: list[str] = list(SPECTATE_ROUND_KINDS) * n_complete
    tail = kinds[len(expected):]
    tail_ok = (tail == [] or (allow_open_prefix and (
        tail == ["HUMAN_TURN_START"]
        or tail == ["HUMAN_TURN_START", "SPECTATOR_SNAPSHOT"])))
    if kinds[:len(expected)] != expected or not tail_ok:
        problems.append(
            "HUMAN_TURN_START/SPECTATOR_SNAPSHOT/HUMAN_TURN_END do not "
            "strictly alternate (round interleave broken: START, SNAPSHOT, "
            "END per round — snapshots must be present between each START "
            "and its END)")
    # per-round turn/participant agreement among the round events
    starts = [r for r in records if r.get("kind") == "HUMAN_TURN_START"]
    snaps = [r for r in records if r.get("kind") == "SPECTATOR_SNAPSHOT"]
    human_ends = [r for r in records if r.get("kind") == "HUMAN_TURN_END"]
    if len(starts) == len(human_ends) and len(snaps) < len(starts):
        problems.append(
            f"round event counts: {len(starts) - len(snaps)} round(s) "
            "missing a SPECTATOR_SNAPSHOT")
    turns = [r.get("turn") for r in starts]
    if turns != sorted(set(turns)) or len(turns) != len(set(turns)):
        problems.append("HUMAN_TURN_START turns are not strictly increasing")
    operators = {r.get("operator") for r in starts + human_ends}
    if len(operators) > 1 or None in operators:
        problems.append("operator identity is missing or inconsistent "
                        f"across human-turn events: {sorted(map(str, operators))}")
    seats = {r.get("phase_player_id") for r in round_events}
    if len(seats) > 1 or None in seats:
        problems.append("phase_player_id is missing or inconsistent across "
                        "spectate round events")
    # turn ids inside each round's start pair must agree (the phase writes
    # them together); a START->END turn SPAN is NOT a structural defect —
    # it is the honest signature of a capture gap (a lost turn boundary
    # made one interval cover two turns) and is surfaced by the
    # validator's eligibility layer instead.
    for i in range(min(len(starts), len(snaps))):
        if starts[i].get("turn") != snaps[i].get("turn"):
            problems.append(
                f"round {i + 1}: HUMAN_TURN_START turn {starts[i].get('turn')} "
                f"vs SPECTATOR_SNAPSHOT turn {snaps[i].get('turn')}")
            break
    for s in snaps:
        digest = s.get("digest")
        if not (isinstance(digest, dict) and isinstance(digest.get("before"), str)
                and isinstance(digest.get("after"), str)
                and isinstance(digest.get("consistent"), bool)):
            problems.append(
                f"SPECTATOR_SNAPSHOT at seq {s.get('seq')} lacks a complete "
                "digest bracket (before/after/consistent)")
            break
    if isinstance(summary, dict) \
            and isinstance(summary.get("completed_rounds"), int) \
            and summary["completed_rounds"] != len(human_ends):
        problems.append(
            f"summary completed_rounds={summary['completed_rounds']} but the "
            f"log carries {len(human_ends)} completed human turns")
    return problems


def final_interval_state(records: list[dict[str, Any]]) -> str:
    """'open' when the log's last round event is an unpaired START (or a
    START+SNAPSHOT with no END) — an observed fact, reported, never
    papered over. 'closed' otherwise."""
    round_events = [r for r in records if r.get("kind") in SPECTATE_ROUND_KINDS]
    if round_events and round_events[-1].get("kind") != "HUMAN_TURN_END":
        return "open"
    return "closed"


def build_manifest(records: list[dict[str, Any]], raw: bytes) -> dict[str, Any]:
    """Completed-manifest trust root written at match_end: per-kind counts
    + byte length + digest over the SUPPLIED bytes. Proves properties of
    the records handed to the checker — it cannot prove the engine emitted
    nothing we failed to observe."""
    kind_counts: dict[str, int] = {}
    for r in records:
        kind_counts[r.get("kind", "?")] = kind_counts.get(r.get("kind", "?"), 0) + 1
    return {
        "schema": 1,
        "bytes": len(raw),
        "lines": len(records),
        "sha256": hashlib.sha256(raw).hexdigest(),
        "kind_counts": kind_counts,
        "note": "properties of supplied records only; not proof of absence "
                "of unobserved engine events",
    }


def check_manifest(run_dir: Path) -> list[str]:
    """Verify the retained manifest against the current log bytes. A
    changed payload (even with untouched seq numbers) fails here."""
    manifest_path = Path(run_dir) / "manifest.json"
    if not manifest_path.exists():
        return []
    try:
        manifest = json.loads(manifest_path.read_text())
    except (json.JSONDecodeError, OSError) as exc:
        return [f"manifest unreadable: {exc}"]
    raw_path = Path(run_dir) / "events.jsonl"
    raw = raw_path.read_bytes() if raw_path.exists() else b""
    problems: list[str] = []
    if len(raw) != manifest.get("bytes"):
        problems.append(
            f"manifest byte length mismatch: recorded {manifest.get('bytes')}, "
            f"actual {len(raw)} — payload changed after closeout")
    if hashlib.sha256(raw).hexdigest() != manifest.get("sha256"):
        problems.append("manifest sha256 mismatch — payload changed after "
                        "closeout (detected against the retained manifest)")
    actual_lines = raw.count(b"\n") if raw else 0
    if actual_lines != manifest.get("lines"):
        problems.append(
            f"manifest line count mismatch: recorded {manifest.get('lines')}, "
            f"actual {actual_lines}")
    return problems


def derive_provenance(run_dir: Path) -> dict[str, Any]:
    """Read-only provenance assessment for an EXISTING run: reconciles the
    immutable run_identity audit event (launch truth) against the summary
    identity, and reports the outcome class — without rewriting anything
    in the run dir."""
    run_dir = Path(run_dir)
    records = []
    events = run_dir / "events.jsonl"
    if events.exists():
        for line in events.read_text().splitlines():
            if not line.strip():
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                break
    summary: dict[str, Any] = {}
    summary_path = run_dir / "summary.json"
    if summary_path.exists():
        try:
            summary = json.loads(summary_path.read_text())
        except (json.JSONDecodeError, OSError):
            summary = {}
    launch_events = [r for r in records if r.get("audit") == "run_identity"]
    launch_identity = launch_events[0].get("identity") if launch_events else None
    summary_identity = summary.get("identity")
    closeout_identity = summary.get("closeout_identity", summary_identity)
    return {
        "run_dir": str(run_dir),
        "outcome": classify_outcome(summary),
        "launch_identity_from_event": launch_identity,
        "summary_identity": summary_identity,
        "closeout_identity": closeout_identity,
        "identity_divergence": (
            launch_identity is not None and summary_identity is not None
            and launch_identity != summary_identity),
        "note": "derived assessment only; original records untouched",
    }
