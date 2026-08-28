"""Turn leases: exclusive, single-holder, validated at every execute.

The spike lease is turn-scoped and in-process (no wall-clock deadline inside
the determinism path — "expiry" means the turn moved on). ``LEASE_EXPIRED``
semantics: the lease's turn no longer matches the engine's current turn.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from civ_arena.game.adapter import RejectionReason


@dataclass
class TurnLease:
    lease_id: str
    match_id: str
    game_instance_id: str
    player_id: int
    agent_id: str
    turn: int
    granted_seq: int
    released: bool = field(default=False)

    def release(self) -> None:
        self.released = True

    def is_current(self, current_turn: int, phase_player: int) -> bool:
        return (
            not self.released
            and self.turn == current_turn
            and (phase_player == -1 or phase_player == self.player_id)
        )


def validate_lease(
    lease: TurnLease | None,
    *,
    current_turn: int,
    phase_player: int,
    player_id: int | None,
    agent_id: str | None,
) -> RejectionReason | None:
    """Session-scoped fast validation; the referee re-validates independently."""
    if lease is None:
        return RejectionReason.NO_LEASE
    if lease.released or lease.turn != current_turn:
        return RejectionReason.LEASE_EXPIRED
    if phase_player != lease.player_id:
        return RejectionReason.LEASE_FOREIGN
    if player_id is not None and lease.player_id != player_id:
        return RejectionReason.LEASE_FOREIGN
    if agent_id is not None and lease.agent_id != agent_id:
        return RejectionReason.LEASE_FOREIGN
    return None
