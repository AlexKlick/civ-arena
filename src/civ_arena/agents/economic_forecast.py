"""S3 bounded economic forecasts over observed production catalogs.

Advisory-only: forecasts and comparisons describe consequences of choices the
existing production policy already surfaced. They never widen the executor's
options, replace its guards, or fabricate quantities no accessor observes
(docs/strategic-autopilot.md S3/S4). A forecast record is issued once and
never edited; later observations produce separate outcome records.
"""
from __future__ import annotations

from collections.abc import Mapping

from civ_arena.agents.production_policy import DEFENDERS

FORECAST_VERSION = 1
HORIZONS = (1, 3, 5)
MAX_COMPARE_CANDIDATES = 4
SELECTION_ALGORITHM = 's3_static_v1'
SELECTION_WEIGHTS = {
    'confirmed_threat_preempts': 'defense candidates precede all investment',
    'preference_order': 'directive production_preferences order',
    'earlier_completion': 'lower engine completion turn',
    'lower_static_maintenance': 'when static effects are supplied',
}

# Quantities the live adapter cannot currently read (city buckets are
# declared placeholder constants in the civ6 response parser). A forecast
# reports them as unsupported; it never substitutes a guessed number.
UNSUPPORTED_ALWAYS = (
    'accumulated_production_progress',
    'production_per_turn_rate',
    'food_bucket_and_growth_rate',
    'housing_amenities_live',
    'gold_maintenance_live',
    'active_modifier_stack',
)


def _cost_or_none(value: object) -> int | None:
    return value if type(value) is int and value >= 0 else None


def _turns_or_none(value: object) -> int | None:
    """Only a genuine positive int is a completion basis; missing stays missing."""
    return value if type(value) is int and value >= 1 else None


def build_forecast(*, turn: int, city_id: str, item_id: str, row: Mapping,
                   threats: tuple[str, ...] | list[str] = ()) -> dict:
    """One pre-action production-completion forecast over the observed catalog row."""
    if type(turn) is not int or turn < 1:
        raise ValueError('forecast requires a positive observed turn')
    if not isinstance(city_id, str) or not city_id:
        raise ValueError('forecast requires a city identity')
    if not isinstance(item_id, str) or not item_id:
        raise ValueError('forecast requires an item identity')
    if not isinstance(row, Mapping):
        raise ValueError('forecast requires the observed catalog row')
    unsupported = list(UNSUPPORTED_ALWAYS)
    turns = _turns_or_none(row.get('turns'))
    if turns is None:
        unsupported.append('engine_turns_estimate')
    ranges: list[dict] = []
    if turns is not None:
        completion = turn + turns
        ranges.append({'metric': 'production_completion_turn', 'lower': completion,
                       'upper': completion, 'method': 'engine_catalog_estimate'})
        ranges.extend({'metric': 'completes_within_horizon', 'horizon': horizon,
                       'value': completion <= turn + horizon} for horizon in HORIZONS)
    return {
        'version': FORECAST_VERSION, 'city_id': city_id, 'item_id': item_id,
        'item_kind': row.get('kind') if isinstance(row.get('kind'), str) else None,
        'issued_turn': turn,
        'observed': {'turn': turn, 'catalog_cost': _cost_or_none(row.get('cost')),
                     'engine_turns_estimate': turns,
                     'confirmed_threat_unit_ids': list(threats)},
        'assumptions': (
            {'id': 'engine_rate_basis', 'source': 'catalog engine turns estimate',
             'invalidation': 'a later catalog observation reports a different turns estimate'},
            {'id': 'queue_unchanged',
             'source': 'set_city_production acceptance is the only observed queue writer',
             'invalidation': 'the observed queue no longer starts with this item'},
            {'id': 'no_material_threat',
             'source': 'no confirmed is_barbarian contact within THREAT_RADIUS of an '
                       'owned city at issue time',
             'invalidation': 'such a contact appears while the item is pending'}),
        'ranges': tuple(ranges),
        'unsupported': tuple(sorted(unsupported)),
    }


def forecast_completion_turn(forecast: Mapping) -> int | None:
    for entry in forecast['ranges']:
        if entry['metric'] == 'production_completion_turn':
            return entry['upper']
    return None


def classify_outcome(*, forecast: Mapping, observed_turn: int,
                     queue_items: list[str]) -> dict | None:
    """Later-observation verdict as a separate record; the forecast is never edited.

    None means the observation resolves nothing (build still pending, on time).
    """
    if type(observed_turn) is not int or observed_turn < 1:
        raise ValueError('outcome requires a positive observed turn')
    item = forecast['item_id']
    completion = forecast_completion_turn(forecast)
    base = {'city_id': forecast['city_id'], 'item_id': item,
            'observed_turn': observed_turn,
            'estimated_completion_turn': completion}
    if queue_items and queue_items[0] == item:
        if completion is not None and observed_turn > completion:
            return {**base, 'outcome': 'overdue_pending',
                    'detail': 'still building past the estimated completion turn'}
        return None
    if queue_items:
        return {**base, 'outcome': 'invalidated_queue_changed',
                'detail': 'queue head changed before observed completion'}
    verdict = ('completion_unsupported' if completion is None else
               'in_estimated_window' if observed_turn <= completion else 'late')
    return {**base, 'outcome': 'completed', 'verdict': verdict}


def _bounded_set(candidates: list[dict], preferences: list[str],
                 threats: tuple[str, ...] | list[str]) -> list[dict]:
    """Bounded mirror of the production-policy cascade; not the full legal catalog."""
    eligible = [row for row in candidates if row.get('eligible')]
    if threats:
        defense = [item for item in preferences if item in DEFENDERS] + list(DEFENDERS)

        def defense_rank(row: dict) -> tuple[int, str]:
            item = row['item_id']
            return (defense.index(item) if item in defense else len(defense), item)

        return sorted(eligible, key=defense_rank)[:MAX_COMPARE_CANDIDATES]

    def growth_rank(row: dict) -> tuple[int, int, str]:
        item, kind = row['item_id'], row['kind']
        preference = preferences.index(item) if item in preferences else len(preferences)
        tier = (0 if item in preferences else 1 if kind == 'building'
                else 2 if kind in ('district', 'project') else 3)
        return tier, preference, item

    return sorted(eligible, key=growth_rank)[:MAX_COMPARE_CANDIDATES]


def compare_alternatives(*, turn: int, city_id: str, candidates: list[dict],
                         catalog: list, preferences: list[str],
                         threats: tuple[str, ...] | list[str] = (),
                         statics: Mapping[str, dict] | None = None) -> dict:
    """Compare one bounded candidate set at equal budget: per-item timing plus the
    first composed sequence (investment-then-next). Selection mirrors the existing
    deterministic cascade; the counterfactual branch carries what it would cost."""
    if type(turn) is not int or turn < 1:
        raise ValueError('comparison requires a positive observed turn')
    rows = {row['item_id']: row for row in catalog
            if isinstance(row, Mapping) and isinstance(row.get('item_id'), str)}
    unsupported = list(UNSUPPORTED_ALWAYS)
    if statics is None:
        unsupported.append('static_yield_and_maintenance_effects')
    summaries = []
    for row in _bounded_set(candidates, preferences, threats):
        item = row['item_id']
        observed = rows.get(item, {})
        turns = _turns_or_none(observed.get('turns'))
        entry = {'item_id': item, 'kind': row['kind'],
                 'eligible_reason': row.get('reason'),
                 'cost': _cost_or_none(observed.get('cost')),
                 'engine_turns': turns,
                 'completion_turn': turn + turns if turns is not None else None}
        if statics is not None and item in statics:
            entry['static_effects'] = statics[item]
        summaries.append(entry)
    counterfactual = None
    timed = [entry for entry in summaries if entry['completion_turn'] is not None]
    if len(timed) >= 2:
        first, second = timed[0], timed[1]
        counterfactual = {
            'description': 'build the first candidate, then the next: composed completion',
            'first_item_id': first['item_id'], 'second_item_id': second['item_id'],
            'second_completion_turn': first['completion_turn'] + second['engine_turns'],
            'assumption': 'engine estimates compose linearly at constant rates',
            'invalidation': 'either item turns estimate changes or the queue changes'}
    return {
        'version': FORECAST_VERSION, 'city_id': city_id, 'issued_turn': turn,
        'candidate_bound': MAX_COMPARE_CANDIDATES,
        'candidate_coverage': 'bounded mirror of the production-policy cascade; '
                              'not the full legal catalog',
        'observed_threat_unit_ids': list(threats),
        'candidates': summaries,
        'counterfactual': counterfactual,
        'threat_contingency': {
            'condition': 'confirmed is_barbarian contact within THREAT_RADIUS '
                         'of an owned city',
            'consequence': 'defense candidates preempt; pending investment forecasts '
                           'are censored, never silently kept'},
        'selection': {'algorithm': SELECTION_ALGORITHM,
                      'weights': dict(SELECTION_WEIGHTS), 'deterministic': True,
                      'seed': 'deterministic_no_sampling',
                      'recommended': summaries[0]['item_id'] if summaries else None,
                      'runner_up': summaries[1]['item_id'] if len(summaries) > 1 else None},
        'unsupported': tuple(sorted(unsupported)),
    }
