"""Aggregate batch rows into variance-normalized strategy tables.

Descriptive, integer-first, no significance claims (the
scripts/strategy_benchmark.py posture): 4-seat ranks have no exact paired
test, so this module reports exact integer sums and means-as-ratios, and
exposes the SEAT decomposition that separates doctrine signal from seat
luck. Strong hypotheses get a paired 2-seat follow-up through
scripts/league.py where the exact one-sided binomial IS valid.
"""

from __future__ import annotations

from typing import Any


def seat_ranks(row: dict[str, Any]) -> dict[int, int]:
    """Competition ranking within one game: equal scalars share the better
    rank; ties by ascending player_id for a deterministic order."""
    ordered = sorted(row["seats"],
                     key=lambda e: (-e["scalar"], e["player_id"]))
    ranks: dict[int, int] = {}
    prev_scalar: int | None = None
    prev_rank = 0
    for index, entry in enumerate(ordered):
        if prev_scalar is not None and entry["scalar"] == prev_scalar:
            ranks[entry["player_id"]] = prev_rank
        else:
            ranks[entry["player_id"]] = index + 1
            prev_rank = index + 1
            prev_scalar = entry["scalar"]
    return ranks


def aggregate(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Per-strategy, per-strategy×seat, and per-seed-cluster tables.

    Only clean rows (``dirty`` False) feed the aggregates; dirty rows are
    counted and listed, never inferred from.
    """
    clean = [r for r in rows if not r["dirty"]]
    dirty_ids = sorted(r["match_id"] for r in rows if r["dirty"])

    per_strategy: dict[str, dict[str, int]] = {}
    seat_decomp: dict[tuple[str, int], dict[str, int]] = {}
    seed_clusters: dict[int, dict[str, Any]] = {}

    for row in clean:
        ranks = seat_ranks(row)
        for entry in row["seats"]:
            doctrine = entry["doctrine"]
            rank = ranks[entry["player_id"]]
            strat = per_strategy.setdefault(doctrine, {
                "matches": 0, "wins": 0, "ties": 0, "rank_sum": 0,
                "scalar_sum": 0, "firsts": 0,
            })
            strat["matches"] += 1
            strat["rank_sum"] += rank
            strat["scalar_sum"] += entry["scalar"]
            if row["winner_pid"] == entry["player_id"]:
                strat["wins"] += 1
                strat["firsts"] += 1
            elif row["winner_pid"] is None:
                strat["ties"] += 1
            cell = seat_decomp.setdefault((doctrine, entry["player_id"]), {
                "n": 0, "rank_sum": 0, "scalar_sum": 0,
            })
            cell["n"] += 1
            cell["rank_sum"] += rank
            cell["scalar_sum"] += entry["scalar"]
            cluster = seed_clusters.setdefault(row["seed"], {})
            cell_c = cluster.setdefault(doctrine, {"rank_sum": 0, "games": 0})
            cell_c["rank_sum"] += rank
            cell_c["games"] += 1

    def mean(total: int, count: int) -> float | None:
        return round(total / count, 4) if count else None

    per_strategy_out = {
        doctrine: {
            **table,
            "mean_rank": mean(table["rank_sum"], table["matches"]),
            "mean_scalar": mean(table["scalar_sum"], table["matches"]),
        }
        for doctrine, table in sorted(per_strategy.items())
    }
    seat_decomp_out = {
        f"{doctrine}@seat{player_id}": {
            **cell,
            "mean_rank": mean(cell["rank_sum"], cell["n"]),
        }
        for (doctrine, player_id), cell in sorted(seat_decomp.items())
    }
    return {
        "games": {"total": len(rows), "clean": len(clean),
                  "dirty": len(rows) - len(clean), "dirty_ids": dirty_ids},
        "per_strategy": per_strategy_out,
        "seat_decomposition": seat_decomp_out,
        "seed_clusters": {
            str(seed): cluster for seed, cluster in sorted(seed_clusters.items())
        },
    }
