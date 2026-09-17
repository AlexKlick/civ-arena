"""Four-seat sim: golden 2-seat hashes, 4-seat layout, determinism, resume.

The golden test below was captured from UNMODIFIED code (commit a59cb44) BEFORE
the 4-seat refactor landed. It is the bit-identity guarantee for every existing
2-seat config: if these literals ever move, the refactor changed history.
"""

from __future__ import annotations

from civ_arena.canonical import state_hash
from civ_arena.game.sim.layouts import duel_start

GOLDEN_2SEAT = {
    947381: "36cd5136354ef4332681b2be805d4b1882675da917bcb0705202e8a604188b03",
    21: "2d8658d6c5d4649abc66cc65028d03b26174297d7b06cdd86e2915a1d03fd8a7",
    2024: "358e6cbef747027c8394ad812a60a1bd0340628ee8524bfba61c6ff57777c8d6",
    999983: "b2fbf330160af8ed019a4edf64f03d90826d6798a68fe7b3546e4a855e2348bf",
}


def test_duel_start_2seat_golden() -> None:
    """2-seat start docs must hash identically to the pre-refactor capture."""
    for seed, expected in GOLDEN_2SEAT.items():
        assert state_hash(duel_start(seed)) == expected
