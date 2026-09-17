"""Research batch planning: roster × Latin-square seat rotation × seed blocks.

A batch assigns a seat-count-sized doctrine roster to seats across several
map seeds such that WITHIN every seed block every doctrine occupies every
seat exactly once (all cyclic rotations of the roster, one per game). Seat
position and map seed are therefore orthogonal to doctrine by construction —
the variance normalization the research loop relies on.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class GamePlan:
    """One planned game: a seed, a rotation of the roster into seats."""

    match_id: str
    seed: int
    rotation: int
    seats: tuple[tuple[int, str], ...]  # (player_id, doctrine) ascending
    max_turns: int


def latin_seats(roster: list[str], rotation: int) -> dict[int, str]:
    """Seat ``s`` gets ``roster[(s + rotation) % len(roster)]``.

    The len(roster) distinct rotations form a reduced Latin square: across
    one full rotation set every doctrine sits at every seat exactly once.
    """
    n = len(roster)
    return {s: roster[(s + rotation) % n] for s in range(n)}


def plan_batch(batch_id: str, roster: list[str], seeds: list[int],
               max_turns: int = 40) -> list[GamePlan]:
    """All games of a batch: len(seeds) × len(roster) games.

    Per seed the full rotation set runs (Latin-square seat coverage within
    the seed block); the rotation offset advances with the seed index so
    seed↔rotation pairing is decorrelated.
    """
    n = len(roster)
    if n not in (2, 4):
        raise ValueError(f"roster size must be 2 or 4 (seat counts), got {n}")
    if len(set(seeds)) != len(seeds):
        raise ValueError("seeds must be distinct within a batch")
    if len(set(roster)) != n and n == 2:
        raise ValueError("a 2-seat roster needs distinct doctrines")
    plans: list[GamePlan] = []
    for seed_index, seed in enumerate(seeds):
        for k in range(n):
            rotation = (k + seed_index) % n
            seats = latin_seats(roster, rotation)
            plans.append(GamePlan(
                match_id=f"{batch_id}-s{seed}-r{k}", seed=seed,
                rotation=rotation, seats=tuple(sorted(seats.items())),
                max_turns=max_turns))
    return plans
