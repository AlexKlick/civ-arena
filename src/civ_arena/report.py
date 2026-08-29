"""Report CLI: summarize a run directory from its event log + summary.

    uv run python -m civ_arena.report runs/<match_id>
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path


def main() -> None:
    ap = argparse.ArgumentParser(prog="civ-arena-report")
    ap.add_argument("run_dir", type=Path)
    opts = ap.parse_args()
    run_dir = opts.run_dir

    summary_path = run_dir / "summary.json"
    if summary_path.exists():
        summary = json.loads(summary_path.read_text())
        print(f"match {summary['match_id']} | final turn {summary['final_turn']} | "
              f"aborted={summary.get('aborted')}")
        print(f"violations: {summary['violations_total']}")
        print(f"final state hash: {summary['final_state_hash']}")
        for civ, score in sorted(summary.get("scores", {}).items()):
            print(f"  {civ}: {score}")

    records = _load_records(run_dir / "events.jsonl")
    kinds = Counter(r["kind"] for r in records)
    print("\nevent counts:", dict(sorted(kinds.items())))

    per_agent: dict[str, Counter] = {}
    accepted: Counter = Counter()
    for rec in records:
        if rec["kind"] == "TOOL_RESULT" and rec.get("agent_id"):
            bucket = per_agent.setdefault(rec["agent_id"], Counter())
            bucket[rec.get("tool", "?")] += 1
            if rec.get("status") == "accepted":
                accepted[f"{rec['agent_id']}:{rec.get('tool')}"] += 1

    print("\ntool calls per agent:")
    for agent in sorted(per_agent):
        print(f"  {agent}: {dict(sorted(per_agent[agent].items()))}")
    print("\naccepted calls:")
    for key, n in sorted(accepted.items()):
        print(f"  {key}: {n}")

    telemetry = summary.get("telemetry", {}) if summary_path.exists() else {}
    tokens = {a: d for a, d in telemetry.items() if d.get("model")}
    if tokens:
        print("\nmodel usage:")
        for agent, doc in sorted(tokens.items()):
            print(f"  {agent}: model={doc['model']} "
                  f"tokens={doc['input_tokens']}in/{doc['output_tokens']}out")

    diaries = _latest_diaries(records)
    if diaries:
        print("\ndiaries (last write per agent):")
        for agent, note in sorted(diaries.items()):
            shown = note if len(note) <= 120 else note[:117] + "..."
            print(f"  {agent}: {shown!r}")

    violations = [r for r in records if r["kind"] == "VIOLATION"]
    for v in violations:
        wd = v.get("watchdog", {})
        print(f"\nVIOLATION turn {v['turn']} agent={v.get('agent_id')} "
              f"kind={wd.get('kind')} detail={wd.get('detail')}")


def _latest_diaries(records: list[dict]) -> dict[str, str]:
    """Last accepted write_diary text per agent, straight from the log."""
    notes: dict[str, str] = {}
    for i, rec in enumerate(records):
        if rec.get("kind") != "TOOL_CALL" or rec.get("tool") != "write_diary":
            continue
        if i + 1 >= len(records):
            continue
        nxt = records[i + 1]
        if (nxt.get("kind") == "TOOL_RESULT" and nxt.get("tool") == "write_diary"
                and nxt.get("status") == "accepted"
                and nxt.get("player_id") == rec.get("player_id")
                and nxt.get("agent_id") == rec.get("agent_id")
                and nxt.get("turn") == rec.get("turn")
                and rec.get("agent_id")):
            text = (rec.get("args") or {}).get("text")
            if isinstance(text, str):
                notes[rec["agent_id"]] = text
    return notes


def _load_records(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            break  # torn tail
    return out


if __name__ == "__main__":
    main()
