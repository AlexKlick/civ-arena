"""Research harness: Latin-square planning, config validity, runner, aggregates."""

from __future__ import annotations

import json
from dataclasses import replace

import pytest

from civ_arena.config import parse_config
from civ_arena.research.aggregate import aggregate, seat_ranks
from civ_arena.research.matrix import latin_seats, plan_batch, seat_spec
from civ_arena.research.runner import config_doc, run_game, summarize_existing

ROSTER = ["turtler", "turtler", "expansionist", "expansionist"]


def test_latin_rotation_set_covers_every_doctrine_seat_pair() -> None:
    distinct = ["a", "b", "c", "d"]
    pairs: set[tuple[int, str]] = set()
    for rotation in range(4):
        seats = latin_seats(distinct, rotation)
        assert len(set(seats.values())) == 4
        pairs |= {(pid, doctrine) for pid, doctrine in seats.items()}
    assert pairs == {(pid, d) for pid in range(4) for d in distinct}


def test_plan_batch_shape_and_ids() -> None:
    plans = plan_batch("b1", ROSTER, [7, 9])
    assert len(plans) == 8
    assert {p.seed for p in plans} == {7, 9}
    ids = [p.match_id for p in plans]
    assert len(set(ids)) == 8
    for seed in (7, 9):
        block = [p for p in plans if p.seed == seed]
        pairs = {(pid, spec.label) for p in block for pid, spec in p.seats}
        assert pairs == {(pid, d) for pid in range(4)
                         for d in ROSTER}, "latin coverage within seed block"
    with pytest.raises(ValueError):
        plan_batch("b", ["a", "b", "c"], [1])
    with pytest.raises(ValueError):
        plan_batch("b", ROSTER, [1, 1])


def test_config_doc_parses_with_seat_validation() -> None:
    plans = plan_batch("b1", ROSTER, [947381])
    doc = config_doc(plans[0])
    spec = parse_config(doc)
    assert spec.player_count == 4
    assert [a.player_id for a in spec.agents] == [0, 1, 2, 3]
    assert spec.agents[0].seed == 947381 * 10 + 0


def _synthetic_rows() -> list[dict]:
    def row(match_id: str, seed: int, scalars: list[int],
            dirty: bool = False) -> dict:
        seats = [
            {"player_id": pid, "agent_id": f"a{pid}",
             "doctrine": ROSTER[pid], "civ_name": f"C{pid}",
             "scalar": scalars[pid]}
            for pid in range(4)
        ]
        ordered = sorted(seats, key=lambda e: (-e["scalar"], e["player_id"]))
        winner = (ordered[0]["player_id"]
                  if ordered[0]["scalar"] > ordered[1]["scalar"] else None)
        return {"match_id": match_id, "seed": seed, "rotation": 0,
                "player_count": 4, "turns": 40,
                "violations": 1 if dirty else 0, "aborted": None,
                "dirty": dirty, "seats": seats,
                "winner_pid": winner,
                "margin": (ordered[0]["scalar"] - ordered[1]["scalar"])
                if winner is not None else 0}

    return [
        row("m1", 7, [500, 300, 400, 200]),
        row("m2", 7, [300, 500, 200, 400]),
        row("m3", 9, [500, 500, 100, 100]),  # tie for first
        row("m4", 9, [100, 100, 100, 100], dirty=True),  # excluded
    ]


def test_seat_ranks_competition_ties() -> None:
    rows = _synthetic_rows()
    assert seat_ranks(rows[2]) == {0: 1, 1: 1, 2: 3, 3: 3}
    assert seat_ranks(rows[0]) == {0: 1, 1: 3, 2: 2, 3: 4}


def test_aggregate_excludes_dirty_and_decomposes_seats() -> None:
    agg = aggregate(_synthetic_rows())
    assert agg["games"] == {"total": 4, "clean": 3, "dirty": 1,
                            "dirty_ids": ["m4"]}
    turtler = agg["per_strategy"]["turtler"]
    # turtler = seats 0/1. m1 ranks (1,3), m2 ranks (3,1), m3 ranks (1,1).
    assert turtler["matches"] == 6
    assert turtler["rank_sum"] == 1 + 3 + 3 + 1 + 1 + 1
    assert turtler["wins"] == 2  # m1 seat0 AND m2 seat1 (m3 is a tie)
    assert turtler["ties"] == 2  # both turtler seats in m3
    assert turtler["mean_rank"] == round(turtler["rank_sum"] / 6, 4)
    seat0 = agg["seat_decomposition"]["turtler@seat0"]
    assert seat0 == {"n": 3, "rank_sum": 1 + 3 + 1, "scalar_sum": 1300,
                     "mean_rank": round(5 / 3, 4)}
    assert "m4" in str(agg["games"]["dirty_ids"])


async def test_run_game_end_to_end_tiny(tmp_path) -> None:

    plans = plan_batch("tiny", ROSTER, [947381])
    plans = [p for p in plans if p.rotation == 0]
    plan = replace(plans[0], max_turns=6)
    row = await run_game(plan, tmp_path)
    assert row["dirty"] is False
    assert row["turns"] == 6
    assert {s["doctrine"] for s in row["seats"]} == set(ROSTER)
    assert all(s["scalar"] > 0 for s in row["seats"])

    run_dir = tmp_path / plan.match_id
    traj = json.loads((run_dir / "trajectory.json").read_text())
    assert [t["turn"] for t in traj["trajectory"]] == [1, 2, 3, 4, 5, 6]
    assert all("scalar" in comp
               for t in traj["trajectory"] for comp in t["scores"].values())
    assert (run_dir / "config.json").exists()
    assert (run_dir / "summary.json").exists()

    # spend discipline: a second run into the same dir refuses
    with pytest.raises(FileExistsError):
        await run_game(plan, tmp_path)
    # and summarize_existing reproduces the same row
    again = summarize_existing(plan, run_dir)
    assert again == row


# --- adaptive pivot seats in research rosters (batch-003 shape) -------

PIVOT = {
    "spec_id": "flood_pivot",
    "initial": "hyperwide_flood",
    "interval": 5,
    "triggers": [{"when": {"min_foreign_units_seen": 3},
                  "switch_to": "turtler"}],
}


def test_seat_spec_normalizes_strings_and_mappings() -> None:
    static = seat_spec("turtler")
    assert static.label == "turtler" and static.policy == "turtler"
    assert static.adaptive is None
    pivot = seat_spec(PIVOT)
    assert pivot.label == "flood_pivot" and pivot.policy == "adaptive"
    assert pivot.adaptive["initial"] == "hyperwide_flood"
    assert pivot.adaptive["triggers"] == PIVOT["triggers"]
    import pytest as _pytest

    with _pytest.raises(ValueError):
        seat_spec({"initial": "turtler"})  # no spec_id
    with _pytest.raises(ValueError):
        seat_spec({"spec_id": "x", "policy": "turtler"})  # maps are adaptive
    with _pytest.raises(ValueError):
        seat_spec(7)


def test_mixed_roster_plans_and_parses() -> None:
    roster = ["hyperwide_flood", "turtler", PIVOT, dict(
        PIVOT, spec_id="turtle_pivot", initial="turtler",
        triggers=[{"when": {"turn_gte": 30, "own_cities_gte": 2},
                   "switch_to": "hyperwide_flood"}])]
    plans = plan_batch("b3", roster, [947381])
    assert len(plans) == 4
    labels = {spec.label for p in plans for _, spec in p.seats}
    assert labels == {"hyperwide_flood", "turtler",
                      "flood_pivot", "turtle_pivot"}
    # the full config path: adaptive blocks must survive parse_config's
    # DOCTRINES/predicate validation (flood_pivot seeds are real doctrines)
    doc = config_doc(plans[0])
    spec = parse_config(doc)
    adaptive = [a for a in spec.agents if a.policy == "adaptive"]
    assert len(adaptive) == 2
    assert {a.agent_id.rsplit("-", 1)[0] for a in adaptive} == {
        "flood_pivot", "turtle_pivot"}
    # pivot labels never masquerade as their parent doctrines in rows
    for plan in plans:
        row_doc = config_doc(plan)
        assert all(a["policy"] in ("adaptive", "hyperwide_flood", "turtler")
                   for a in row_doc["agents"])
