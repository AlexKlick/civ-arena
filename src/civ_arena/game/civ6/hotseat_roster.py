"""Explicit supported hotseat rosters, independent of engine/simulator clocks."""
from __future__ import annotations


def seat_order(players, *, fresh: bool = False) -> list[int]:
    seats = list(players)
    if not 2 <= len(seats) <= 4:
        raise ValueError("hotseat requires two through four distinct seats")
    if any(type(pid) is not int or not 0 <= pid <= 63 for pid in seats):
        raise ValueError("hotseat seat IDs must be integers from 0 through 63")
    if len(set(seats)) != len(seats):
        raise ValueError("hotseat requires distinct seats")
    seats.sort()
    if fresh and seats != list(range(len(seats))):
        raise ValueError("fresh hotseat requires contiguous seats starting at 0")
    return seats
