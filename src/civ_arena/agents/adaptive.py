"""Adaptive doctrine switcher: agents that change tactics mid-game.

``policy: adaptive`` starts with one scripted doctrine and re-resolves the
doctrine at a fixed cadence from MEMORYLESS trigger predicates over the
agent's own projected observation ("under expansion pressure and losing
units → stop marching, turtle up").

The load-bearing design decision: the doctrine at turn *t* is a PURE
FUNCTION of ``(t, spec, observation_at_t)`` — re-derived, never
remembered. Consequences:

- resume is free (the coordinator already restores ``agent-{pid}`` rng and
  nothing else; there is no doctrine state to checkpoint);
- replay is free (the doctrine timeline is recoverable from the log —
  every switch is announced through ``write_diary``);
- v1 predicates are deliberately memoryless. History-dependent triggers
  ("lost 2 units in 5 turns") need log-derived state and a resume story —
  deferred, recorded in research/RESEARCH-LEDGER.md.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# Memoryless predicate vocabulary (config validation refuses unknown keys).
# Naming: *_gte/*_lte are inclusive comparisons on the named observation;
# min_foreign_*_seen means "at least this many foreign entities currently
# visible"; own_units_hp_below_frac means "the wounded-est own unit that
# reports max_hp is below this fraction".
PREDICATES: frozenset[str] = frozenset({
    "turn_gte", "turn_lte",
    "own_cities_lte", "own_cities_gte",
    "own_population_gte",
    "own_units_lte",
    "own_gold_gte", "own_techs_gte",
    "min_foreign_units_seen", "min_foreign_cities_seen",
    "own_units_hp_below_frac",
})

_COMPARATORS: dict[str, tuple[str, str]] = {
    "turn_gte": ("turn", "gte"),
    "turn_lte": ("turn", "lte"),
    "own_cities_lte": ("own_cities", "lte"),
    "own_cities_gte": ("own_cities", "gte"),
    "own_population_gte": ("own_population", "gte"),
    "own_units_lte": ("own_units", "lte"),
    "own_gold_gte": ("own_gold", "gte"),
    "own_techs_gte": ("own_techs", "gte"),
    "min_foreign_units_seen": ("foreign_units_seen", "gte"),
    "min_foreign_cities_seen": ("foreign_cities_seen", "gte"),
    "own_units_hp_below_frac": ("own_units_hp_frac", "lt"),
}


@dataclass(frozen=True)
class AdaptiveTrigger:
    when: dict[str, float]
    switch_to: str


@dataclass(frozen=True)
class AdaptiveSpec:
    initial: str
    interval: int
    triggers: tuple[AdaptiveTrigger, ...]


def observations(turn: int, overview: dict[str, Any],
                 units: list[dict[str, Any]],
                 cities: list[dict[str, Any]], player_id: int) -> dict[str, float]:
    """The memoryless observation vector, from projected docs only.

    Projected shapes differ by side: own cities keep the state ``owner``
    key, foreign city projections carry ``owner_id`` (visibility.py
    FOREIGN_CITY_FIELDS); units are uniform ``owner_id``. hp_frac reads
    only units that report max_hp — if none do (or nothing is wounded)
    the fraction is 1.0, which can never fire the trigger.
    """
    own_units = [u for u in units if u.get("owner_id") == player_id]
    foreign_units = [u for u in units if u.get("owner_id") != player_id]
    own_cities: list[dict[str, Any]] = []
    foreign_cities: list[dict[str, Any]] = []
    for c in cities:
        if c.get("owner") == player_id or c.get("owner_id") == player_id:
            own_cities.append(c)
        else:
            foreign_cities.append(c)
    wounded = [u for u in own_units
               if isinstance(u.get("max_hp"), int) and u["max_hp"] > 0
               and isinstance(u.get("hp"), int)]
    hp_frac = (min(u["hp"] / u["max_hp"] for u in wounded) if wounded else 1.0)
    you = overview.get("you", {})
    return {
        "turn": turn,
        "own_cities": len(own_cities),
        "own_population": sum(c.get("population", 0) for c in own_cities),
        "own_units": len(own_units),
        "own_gold": you.get("gold", 0),
        "own_techs": len(you.get("researched", [])),
        "foreign_units_seen": len(foreign_units),
        "foreign_cities_seen": len(foreign_cities),
        "own_units_hp_frac": hp_frac,
    }


def resolve(spec: AdaptiveSpec, turn: int, overview: dict[str, Any],
            units: list[dict[str, Any]], cities: list[dict[str, Any]],
            player_id: int) -> tuple[str, str]:
    """(doctrine_id, reason). First matching trigger wins (list order is
    documented semantics); no match resolves to ``initial``. Pure."""
    obs = observations(turn, overview, units, cities, player_id)
    for trigger in spec.triggers:
        matched = []
        fired = True
        for key, value in trigger.when.items():
            obs_key, comparator = _COMPARATORS[key]
            actual = obs[obs_key]
            if comparator == "gte":
                ok = actual >= value
            elif comparator == "lte":
                ok = actual <= value
            else:  # lt
                ok = actual < value
            if not ok:
                fired = False
                break
            matched.append(f"{key}={value}")
        if fired:
            return trigger.switch_to, ",".join(matched)
    return spec.initial, "initial"


@dataclass
class AdaptiveRuntime:
    """Scripted-equivalent runtime whose doctrine re-resolves on a cadence.

    Subclasses nothing deliberately: it owns the same ``rng`` checkpoint
    contract as ``ScriptedRuntime`` (attribute, restored by ``load_rng``)
    and forwards execution to ``scripted.run_policy`` with the resolved
    doctrine. The rng is the ONLY state; ``_current`` is a derived cache
    that is safe to lose (re-derived on the next evaluation boundary).
    """

    profile: Any
    spec: AdaptiveSpec
    rng: Any = None
    _current: str | None = None

    def __post_init__(self) -> None:
        if self.rng is None:
            import random

            self.rng = random.Random(self.profile.seed)

    @property
    def current_doctrine(self) -> str | None:
        return self._current

    async def take_turn(self, facade: Any) -> None:
        from civ_arena.agents.scripted import DOCTRINES, run_policy

        overview = await facade.get_overview()
        turn = overview["turn"]
        if self._current is None or turn % self.spec.interval == 0:
            units = await facade.get_units()
            cities = await facade.get_cities()
            doctrine, reason = resolve(
                self.spec, turn, overview, units, cities,
                self.profile.player_id)
            if doctrine != self._current:
                await self._announce(facade, turn, doctrine, reason)
                self._current = doctrine
        # resolve() always returns a doctrine, so _current is set here
        await run_policy(self, facade, doctrine=DOCTRINES[self._current])

    async def _announce(self, facade: Any, turn: int, doctrine: str,
                        reason: str) -> None:
        writer = getattr(facade, "write_diary", None)
        if writer is None:
            return
        await writer(f"ADAPTIVE: t={turn} doctrine={doctrine} via {reason}")
