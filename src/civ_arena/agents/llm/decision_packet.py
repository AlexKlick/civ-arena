"""Bounded observation changes, not forecasts or inferred hidden engine state."""
from __future__ import annotations

import copy
from typing import Any

from civ_arena.agents.llm.context_curator import ContextCurator, selected

UNIT_FIELDS = ('type', 'coord', 'movement', 'max_movement', 'hp', 'hp_bucket', 'fortified')
CITY_FIELDS = ('coord', 'population', 'production_queue', 'hp')
CHANGE_LIMIT = 16


def decision_snapshot(curator: ContextCurator, turn: int) -> dict:
    """Take only projected fields; retain no omniscient facade or engine objects."""
    owned_units = curator.own('get_units')
    snapshot = {
        'turn': turn, 'player_id': curator.player_id,
        'units': {row['unit_id']: selected(row, UNIT_FIELDS) for row in owned_units},
        'cities': {row['city_id']: selected(row, CITY_FIELDS)
                   for row in curator.own('get_cities')},
        'contacts': sorted(row['unit_id'] for row in curator.state['get_units']
                           if row not in owned_units),
        'you': curator.state['get_overview'].get('you', {}),
        'known_tiles': sorted(curator.state['get_visible_map']['tiles']),
    }
    return copy.deepcopy(snapshot)


def _bounded(rows: list[Any]) -> dict:
    return {'items': rows[:CHANGE_LIMIT], 'omitted': max(0, len(rows) - CHANGE_LIMIT)}


def _changed_fields(before: dict, after: dict) -> list[str]:
    return sorted(key for key in before | after
                  if (key in before, before.get(key)) != (key in after, after.get(key)))


def decision_packet(current: dict, previous: dict | None) -> dict:
    """Changes compare strategy-request observations, including intervening quiet turns."""
    if previous is not None and previous['player_id'] != current['player_id']:
        raise ValueError('decision history belongs to a different player')
    packet = {
        'version': 1, 'observed_turn': current['turn'],
        'source': 'audited_player_projection',
        'outstanding': {
            'research_choice': not bool(current['you'].get('researching')),
            'idle_city_ids': sorted(cid for cid, row in current['cities'].items()
                                    if not row.get('production_queue')),
        },
        'changes_since_strategy_request': None,
    }
    if previous is None:
        return packet
    changes: dict[str, Any] = {'from_turn': previous['turn'], 'to_turn': current['turn']}
    for kind in ('units', 'cities'):
        before, after = previous[kind], current[kind]
        changed = [{'id': key, 'fields': _changed_fields(before[key], after[key])}
                   for key in sorted(before.keys() & after.keys()) if before[key] != after[key]]
        changes[kind] = {'newly_owned': _bounded(sorted(after.keys() - before.keys())),
                         'no_longer_owned': _bounded(sorted(before.keys() - after.keys())),
                         'changed': _bounded(changed)}
    before_contacts, after_contacts = set(previous['contacts']), set(current['contacts'])
    changes['contacts'] = {
        'newly_observed': _bounded(sorted(after_contacts - before_contacts)),
        'no_longer_observed': _bounded(sorted(before_contacts - after_contacts)),
    }
    changes['you_changed_fields'] = _changed_fields(previous['you'], current['you'])
    changes['new_known_tile_count'] = len(
        set(current['known_tiles']) - set(previous['known_tiles']))
    packet['changes_since_strategy_request'] = changes
    return packet
