"""Per-bot strategy-execution audit: one glm-5.3-flash analyst per seat.

The deterministic facts (trajectories, switch events, aggregate outcomes)
are computed LOCALLY and handed to the model — the analyst audits against
real numbers, never reconstructs them. Every verdict is fail-soft: a bad
or missing response costs that label's report, never the loop.

Prompt budget discipline: one label per call (~3-4k input tokens), four
calls per batch + the probe — well inside the 12-call iteration cap, and
small enough to dodge the transport hangs the omnibus analyze prompt hit
on 2026-09-17.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from civ_arena.agents.scripted import DOCTRINES

VERDICT_CONTRACT = """{
  "executing_strategy": true,          // did the bot visibly run its doctrine
  "execution_score_0_10": 7,           // 0 = ignored its doctrine, 10 = crisp
  "evidence": ["...", "..."],          // 3-6 observations GROUNDED in the
                                       //   per-game curves given below
  "deviations": ["...", "..."],        // where behavior diverged from the
                                       //   thesis / expected signature
  "pivot_assessment": {                // adaptive seats only; {} otherwise
    "fired": true,                     // any switch ever happened
    "timing_sensible": true,           // did switches track game state
    "flip_flops": 2,                   // oscillations you can count below
    "notes": "..."
  },
  "recommended_changes": ["...", "..."] // concrete param/trigger changes
}"""

_SYSTEM = (
    "You are a rigorous strategy-execution auditor for a 4-player "
    "deterministic civ-style simulator. You judge ONLY what the data shows: "
    "whether each bot visibly executed its doctrine, and whether its "
    "doctrine's stated thesis matches what its curves actually did. Never "
    "invent numbers — every figure you cite must appear in the provided "
    "facts. Reply with a single JSON object exactly matching the contract."
)

_SAMPLE_EVERY = 10  # trajectory sample stride for the prompt


def _games_for_label(label: str, rows: list[dict[str, Any]]) -> list[dict]:
    """Every clean seat-game this label played (duplicate-label rosters
    contribute one entry per seat per row)."""
    out = []
    for row in rows:
        if row.get("dirty"):
            continue
        for seat in row["seats"]:
            if seat["doctrine"] == label:
                out.append({"row": row, "seat": seat})
    return out


def audit_facts(label: str, rows: list[dict[str, Any]],
                runs_root: Path) -> dict[str, Any]:
    """Deterministic per-label facts: curves, ranks, switch events."""
    games = _games_for_label(label, rows)
    per_game: list[dict[str, Any]] = []
    for game in games:
        row, seat = game["row"], game["seat"]
        traj_path = Path(runs_root) / row["match_id"] / "trajectory.json"
        points: list[dict[str, Any]] = []
        switches: list[dict[str, str]] = []
        seen: set[str] = set()
        last_doctrine: str | None = None
        if traj_path.exists():
            trajectory = json.loads(traj_path.read_text())["trajectory"]
            for tick in trajectory:
                doctrine = tick["doctrines"].get(str(seat["player_id"]))
                if doctrine is not None:
                    seen.add(doctrine)
                if last_doctrine is not None and doctrine != last_doctrine:
                    switches.append({"turn": tick["turn"],
                                     "from": last_doctrine, "to": doctrine})
                last_doctrine = doctrine
                if tick["turn"] % _SAMPLE_EVERY == 0 \
                        or tick is trajectory[-1]:
                    comp = tick["scores"][seat["civ_name"]]
                    points.append({"t": tick["turn"], "cities": comp["cities"],
                                   "pop": comp["population"],
                                   "units": comp["units"],
                                   "gold": comp["gold"],
                                   "techs": comp["techs"],
                                   "scalar": comp["scalar"]})
        scalars = [s["scalar"] for s in row["seats"]]
        rank = 1 + sum(1 for s in scalars if s > seat["scalar"])
        per_game.append({
            "match_id": row["match_id"], "seed": row["seed"],
            "seat": seat["player_id"], "final_scalar": seat["scalar"],
            "rank": rank, "doctrines_played": sorted(seen),
            "switches": switches, "curve": points,
        })
    wins = sum(1 for g in per_game if g["rank"] == 1)
    mean_rank = (round(sum(g["rank"] for g in per_game) / len(per_game), 4)
                 if per_game else None)
    return {"label": label, "games": len(per_game), "wins": wins,
            "mean_rank": mean_rank, "per_game": per_game}


def doctrine_docs_for(label: str, batch_roster: list[Any]) -> dict[str, Any]:
    """Doctrine definitions + pivot spec for the prompt.

    Static seats get their DOCTRINES entry (thesis / expected signature /
    failure mode included verbatim — flash wrote them; now flash audits
    against them). Adaptive seats get the spec plus BOTH parents."""
    docs: dict[str, Any] = {"label": label}
    for entry in batch_roster:
        if isinstance(entry, dict) and entry.get("spec_id") == label:
            docs["pivot_spec"] = entry
            # parents: the initial doctrine + every switch target in triggers
            parents = [entry.get("initial")] + [
                trigger.get("switch_to") for trigger in entry.get("triggers", [])
                if isinstance(trigger, dict)]
            for doctrine in parents:
                if doctrine in DOCTRINES:
                    docs[doctrine] = DOCTRINES[doctrine]
            return docs
    if label in DOCTRINES:
        docs["doctrine"] = DOCTRINES[label]
    return docs


def audit_prompt(facts: dict[str, Any], docs: dict[str, Any]) -> str:
    return (
        f"AUDIT TARGET: seat label {facts['label']}\n\n"
        f"DOCTRINE/SPEC (what this bot is supposed to execute):\n"
        f"{json.dumps(docs, indent=1)}\n\n"
        f"OUTCOMES: {facts['games']} clean games, {facts['wins']} wins, "
        f"mean_rank {facts['mean_rank']} (1 is best of 4).\n\n"
        f"PER-GAME FACTS (locally computed from the recorded trajectories; "
        f"curves sampled every {_SAMPLE_EVERY} turns + final turn):\n"
        f"{json.dumps(facts['per_game'], indent=1)}\n\n"
        "TASK: judge whether this bot EFFECTIVELY EXECUTED its strategy. "
        "Compare each curve against the doctrine's thesis and "
        "expected_signature; for pivot seats judge whether switches fired, "
        "whether their timing tracked the game state, and whether any "
        "flip-flopping helped or hurt. Cite only numbers present above.\n\n"
        "Reply with one JSON object exactly like this contract:\n"
        f"{VERDICT_CONTRACT}"
    )


def validate_verdict(data: Any, label: str) -> tuple[dict[str, Any] | None,
                                                      str | None]:
    """(verdict, problem). Loose contract check — fail-soft, never raises."""
    if not isinstance(data, dict):
        return None, f"{label}: verdict is not a JSON object"
    problems: list[str] = []
    if not isinstance(data.get("executing_strategy"), bool):
        problems.append("executing_strategy must be a boolean")
    score = data.get("execution_score_0_10")
    if not isinstance(score, (int, float)) or not 0 <= score <= 10:
        problems.append("execution_score_0_10 must be a number 0..10")
    for field in ("evidence", "deviations", "recommended_changes"):
        if not isinstance(data.get(field), list):
            problems.append(f"{field} must be a list")
    if problems:
        return None, f"{label}: " + "; ".join(problems)
    return data, None
