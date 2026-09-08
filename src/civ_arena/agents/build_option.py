"""Closed-loop develop-city option with a threat interrupt (advisory only).

The option spans turns for one owned city: it registers a pre-action forecast
when the executor accepts a production choice, censors the forecast when a
confirmed threat appears under a non-defender build, and resolves the outcome
from later queue observations. It composes bounded advisory context for the
strategic controller's decision metadata. It never widens the executor's
choices: the production policy's defense override remains the only behavioral
effect of a threat.
"""
from __future__ import annotations

from collections.abc import Mapping

from civ_arena.agents.economic_forecast import (
    classify_outcome,
    compare_alternatives,
    forecast_completion_turn,
)
from civ_arena.agents.production_policy import DEFENDERS, THREAT_RADIUS
from civ_arena.agents.strategy_directive import coordinate

OPTION_NAME = 'develop_city_under_threat_watch/v1'
MAX_PENDING = 8
MAX_RECENT_OUTCOMES = 8
MAX_ADVISORY_CITIES = 2


def _queue_items(city: Mapping) -> list[str]:
    value = city.get('production_queue')
    if isinstance(value, str):
        value = [value] if value else []
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str)]


def _near_any_city(unit: Mapping, cities: list) -> bool:
    coord = unit.get('coord')
    if not isinstance(coord, str):
        return False
    try:
        q, r = coordinate(coord)
    except ValueError:
        return False
    for city in cities:
        target = city.get('coord')
        if not isinstance(target, str):
            continue
        try:
            cq, cr = coordinate(target)
        except ValueError:
            continue
        if max(abs(q - cq), abs(r - cr), abs(q + r - cq - cr)) <= THREAT_RADIUS:
            return True
    return False


def barbarians_near_owned_cities(units: list, cities: list, *,
                                 player_id: int) -> list[str]:
    """Same observable the production policy uses; one threat source of truth."""
    return sorted(
        unit['unit_id'] for unit in units
        if unit.get('owner', unit.get('owner_id')) != player_id
        and unit.get('is_barbarian') is True
        and isinstance(unit.get('unit_id'), str)
        and _near_any_city(unit, cities))


class DevelopmentOptionMonitor:
    """One instance per strategic runtime; fresh-only like the controller itself."""

    def __init__(self, player_id: int) -> None:
        if type(player_id) is not int or player_id < 0:
            raise ValueError('development option requires a player identity')
        self.player_id = player_id
        self._pending: dict[str, dict] = {}
        self._recent_outcomes: list[dict] = []

    def register(self, forecast: Mapping) -> None:
        """Hold one accepted forecast per city; a different re-issue supersedes."""
        cid = forecast['city_id']
        previous = self._pending.get(cid)
        if previous is not None and previous['forecast']['item_id'] != forecast['item_id']:
            self._retain({'city_id': cid, 'item_id': previous['forecast']['item_id'],
                          'observed_turn': forecast['issued_turn'],
                          'estimated_completion_turn': None,
                          'event': 'invalidated_superseded',
                          'detail': 'a new accepted production replaced the pending plan'})
        self._pending[cid] = {'forecast': dict(forecast), 'censored': None,
                              'censored_turn': None, 'overdue_reported': False}

    def _retain(self, outcome: dict) -> None:
        self._recent_outcomes.append(outcome)
        del self._recent_outcomes[:-MAX_RECENT_OUTCOMES]

    def observe(self, *, turn: int, cities: list, units: list) -> list[dict]:
        """Resolve pending options against fresh observations; returns audit events.

        Runs before this turn's production actions so an interrupt is visible
        before the next primitive effect, never only after it.
        """
        events: list[dict] = []
        own = [city for city in cities
               if city.get('owner', city.get('owner_id')) == self.player_id]
        threats = barbarians_near_owned_cities(units, own, player_id=self.player_id)
        by_id = {city['city_id']: city for city in own}
        for cid in sorted(self._pending):
            state = self._pending[cid]
            forecast = state['forecast']
            item = forecast['item_id']
            if threats and item not in DEFENDERS and state['censored'] is None:
                state['censored'] = 'threat_interrupt'
                state['censored_turn'] = turn
                events.append({'city_id': cid, 'item_id': item, 'turn': turn,
                               'event': 'threat_censored',
                               'detail': 'no_material_threat assumption invalidated; '
                                         'defense preempts investment',
                               'threat_unit_ids': list(threats)})
            city = by_id.get(cid)
            if city is None:
                events.append({'city_id': cid, 'item_id': item, 'turn': turn,
                               'event': 'invalidated_city_unobserved',
                               'detail': 'owned city no longer observed'})
                self._retain(events[-1])
                del self._pending[cid]
                continue
            outcome = classify_outcome(forecast=forecast, observed_turn=turn,
                                       queue_items=_queue_items(city))
            if outcome is None:
                continue
            if outcome['outcome'] == 'overdue_pending':
                if state['overdue_reported']:
                    continue
                state['overdue_reported'] = True
            else:
                if state['censored'] is not None:
                    outcome['censored'] = state['censored']
                    outcome['censored_turn'] = state['censored_turn']
                self._pending.pop(cid, None)
            outcome['event'] = outcome.pop('outcome')
            self._retain(outcome)
            events.append(outcome)
        return events

    def advisory(self, *, turn: int, cities: list, production: Mapping,
                 preferences: list) -> dict:
        """Bounded advisory context for decision metadata.

        Plans use observed catalogs and preferences only; the executor's
        inventory evaluation is intentionally not duplicated here.
        """
        own = [city for city in cities
               if city.get('owner', city.get('owner_id')) == self.player_id]
        pending = [{'city_id': cid, 'item_id': state['forecast']['item_id'],
                    'estimated_completion_turn': forecast_completion_turn(
                        state['forecast']),
                    'censored': state['censored']}
                   for cid, state in sorted(self._pending.items())[:MAX_PENDING]]
        plans: dict[str, dict] = {}
        for city in sorted(own, key=lambda row: row['city_id']):
            cid = city['city_id']
            if _queue_items(city) or cid not in production:
                continue
            catalog = production[cid]
            candidates = [{'item_id': row['item_id'], 'kind': row['kind'],
                           'eligible': True,
                           'reason': 'catalog_observed_inventory_not_evaluated'}
                          for row in catalog if isinstance(row, Mapping)
                          and isinstance(row.get('item_id'), str)]
            plans[cid] = compare_alternatives(turn=turn, city_id=cid,
                                              candidates=candidates, catalog=catalog,
                                              preferences=list(preferences))
            if len(plans) >= MAX_ADVISORY_CITIES:
                break
        return {'version': 1, 'option': OPTION_NAME,
                'authority': 'advisory_only_executor_unchanged',
                'basis': 'catalog_and_preferences_only_inventory_not_evaluated',
                'pending': pending, 'recent_outcomes': list(self._recent_outcomes),
                'city_plans': plans}
