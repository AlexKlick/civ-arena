"""Turn leases: scoping, expiry, release semantics."""

from __future__ import annotations

from civ_arena.arena.turn_lease import TurnLease, validate_lease
from civ_arena.game.adapter import RejectionReason


def make_lease(**kw) -> TurnLease:
    base = dict(
        lease_id="m1-t1-p0", match_id="m1", game_instance_id="g1",
        player_id=0, agent_id="roman", turn=1, granted_seq=0,
    )
    base.update(kw)
    return TurnLease(**base)


def test_lease_scoped_to_player():
    lease = make_lease(player_id=0)
    assert validate_lease(lease, current_turn=1, phase_player=0,
                          player_id=0, agent_id="roman") is None
    assert validate_lease(lease, current_turn=1, phase_player=0,
                          player_id=1, agent_id="korea") == \
        RejectionReason.LEASE_FOREIGN


def test_expired_lease_rejected():
    lease = make_lease(turn=1)
    assert validate_lease(lease, current_turn=2, phase_player=0,
                          player_id=0, agent_id="roman") == \
        RejectionReason.LEASE_EXPIRED


def test_release_revokes():
    lease = make_lease()
    lease.release()
    assert not lease.is_current(1, 0)
    assert validate_lease(lease, current_turn=1, phase_player=0,
                          player_id=0, agent_id="roman") == \
        RejectionReason.LEASE_EXPIRED


def test_no_lease():
    assert validate_lease(None, current_turn=1, phase_player=0,
                          player_id=0, agent_id="roman") == \
        RejectionReason.NO_LEASE


def test_foreign_phase_rejected():
    lease = make_lease(player_id=0)
    assert validate_lease(lease, current_turn=1, phase_player=1,
                          player_id=0, agent_id="roman") == \
        RejectionReason.LEASE_FOREIGN
