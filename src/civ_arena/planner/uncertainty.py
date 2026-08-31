"""M16c — typed uncertainty over beliefs, derived at read time.

The M11 no-expiry invariant is PRESERVED: the belief STORE keeps every
last-seen entry forever (an unobserved death must not erase the last
known position). What changes is that the DETERMINIZED state materializes
a derived confidence per foreign entity — an integer 0..10000 (canonical
JSON bans floats; 10000 = certainly-there) — which option predicates
CONSUME. The store keeps the ghost; the strategy stops charging at it.

Derivation, declared:
- own entities: confidence 10000 (full self-knowledge);
- a foreign entity seen ``d`` turns ago: ``max(0, 10000 - 2500*d)`` —
  four turns of silence exhausts it (SCOUT sight ranges and infantry
  tempo make a military contact older than that strategic noise);
- an entry CONTRADICTED by a live observation (suppressed at build, M15d)
  never reaches the doc, so it carries no confidence at all.

Consumers (the layer must not be a dead field — M11's ``confidence``
lesson): ``rush`` initiation requires a foreign target at or above
RUSH_CONF_MIN; ``defend``'s threat detection counts only foreign military
at or above THREAT_CONF_MIN. Both are strategic behavior changes, not
cosmetic annotations: a stale ghost no longer launches an attack march
or a walls-first pivot.
"""

from __future__ import annotations

from typing import Any

# fixed-point 0..10000
CONF_MAX = 10000
STALENESS_PENALTY_PER_TURN = 2500

RUSH_CONF_MIN = 5000      # attack only recently-sighted targets
THREAT_CONF_MIN = 4000    # defend against probably-real threats


def staleness_confidence(last_seen_turn: int, turn: int) -> int:
    d = max(0, turn - last_seen_turn)
    return max(0, CONF_MAX - STALENESS_PENALTY_PER_TURN * d)


def unit_confidence(belief: Any, uid: str, turn: int) -> int:
    """Duck-typed on PlannerBelief (no import — belief imports this module
    for the materialization constants; the cycle stops here)."""
    entry = belief.foreign_units.get(uid)
    if entry is None:
        return CONF_MAX if uid in belief.own_units else 0
    return staleness_confidence(entry["last_seen_turn"], turn)


def confident_foreign_units(state: Any, pid: int, floor: int) -> list[dict]:
    """Foreign units from the determinized doc at or above the confidence
    floor — the consumer-side filter every gated predicate uses. Own
    entities and prior-roster units carry no key (= CONF_MAX semantics:
    full setup knowledge, deliberately not decayed)."""
    return [u for u in state.units.values()
            if u["owner"] != pid and u.get("confidence", CONF_MAX) >= floor]
