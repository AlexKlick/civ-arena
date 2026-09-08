"""Evidence-qualified dataset exporter v0 (CAP-03).

Turns a completed run directory into decision-linked samples with STRICT
class separation:

- ``controlled_decision`` — driven-agent runs: one sample per turn with
  the decision/directive identity (when the run predates decision
  boundaries: null ids + a quality flag, never fabricated), logical
  request identity and costs (when the run predates the cost ledger:
  null costs + a flag), observation/action/receipt REFERENCES (seq
  numbers into events.jsonl — payloads are never copied), and outcome
  horizon with censoring.

- ``spectator_interval`` — spectate runs: one sample per recorded round.
  Intervals are OBSERVATION INTERVALS: net state differences with owner
  attribution only, never command sequences, never actor claims. Actor
  attribution is ``owner_only``; exact action imitation and reasoning
  attribution are declared ineligible.

Determinism: the same run dir exports byte-identical sample bytes (sorted
keys, no export timestamps inside samples). The manifest (separate file)
carries export time and source digests.

Usage:
    python -m civ_arena.research.export_dataset runs/<id> -o out.jsonl
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SCHEMA_CONTROLLED = "cap03.controlled_decision/0"
SCHEMA_SPECTATOR = "cap03.spectator_interval/0"

_OBSERVE_TOOLS = frozenset({
    "get_units", "get_overview", "get_cities", "get_visible_map",
    "get_available_research", "get_available_production", "recall_lessons",
})
_ACTION_TOOLS = frozenset({
    "move_unit", "attack", "fortify", "found_city", "set_research",
    "set_city_production", "purchase", "end_turn",
})
# privileged/future label fields live OUTSIDE any actor-input section;
# the exporter emits no context/prompt/payload fields at all
_FORBIDDEN_KEYS = frozenset({
    "request", "response", "prompt", "system", "messages", "payload",
    "context", "wire", "actor_input",
})


def _load_events(run_dir: Path) -> list[dict[str, Any]]:
    return _parse_events((run_dir / "events.jsonl").read_bytes())


def _parse_events(raw: bytes) -> list[dict[str, Any]]:
    rows = [json.loads(line) for line
            in raw.decode().splitlines() if line.strip()]
    if [r.get("seq") for r in rows] != list(range(len(rows))):
        raise ValueError("event sequence is not contiguous — refusing export")
    return rows


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# R-03 (recheck 2026-09-08): the exporter's own inputs. write() refuses
# to place an output (or its manifest sibling) on any of these — a
# derived artifact clobbering its evidence source is unrecoverable.
_SOURCE_ARTIFACTS = ("events.jsonl", "summary.json", "llm_costs.jsonl")


def _resolve_target(path: Path) -> Path:
    """Canonical identity of an output target: symlinks resolved, `..` and
    duplicate separators folded — two spellings of the same file must
    compare equal so an alias cannot smuggle past the source guard."""
    return Path(path).resolve(strict=False)


def _source_paths(run_dir: Any) -> set[Path]:
    """Every path write() must refuse to touch for the run the manifest
    names: the three consumed artifacts AND the run directory itself
    (a directory target would shadow the sources on creation)."""
    try:
        run = Path(str(run_dir))
    except (TypeError, ValueError):
        return set()
    return {_resolve_target(run / name) for name in _SOURCE_ARTIFACTS} \
        | {_resolve_target(run)}


def _boundary_view(event: dict[str, Any]) -> dict[str, Any]:
    """CAP-R1 #2: PRODUCTION decision audits route through
    strategy_audit_event, which nests the payload fields
    (decision_id/directive_id/provider_requests/source) inside
    ``strategy_payload_json``; hand-built and legacy events carry them
    top-level. This view exposes BOTH shapes as one flat dict."""
    view = dict(event)
    raw = event.get("strategy_payload_json")
    if isinstance(raw, str):
        try:
            nested = json.loads(raw)
        except ValueError:
            nested = None
        if isinstance(nested, dict):
            view.update(nested)
    return view


def _ref(seq: int) -> dict[str, Any]:
    return {"artifact": "events.jsonl", "seq": seq}


def _split_group(summary: dict[str, Any]) -> str:
    """Sibling runs and restarts of the same configured match share a split
    group; the game INSTANCE distinguishes individual executions."""
    return str(summary.get("match_id") or "unknown-match")


def export(run_dir: Path) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Returns (samples, manifest). Never mutates the run dir.

    R-03 (recheck 2026-09-08): every consumed input is read as BYTES
    exactly once — the manifest digests those bytes, and the samples
    parse from them, so the hashes bind to what was actually consumed
    (no re-read window between digest and parse)."""
    run_dir = Path(run_dir)
    events_bytes = (run_dir / "events.jsonl").read_bytes()
    events = _parse_events(events_bytes)
    summary_path = run_dir / "summary.json"
    summary_bytes = summary_path.read_bytes() if summary_path.exists() else None
    summary = json.loads(summary_bytes) if summary_bytes is not None else {}
    costs_path = run_dir / "llm_costs.jsonl"
    # Codex r1 finding 2: the ledger is read as bytes ONCE — rows parse
    # and the manifest hashes the SAME buffer (a re-read could straddle
    # an append and bind samples to one version, the hash to another)
    costs_bytes = costs_path.read_bytes() if costs_path.exists() else None
    cost_rows = _parse_costs(costs_bytes) if costs_bytes is not None else None
    if summary.get("phase") == "spectate":
        samples = _spectator_intervals(events, summary)
    elif not summary_path.exists():
        # summary.json is the outcome record (final_turn/aborted/ids) —
        # without it a driven run's outcome horizon and censoring are
        # UNKNOWN and a controlled export would invent them. Spectator
        # data must not fall into the controlled class merely because
        # MATCH_START exists.
        raise ValueError(
            "driven run has no summary.json — outcome unknown; refusing "
            "controlled export")
    elif any(e["kind"] == "MATCH_START" for e in events):
        samples = _controlled_decisions(run_dir, events, summary,
                                        cost_rows=cost_rows,
                                        ledger_absent=costs_bytes is None)
    else:
        raise ValueError("run dir is neither spectate nor driven "
                         "(no MATCH_START); refusing export")
    for sample in samples:
        overlap = _FORBIDDEN_KEYS & set(sample)
        if overlap:
            raise AssertionError(  # pragma: no cover — schema guard
                f"class purity violation: {overlap} in {sample['sample_class']}")
    manifest = {
        "exported_at": datetime.now(UTC).isoformat(),
        "run_dir": str(run_dir),
        # CAP-R1 #12: digest EVERY consumed input — samples derive from
        # summary.json (outcome/horizon/ids) and llm_costs.jsonl (costs),
        # so a changed input with an unchanged events digest must still
        # be detectable against the manifest. R-03: each digest is the
        # sha of the exact bytes parsed above.
        "source": {"events_sha256":
                   hashlib.sha256(events_bytes).hexdigest(),
                   "events": len(events),
                   **({"summary_sha256":
                       hashlib.sha256(summary_bytes).hexdigest()}
                      if summary_bytes is not None else {}),
                   **({"llm_costs_sha256":
                       hashlib.sha256(costs_bytes).hexdigest()}
                      if costs_bytes is not None else {})},
        "sample_counts": {
            cls: sum(1 for s in samples if s["sample_class"] == cls)
            for cls in {s["sample_class"] for s in samples}},
        "schema_versions": sorted({s["schema_version"] for s in samples}),
    }
    return samples, manifest


def write(samples: list[dict[str, Any]], manifest: dict[str, Any],
          out_path: Path) -> None:
    """Publish derived outputs without clobbering evidence (R-03): the
    output and its manifest sibling are refused on any source artifact
    of the run the manifest names — direct path, symlink alias, or `..`
    spelling compare equal after resolution; an EXISTING target that
    hardlinks a source is caught by samefile() (Codex r1 finding 1:
    resolution cannot see hardlinks — open(..., 'w') would truncate the
    shared inode)."""
    out_path = Path(out_path)
    manifest_path = out_path.with_suffix(out_path.suffix + ".manifest.json")
    sources = _source_paths(manifest.get("run_dir")) if isinstance(
        manifest, dict) else set()
    for target in (out_path, manifest_path):
        resolved = _resolve_target(target)
        hit = resolved in sources
        if not hit and target.exists():
            hit = any(source.exists() and os.path.samefile(target, source)
                      for source in sources)
        if hit:
            raise ValueError(
                f"refusing to overwrite source evidence: {target} is a "
                f"consumed artifact of run_dir "
                f"{manifest.get('run_dir')!r}; write derived outputs "
                "elsewhere")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as fh:
        for sample in samples:
            fh.write(json.dumps(sample, sort_keys=True,
                                separators=(",", ":")) + "\n")
    with open(manifest_path, "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, sort_keys=True, indent=2)


def _parse_costs(raw: bytes) -> list[dict[str, Any]]:
    return [json.loads(line) for line in raw.decode().splitlines()
            if line.strip()]


def _controlled_decisions(run_dir: Path, events: list[dict[str, Any]],
                          summary: dict[str, Any], *,
                          cost_rows: list[dict[str, Any]] | None = None,
                          ledger_absent: bool | None = None
                          ) -> list[dict[str, Any]]:
    """CAP-R1 #3: samples are keyed by (turn, agent_id) — a hotseat turn
    carries TWO agents' decisions and each must export as its own sample
    with only ITS observations/actions/receipts/boundary. Boundaries join
    per agent; a boundary without an agent id (legacy/hand-built) joins
    the turn's agent. Boundary FIELDS are read through _boundary_view
    (#2): production audits nest them in strategy_payload_json.

    R-03: ``cost_rows`` are the rows export() already parsed from the
    exact bytes it digested into the manifest; when omitted (direct
    calls) the file is read as before.

    Codex r2 finding 3: ``ledger_absent`` is the CAPTURED existence
    verdict from export() — when the ledger was absent at capture, no
    later disk read may resurrect it (a file created between capture
    and segmentation would join rows the manifest never hashed).
    ``None`` derives from disk (the direct-call path, unchanged)."""
    boundaries: dict[tuple[int, str], list[dict[str, Any]]] = {}
    ledger_gaps = 0
    for e in events:
        if e["kind"] == "HEARTBEAT" and e.get("audit") == "decision_boundary":
            boundaries.setdefault(
                (int(e["turn"]), str(e.get("agent_id"))),
                []).append(_boundary_view(e))
        elif e["kind"] == "HEARTBEAT" \
                and e.get("audit") == "ledger_write_failed":
            ledger_gaps += 1

    pre_ledger = (ledger_absent if ledger_absent is not None
                  else not (run_dir / "llm_costs.jsonl").exists())
    if not pre_ledger and cost_rows is None:
        cost_rows = _parse_costs((run_dir / "llm_costs.jsonl").read_bytes())
    if cost_rows is None:
        cost_rows = []

    final_turn = summary.get("final_turn")
    aborted = summary.get("aborted")
    segments = sorted({(int(e["turn"]), str(e.get("agent_id")))
                       for e in events
                       if e["kind"] in ("TOOL_CALL", "TOOL_RESULT")
                       and isinstance(e.get("turn"), int) and e["turn"] >= 1})
    run_agents = {aid for _, aid in segments}
    single_agent = len(run_agents) == 1
    quality: list[str] = []
    if not boundaries:
        quality.append("pre_boundary_run")
    if pre_ledger:
        quality.append("pre_ledger_run")
    if ledger_gaps:
        quality.append("ledger_write_gaps")
    if aborted:
        quality.append("run_aborted")

    samples: list[dict[str, Any]] = []
    for turn, agent_id in segments:
        def _agents(e: dict[str, Any], *, _t: int = turn,
                    _a: str = agent_id) -> bool:
            return e.get("turn") == _t and str(e.get("agent_id")) == _a

        # a turn may carry SEVERAL decisions (CAP-R1 #4: the economy-
        # refresh path accepts a replacement directive with its own
        # boundary) — in event order. R-02 tail (recheck 2026-09-08):
        # GENUINE decision grain — one sample per boundary, not one
        # aggregated sample per turn. Events partition by seq: decision
        # k owns the calls after its boundary and before the next
        # boundary of the same (turn, agent); the last decision owns
        # the rest of the turn. The LAST boundary keeps the stable
        # segment id; superseded ones carry an @decision_id suffix.
        segment_boundaries = sorted(
            boundaries.get((turn, agent_id), [])
            + boundaries.get((turn, "None"), []),
            key=lambda b: b.get("seq") or 0)
        # Codex r1 finding 6 / r2 finding 2: a decision_id REUSED by
        # several boundaries of this (turn, agent) is ambiguous — rows
        # join only the LAST OCCURRENCE of that id (A, A, B: the second
        # A carries A's rows; B its own), every sibling is flagged.
        last_by_id: dict[str, dict[str, Any]] = {}
        id_counts: dict[str, int] = {}
        for b in segment_boundaries:
            if b.get("decision_id"):
                id_counts[b["decision_id"]] = \
                    id_counts.get(b["decision_id"], 0) + 1
                last_by_id[b["decision_id"]] = b
        reused_ids = {d for d, n in id_counts.items() if n > 1}
        # windows carry TWO starts: OBSERVATIONS span the wide window
        # (previous boundary, next boundary) — inputs precede the
        # boundary (r1 finding 3); ACTIONS/RECEIPTS span the narrow
        # window (own boundary, next boundary) — an execution belongs
        # to the decision that commanded it, never to a replacement
        # that superseded it (r2 finding 1).
        windows: list[tuple[dict[str, Any] | None, int | None, int | None,
                            int | None]] = []
        for i, b in enumerate(segment_boundaries):
            w_in = (segment_boundaries[i - 1].get("seq")
                    if i > 0
                    and isinstance(segment_boundaries[i - 1].get("seq"), int)
                    else None)
            w_exec = (b.get("seq")
                      if isinstance(b.get("seq"), int) else None)
            w_end = (segment_boundaries[i + 1].get("seq")
                     if i + 1 < len(segment_boundaries)
                     and isinstance(segment_boundaries[i + 1].get("seq"), int)
                     else None)
            windows.append((b, w_in, w_exec, w_end))
        if not windows:
            windows.append((None, None, None, None))
        base_segment = f"turn:{turn}" if single_agent \
            else f"turn:{turn}:{agent_id}"
        for boundary, w_start, w_exec, w_end in windows:
            def _window(e: dict[str, Any], *, _s: int | None,
                        _e: int | None = w_end) -> bool:
                if not _agents(e):
                    return False
                # only int seqs are windowable; a non-int seq falls out
                # of every explicit window (the window is (start, end)
                # EXCLUSIVE on both sides)
                if not isinstance(e.get("seq"), int):
                    return _s is None and _e is None
                if _s is not None and not e["seq"] > _s:
                    return False
                return not (_e is not None and not e["seq"] < _e)

            obs = [_ref(e["seq"]) for e in events
                   if e["kind"] == "TOOL_RESULT"
                   and _window(e, _s=w_start)
                   and e.get("tool") in _OBSERVE_TOOLS]
            actions = [_ref(e["seq"]) for e in events
                       if e["kind"] == "TOOL_CALL"
                       and _window(e, _s=w_exec)
                       and e.get("tool") in _ACTION_TOOLS]
            receipts = [_ref(e["seq"]) for e in events
                        if e["kind"] == "TOOL_RESULT"
                        and _window(e, _s=w_exec)
                        and e.get("status") == "accepted"
                        and (e.get("mutations") or e.get("receipts"))]
            decision_id = boundary.get("decision_id") if boundary else None
            decision_ids = [decision_id] if decision_id else []
            cost_recipient = (boundary is last_by_id[decision_id]
                              if decision_id else False)
            shared_id = decision_id in reused_ids
            rows = [r for r in cost_rows
                    if decision_ids
                    and cost_recipient
                    and r.get("decision_id") in decision_ids
                    and (r.get("agent_id") is None
                         or str(r.get("agent_id")) == agent_id)] \
                if not pre_ledger else []
            segment_flags = quality.copy()
            superseded = boundary is not segment_boundaries[-1] \
                if segment_boundaries else False
            if len(segment_boundaries) > 1:
                segment_flags.append("boundary_superseded_within_turn")
            if shared_id:
                segment_flags.append("decision_id_reused_costs_ambiguous")
            expected = boundary.get("provider_requests") \
                if boundary else None
            # Codex r1 finding 4: the expected-vs-recorded comparison
            # runs even with ZERO recorded rows — a boundary that
            # declared requests with an empty (but CAPTURED) ledger is
            # exactly the sink-failure shape the flag exists for. A
            # pre-ledger run (absent at capture) keeps request_costs
            # None: nothing about costs was consumed at all.
            request_costs = None
            if rows or (not pre_ledger and isinstance(expected, int)
                        and expected > 0):
                tokens_in = [r["input_tokens"] for r in rows
                             if isinstance(r.get("input_tokens"), int)]
                tokens_out = [r["output_tokens"] for r in rows
                              if isinstance(r.get("output_tokens"), int)]
                latencies = [r["latency_ms"] for r in rows
                             if isinstance(r.get("latency_ms"), int)]
                # R-04 (recheck 2026-09-08): a KNOWN subtotal, never a
                # silently-qualified total. Attempts whose usage the cost
                # ledger did not record (None) are counted separately and
                # flag the sample; they are NOT folded in as zeros.
                # Genuine zeros survive — a recorded 0 is data, an absent
                # field is not (sum over the known list only; empty list
                # -> None, not 0, so "no known usage" stays
                # distinguishable from "usage known to be zero").
                unknown_usage = sum(
                    1 for r in rows
                    if not isinstance(r.get("input_tokens"), int)
                    or not isinstance(r.get("output_tokens"), int))
                unknown_latency = len(rows) - len(latencies)
                request_costs = {
                    "attempts": len(rows),
                    "expected_attempts": expected,
                    "attempts_with_unknown_usage": unknown_usage,
                    "attempts_with_unknown_latency": unknown_latency,
                    "logical_request_ids": sorted({
                        r["logical_request_id"] for r in rows
                        if r.get("logical_request_id")}),
                    "tokens_in": sum(tokens_in) if tokens_in else None,
                    "tokens_out": sum(tokens_out) if tokens_out else None,
                    "latency_ms_sum": sum(latencies) if latencies else None,
                    "usage_complete": unknown_usage == 0
                    and unknown_latency == 0,
                }
                if unknown_usage or unknown_latency:
                    segment_flags.append("costs_partial")
                if isinstance(expected, int) and len(rows) < expected:
                    segment_flags.append("costs_attempts_missing")
            # the superseded suffix carries the boundary's seq too — a
            # replacement chain reusing one decision_id would otherwise
            # mint duplicate segment_ids (downstream dicts key on it)
            suffix = (f"@{decision_id}~{boundary['seq']}"
                      if superseded and decision_id
                      and isinstance(boundary.get("seq"), int)
                      else f"@{decision_id}" if superseded and decision_id
                      else "")
            # Codex r1 finding 5: identity comes from the BOUNDARY first
            # (an empty window — two consecutive boundaries — must not
            # lose a known player), then the first NON-NULL player_id in
            # the window, then anywhere in the (turn, agent) segment.
            player_id = (boundary.get("player_id")
                         if boundary is not None
                         and isinstance(boundary.get("player_id"), int)
                         else None)
            if player_id is None:
                player_id = next(
                    (e["player_id"] for e in events
                     if _window(e, _s=w_start)
                     and isinstance(e.get("player_id"), int)), None)
            if player_id is None:
                player_id = next(
                    (e["player_id"] for e in events
                     if _agents(e)
                     and isinstance(e.get("player_id"), int)), None)
            samples.append({
                "sample_class": "controlled_decision",
                "schema_version": SCHEMA_CONTROLLED,
                "run_id": summary.get("match_id"),
                "game_instance_id": summary.get("game_instance_id"),
                "segment_id": base_segment + suffix,
                "agent_id": agent_id,
                "player_id": player_id,
                "split_group": _split_group(summary),
                "decision_id": decision_id,
                "directive_id": boundary.get("directive_id")
                if boundary else None,
                "provider_requests": boundary.get("provider_requests")
                if boundary else None,
                "decisions_in_turn": len(segment_boundaries),
                "decision_source": boundary.get("source") if boundary else None,
                "logical_request_ids": sorted({
                    r["logical_request_id"] for r in rows
                    if r.get("logical_request_id")}),
                "observation_refs": obs,
                "requested_action_refs": actions,
                "execution_receipt_refs": receipts,
                "request_costs": request_costs,
                "procedural_tool_calls": len(actions),
                "outcome_horizon": (final_turn - turn)
                if isinstance(final_turn, int) else None,
                "censoring": "run_aborted" if aborted else None,
                "visibility_class": "ordinary_player_information",
                "quality_flags": segment_flags,
                "source_manifest_ref": {"artifact": "summary.json"},
            })
    return samples


def _spectator_intervals(events: list[dict[str, Any]],
                         summary: dict[str, Any]) -> list[dict[str, Any]]:
    """Intervals pair CHRONOLOGICALLY (each HUMAN_TURN_END closes the
    currently-open round), never by turn-label equality: in wrap-affected
    runs a round that lost its DEACT to a ring wrap is closed by the NEXT
    turn's END, so the start/end turn labels legitimately differ. The
    mismatch is carried as a quality flag, never silently reconciled."""
    gaps = [e for e in events if e.get("audit") == "trace_gap"]
    jump_gaps = [e for e in events if e.get("audit") == "capture_gap"]
    gap_inferred_rounds = sum(
        1 for e in events
        if e.get("kind") == "HUMAN_TURN_START"
        and e.get("boundary") == "gap_inferred")
    open_start: dict[str, Any] | None = None
    samples: list[dict[str, Any]] = []
    for e in events:
        if e["kind"] == "HUMAN_TURN_START":
            open_start = e
            continue
        if e["kind"] not in ("HUMAN_TURN_END",):
            continue
        end = e
        start = open_start
        open_start = None
        snap = next((s for s in events
                     if s["kind"] == "SPECTATOR_SNAPSHOT"
                     and start is not None
                     and start["seq"] < s["seq"] < end["seq"]), None)
        start_turn = int(start["turn"]) if start else None
        end_turn = int(end["turn"])
        flags = []
        if start is None:
            flags.append("orphan_end_without_start")
        if start_turn != end_turn:
            flags.append("turn_label_mismatch")
        if start is not None and start.get("boundary") == "gap_inferred":
            flags.append("gap_inferred_round")
        samples.append({
            "sample_class": "spectator_interval",
            "schema_version": SCHEMA_SPECTATOR,
            "run_id": summary.get("match_id"),
            "game_instance_id": summary.get("game_instance_id"),
            "segment_id": f"seq:{start['seq']}" if start
            else f"seq:{end['seq']}",
            "split_group": _split_group(summary),
            "interval_turn_start": start_turn,
            "interval_turn_end": end_turn,
            "observed_interval_start": _ref(start["seq"]) if start else None,
            "observed_interval_end": _ref(end["seq"]),
            "interval_closed": True,
            # CAP-R1 #11: the ACTUAL provenance cursors when the run
            # carries them (None only for pre-CAP-01 runs)
            "source_cursor_start":
                start.get("source_cursor") if start else None,
            "source_cursor_end": end.get("source_cursor"),
            "snapshot_refs": [_ref(snap["seq"])] if snap else [],
            "ambient_diff_refs": [_ref(end["seq"])],
            "ambient_row_count": len(end.get("human_ambient", [])),
            "actor_attribution": "owner_only",
            "observed_actor_ids": [],
            "entity_owner_ids": sorted(
                {str(row.get("entity_id", ":")).split(":")[0]
                 for row in end.get("human_ambient", [])}),
            "capture_gaps": len(gaps),
            "engine_jump_gaps": len(jump_gaps),
            "gap_inferred_rounds": gap_inferred_rounds,
            "truncated": bool(snap.get("truncated")) if snap else None,
            "human_window": start.get("window") if start else None,
            "eligible_tasks": ["state_trend", "outcome_label"],
            "ineligible_with_reasons": {
                "exact_action_imitation":
                    "interval diffs are net state changes, not commands",
                "reasoning_attribution":
                    "no intent channel recorded for this run",
            },
            "quality_flags": flags,
        })
    if open_start is not None:  # interrupted final turn: never fake an end
        samples.append({
            "sample_class": "spectator_interval",
            "schema_version": SCHEMA_SPECTATOR,
            "run_id": summary.get("match_id"),
            "game_instance_id": summary.get("game_instance_id"),
            "segment_id": f"seq:{open_start['seq']}",
            "split_group": _split_group(summary),
            "interval_turn_start": int(open_start["turn"]),
            "interval_turn_end": None,
            "observed_interval_start": _ref(open_start["seq"]),
            "observed_interval_end": None,
            "interval_closed": False,
            "source_cursor_start": open_start.get("source_cursor"),
            "source_cursor_end": None,
            "snapshot_refs": [_ref(s["seq"]) for s in events
                              if s["kind"] == "SPECTATOR_SNAPSHOT"
                              and s["seq"] > open_start["seq"]][:1],
            "ambient_diff_refs": [],
            "ambient_row_count": None,
            "actor_attribution": "owner_only",
            "observed_actor_ids": [],
            "entity_owner_ids": [],
            "capture_gaps": len(gaps),
            "engine_jump_gaps": len(jump_gaps),
            "gap_inferred_rounds": gap_inferred_rounds,
            "truncated": None,
            "human_window": open_start.get("window"),
            "eligible_tasks": ["state_trend", "outcome_label"],
            "ineligible_with_reasons": {
                "exact_action_imitation":
                    "interval diffs are net state changes, not commands",
                "reasoning_attribution":
                    "no intent channel recorded for this run",
            },
            "quality_flags": ["interval_open_at_export"],
        })
    samples.sort(key=lambda s: s["segment_id"])
    return samples


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="civ-arena-export-dataset",
                                 description=__doc__)
    ap.add_argument("run_dir", type=Path)
    ap.add_argument("-o", "--out", type=Path, required=True)
    opts = ap.parse_args(argv)
    samples, manifest = export(opts.run_dir)
    write(samples, manifest, opts.out)
    counts = manifest["sample_counts"]
    print(f"exported {sum(counts.values())} samples "
          f"({', '.join(f'{k}={v}' for k, v in sorted(counts.items()))}) "
          f"-> {opts.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
