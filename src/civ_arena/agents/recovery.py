"""Observed-health recovery overlay; never changes or replaces a unit's mission.

No engine calls or clocks. begin_turn is a proposal; only commit after successful
turn closure advances persistence. Unknown health never proves recovery complete.
"""
from __future__ import annotations

import copy
from dataclasses import asdict, dataclass

MAX_HEALTH = 1_000_000
MAX_TRACKED_UNITS = 512


def observed_health(unit: dict) -> dict:
    hp, maximum = unit.get("hp"), unit.get("max_hp")
    valid = (unit.get("health_valid") is True and type(hp) is int
             and type(maximum) is int and 0 < maximum <= MAX_HEALTH and 0 <= hp <= maximum)
    return {"hp": hp if valid else None, "max_hp": maximum if valid else None,
            "health_valid": valid}


@dataclass(frozen=True)
class RecoveryPolicy:
    """Configurable hysteresis heuristics, not calibrated strategy thresholds."""
    enter_below_percent: int = 70
    resume_at_percent: int = 90
    no_gain_turns: int = 3

    def __post_init__(self):
        if (type(self.enter_below_percent) is not int or type(self.resume_at_percent) is not int
                or not 1 <= self.enter_below_percent < self.resume_at_percent <= 100):
            raise ValueError("recovery thresholds require 1 <= enter < resume <= 100")
        if type(self.no_gain_turns) is not int or not 1 <= self.no_gain_turns <= 60:
            raise ValueError("recovery no-gain turns must be between 1 and 60")


class RecoveryTracker:
    def __init__(self, policy: RecoveryPolicy | None = None):
        self.policy = RecoveryPolicy() if policy is None else policy
        if not isinstance(self.policy, RecoveryPolicy):
            raise ValueError("recovery requires an explicit RecoveryPolicy")
        self._units: dict[str, dict] = {}
        self._completed_turn = 0

    def begin_turn(self, units: list[dict], *, player_id: int, turn: int,
                   completed_turn: int) -> dict:
        if (type(turn) is not int or type(completed_turn) is not int
                or completed_turn != self._completed_turn or turn != completed_turn + 1):
            raise ValueError("recovery requires consecutive completed own turns")
        if type(player_id) is not int or player_id < 0:
            raise ValueError("recovery requires an explicit player identity")
        if not isinstance(units, list) or len(units) > MAX_TRACKED_UNITS:
            raise ValueError("recovery roster unavailable or oversized")
        owned = {}
        for unit in units:
            owner = unit.get("owner_id", unit.get("owner"))
            if type(owner) is not int:
                raise ValueError("recovery requires explicit observed ownership")
            if owner != player_id:
                continue
            uid = unit.get("unit_id")
            if not isinstance(uid, str) or not uid or uid in owned:
                raise ValueError("recovery requires unique owned unit identities")
            owned[uid] = unit
        pending, transitions, reviews = {}, [], set()
        for uid in sorted(set(self._units) - set(owned)):
            transitions.append({"unit_id": uid, "reason": "no_longer_observed_owned"})
        for uid, unit in sorted(owned.items()):
            health = observed_health(unit)
            previous = self._units.get(uid)
            recovering = bool(previous and previous["recovering"])
            if health["health_valid"]:
                hp, maximum = health["hp"], health["max_hp"]
                threshold = (self.policy.resume_at_percent if recovering
                             else self.policy.enter_below_percent)
                if hp * 100 >= maximum * threshold:
                    if previous:
                        transitions.append({"unit_id": uid, **health, "reason":
                            "observed_recovery_complete" if recovering else
                            "health_observation_restored"})
                    continue
                recovering = True
            row = {"unit_id": uid, **health, "observed_turn": turn,
                   "started_turn": previous["started_turn"] if previous else turn,
                   "recovering": recovering,
                   "state": "recovering" if health["health_valid"] else "health_unavailable",
                   "no_gain_turns": 0, "no_gain_reported": False}
            if previous:
                # Compare only valid observations with the same native maximum.
                comparable = (health["health_valid"] and previous["health_valid"]
                              and health["max_hp"] == previous["max_hp"])
                gained = comparable and health["hp"] > previous["hp"]
                damaged = comparable and health["hp"] < previous["hp"]
                if damaged:
                    reviews.add("recovery_under_damage")
                if not gained:
                    row["no_gain_turns"] = previous["no_gain_turns"] + 1
                    row["no_gain_reported"] = previous["no_gain_reported"]
                if (row["no_gain_turns"] >= self.policy.no_gain_turns
                        and not row["no_gain_reported"]):
                    reviews.add("recovery_no_observed_gain")
                    row["no_gain_reported"] = True
            else:
                transitions.append({"unit_id": uid, **health, "reason": row["state"]})
            pending[uid] = row
        return {"turn": turn, "policy": asdict(self.policy), "units": pending,
                "transitions": transitions, "review_reasons": sorted(reviews),
                "authority": "projected_owned_health; persistent_mission_unchanged"}

    def commit(self, proposal: dict) -> None:
        if proposal["turn"] != self._completed_turn + 1:
            raise ValueError("recovery commit must close the next own turn exactly once")
        self._units = copy.deepcopy(proposal["units"])
        self._completed_turn = proposal["turn"]
