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
    rows = [json.loads(line) for line
            in (run_dir / "events.jsonl").read_text().splitlines() if line.strip()]
    if [r.get("seq") for r in rows] != list(range(len(rows))):
        raise ValueError("event sequence is not contiguous — refusing export")
    return rows


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


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
    """Returns (samples, manifest). Never mutates the run dir."""
    run_dir = Path(run_dir)
    events = _load_events(run_dir)
    summary_path = run_dir / "summary.json"
    summary = json.loads(summary_path.read_text()) if summary_path.exists() \
        else {}
    if summary.get("phase") == "spectate":
        samples = _spectator_intervals(events, summary)
    elif any(e["kind"] == "MATCH_START" for e in events):
        samples = _controlled_decisions(run_dir, events, summary)
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
        # be detectable against the manifest
        "source": {"events_sha256": _digest(run_dir / "events.jsonl"),
                   "events": len(events),
                   **({"summary_sha256": _digest(summary_path)}
                      if summary_path.exists() else {}),
                   **({"llm_costs_sha256": _digest(run_dir / "llm_costs.jsonl")}
                      if (run_dir / "llm_costs.jsonl").exists() else {})},
        "sample_counts": {
            cls: sum(1 for s in samples if s["sample_class"] == cls)
            for cls in {s["sample_class"] for s in samples}},
        "schema_versions": sorted({s["schema_version"] for s in samples}),
    }
    return samples, manifest


def write(samples: list[dict[str, Any]], manifest: dict[str, Any],
          out_path: Path) -> None:
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as fh:
        for sample in samples:
            fh.write(json.dumps(sample, sort_keys=True,
                                separators=(",", ":")) + "\n")
    with open(out_path.with_suffix(
            out_path.suffix + ".manifest.json"), "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, sort_keys=True, indent=2)


def _controlled_decisions(run_dir: Path, events: list[dict[str, Any]],
                          summary: dict[str, Any]) -> list[dict[str, Any]]:
    """CAP-R1 #3: samples are keyed by (turn, agent_id) — a hotseat turn
    carries TWO agents' decisions and each must export as its own sample
    with only ITS observations/actions/receipts/boundary. Boundaries join
    per agent; a boundary without an agent id (legacy/hand-built) joins
    the turn's agent. Boundary FIELDS are read through _boundary_view
    (#2): production audits nest them in strategy_payload_json."""
    boundaries: dict[tuple[int, str], dict[str, Any]] = {}
    ledger_gaps = 0
    for e in events:
        if e["kind"] == "HEARTBEAT" and e.get("audit") == "decision_boundary":
            boundaries[(int(e["turn"]), str(e.get("agent_id")))] = \
                _boundary_view(e)
        elif e["kind"] == "HEARTBEAT" \
                and e.get("audit") == "ledger_write_failed":
            ledger_gaps += 1

    cost_rows: list[dict[str, Any]] = []
    pre_ledger = not (run_dir / "llm_costs.jsonl").exists()
    if not pre_ledger:
        cost_rows = [json.loads(line) for line
                     in (run_dir / "llm_costs.jsonl").read_text()
                     .splitlines() if line.strip()]

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

        obs = [_ref(e["seq"]) for e in events
               if e["kind"] == "TOOL_RESULT" and _agents(e)
               and e.get("tool") in _OBSERVE_TOOLS]
        actions = [_ref(e["seq"]) for e in events
                   if e["kind"] == "TOOL_CALL" and _agents(e)
                   and e.get("tool") in _ACTION_TOOLS]
        receipts = [_ref(e["seq"]) for e in events
                    if e["kind"] == "TOOL_RESULT" and _agents(e)
                    and e.get("status") == "accepted"
                    and (e.get("mutations") or e.get("receipts"))]
        boundary = boundaries.get((turn, agent_id)) \
            or boundaries.get((turn, "None"))
        decision_id = boundary.get("decision_id") if boundary else None
        rows = [r for r in cost_rows
                if decision_id is not None
                and r.get("decision_id") == decision_id
                and (r.get("agent_id") is None
                     or str(r.get("agent_id")) == agent_id)] \
            if not pre_ledger else []
        request_costs = None
        if rows:
            tokens_in = [r["input_tokens"] for r in rows
                         if isinstance(r.get("input_tokens"), int)]
            tokens_out = [r["output_tokens"] for r in rows
                          if isinstance(r.get("output_tokens"), int)]
            request_costs = {
                "attempts": len(rows),
                "logical_request_ids": sorted({
                    r["logical_request_id"] for r in rows
                    if r.get("logical_request_id")}),
                "tokens_in": sum(tokens_in) if tokens_in else None,
                "tokens_out": sum(tokens_out) if tokens_out else None,
                "latency_ms_sum": sum(r.get("latency_ms") or 0 for r in rows),
            }
        samples.append({
            "sample_class": "controlled_decision",
            "schema_version": SCHEMA_CONTROLLED,
            "run_id": summary.get("match_id"),
            "game_instance_id": summary.get("game_instance_id"),
            # multi-agent runs must disambiguate the segment; single-agent
            # runs keep the original stable id
            "segment_id": f"turn:{turn}" if single_agent
            else f"turn:{turn}:{agent_id}",
            "agent_id": agent_id,
            "player_id": next((e.get("player_id") for e in events
                               if _agents(e)), None),
            "split_group": _split_group(summary),
            "decision_id": decision_id,
            "directive_id": boundary.get("directive_id") if boundary else None,
            "provider_requests": boundary.get("provider_requests")
            if boundary else None,
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
            "quality_flags": quality.copy(),
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
