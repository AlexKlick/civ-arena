"""Research batch planning: roster × Latin-square seat rotation × seed blocks.

A batch assigns a seat-count-sized doctrine roster to seats across several
map seeds such that WITHIN every seed block every doctrine occupies every
seat exactly once (all cyclic rotations of the roster, one per game). Seat
position and map seed are therefore orthogonal to doctrine by construction —
the variance normalization the research loop relies on.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class SeatSpec:
    """One roster entry. Static doctrines are a name; adaptive seats carry
    the switcher spec (config ``adaptive:`` block shape, validated later by
    parse_config). ``label`` is the identity everywhere downstream —
    agent_id, result rows, aggregates — so a pivot seat never pollutes its
    parent doctrine's stats."""

    label: str
    policy: str  # doctrine id | "adaptive"
    adaptive: dict[str, Any] | None = None


def seat_spec(entry: str | dict[str, Any]) -> SeatSpec:
    """Normalize a roster entry (yaml string or mapping) to a SeatSpec."""
    if isinstance(entry, str):
        return SeatSpec(label=entry, policy=entry)
    if not isinstance(entry, dict):
        raise ValueError(f"roster entry must be a doctrine name or mapping, "
                         f"got {type(entry).__name__}")
    label = entry.get("spec_id")
    if not isinstance(label, str) or not label:
        raise ValueError(f"adaptive roster entry needs a spec_id: {entry}")
    if entry.get("policy", "adaptive") != "adaptive":
        raise ValueError(
            f"roster mapping entries are adaptive seats (policy: adaptive), "
            f"got policy {entry.get('policy')!r} for {label!r}")
    adaptive = {"initial": entry.get("initial"),
                "interval": entry.get("interval", 5),
                "triggers": entry.get("triggers")}
    return SeatSpec(label=label, policy="adaptive", adaptive=adaptive)


@dataclass(frozen=True)
class GamePlan:
    """One planned game: a seed, a rotation of the roster into seats."""

    match_id: str
    seed: int
    rotation: int
    seats: tuple[tuple[int, SeatSpec], ...]  # (player_id, spec) ascending
    max_turns: int


def latin_seats(roster: list[SeatSpec], rotation: int) -> dict[int, SeatSpec]:
    """Seat ``s`` gets ``roster[(s + rotation) % len(roster)]``.

    The len(roster) distinct rotations form a reduced Latin square: across
    one full rotation set every doctrine sits at every seat exactly once.
    """
    n = len(roster)
    return {s: roster[(s + rotation) % n] for s in range(n)}


def plan_batch(batch_id: str, roster: list[str | dict[str, Any]],
               seeds: list[int], max_turns: int = 40) -> list[GamePlan]:
    """All games of a batch: len(seeds) × len(roster) games.

    Per seed the full rotation set runs (Latin-square seat coverage within
    the seed block); the rotation offset advances with the seed index so
    seed↔rotation pairing is decorrelated. Entries normalize via
    ``seat_spec`` (doctrines and adaptive pivot seats mix freely).
    """
    specs = [seat_spec(entry) for entry in roster]
    n = len(specs)
    if n not in (2, 4):
        raise ValueError(f"roster size must be 2 or 4 (seat counts), got {n}")
    if len(set(seeds)) != len(seeds):
        raise ValueError("seeds must be distinct within a batch")
    if len({s.label for s in specs}) != n and n == 2:
        raise ValueError("a 2-seat roster needs distinct labels")
    plans: list[GamePlan] = []
    for seed_index, seed in enumerate(seeds):
        for k in range(n):
            rotation = (k + seed_index) % n
            seats = latin_seats(specs, rotation)
            plans.append(GamePlan(
                match_id=f"{batch_id}-s{seed}-r{k}", seed=seed,
                rotation=rotation, seats=tuple(sorted(seats.items())),
                max_turns=max_turns))
    return plans
