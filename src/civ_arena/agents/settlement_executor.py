"""One observed, player-bound settlement action per turn through the existing facade.

A submitted command is not progress. Ambiguous/rejected actions block automatic
repetition; this fresh-match controller has no retry, save restore or native RPC.
"""
from __future__ import annotations

import copy
import hashlib
import json
from collections.abc import Awaitable, Callable

from civ_arena.agents.growth_policy import GrowthPolicy, _distance, _healthy, _number, _owner


class SettlementExecutor:
    def __init__(self, player_id: int):
        self.player_id = player_id
        self.pending: dict | None = None
        self._attempted_turn = 0
        self._reported: set[str] = set()
        self.last_observation: dict | None = None

    def summary(self):
        return {'pending': {key: self.pending.get(key) for key in
                    ('mission_id', 'unit_id', 'action', 'turn', 'dest', 'status')}
                if self.pending else None,
                'last_observed_outcome': (self.last_observation or {}).get('outcome'),
                'automatic_retries': 0}

    def observe(self, policy: GrowthPolicy, state: dict, turn: int) -> dict | None:
        if policy.player_id != self.player_id:
            raise ValueError('settlement executor player mismatch')
        pending = self.pending
        if pending is None:
            return None
        own = [u for u in state['get_units'] if _owner(u) == self.player_id]
        unit = next((u for u in own if u['unit_id'] == pending['unit_id']), None)
        outcome = 'submitted_progress_unconfirmed'
        if pending.get('superseded'):
            outcome = 'intervening_actor_action_correlation_unavailable'
        elif pending['status'] == 'rejected':
            outcome = 'rejected_automatic_repeat_blocked'
        elif pending['action'] == 'move_unit':
            if unit and unit['coord'] == pending['dest']:
                outcome = 'requested_destination_observed'
                policy.confirm_movement_observation(pending['mission_id'], turn)
            elif unit is None:
                outcome = 'actor_no_longer_observed_owned'
            elif unit['coord'] != pending['origin']:
                outcome = 'other_position_observed_request_unconfirmed'
        else:
            cities = [c for c in state['get_cities'] if _owner(c) == self.player_id
                      and c['coord'] == pending['site']
                      and c['city_id'] not in pending['previous_city_ids']]
            mission = policy.mission
            if (cities and unit is None and mission
                    and mission['mission_id'] == pending['mission_id']
                    and mission['status'] == 'city_observed_at_site'):
                policy.confirm_founding_observation(pending['mission_id'], turn)
                outcome = 'accepted_founder_consumed_new_owned_city_observed'
        record = {**copy.deepcopy(pending), 'observed_turn': turn, 'outcome': outcome}
        self.last_observation = record
        if outcome in {'requested_destination_observed',
                       'accepted_founder_consumed_new_owned_city_observed'}:
            self.pending = None
        return record

    def review_reasons(self, turn: int, mission: dict | None = None) -> list[str]:
        if mission and mission.get('production_wait_review_due'):
            key = 'production:' + mission['mission_id']
            if key not in self._reported:
                self._reported.add(key)
                return ['settlement_production_wait_review']
        if self.pending is None:
            return []
        p = self.pending
        if p['status'] != 'rejected' and turn - p['turn'] < 2:
            return []
        # At most one additional strategic trigger per unresolved mission.
        if p['mission_id'] in self._reported:
            return []
        self._reported.add(p['mission_id'])
        return ['settlement_progress_unconfirmed_review']

    async def run(self, policy: GrowthPolicy, state: dict, *, turn: int, match_id: str,
                  execute: Callable[[str, dict], Awaitable[dict]],
                  refresh: Callable[[], Awaitable[dict]],
                  reserved_roles: dict, recovery: dict,
                  tactical_ids: set[str], attempted_ids: set[str],
                  untouched_frozen_ids: set[str]) -> dict:
        policy.refresh(state)
        if self.pending and self.pending['unit_id'] in attempted_ids:
            self.pending['superseded'] = True
        observation = self.observe(policy, state, turn)
        mission = policy.mission
        report = {'version': 1, 'source': 'automatic_settlement_mission', 'turn': turn,
                  'mission': mission, 'observation': observation, 'execution': [],
                  'limits': {'actions_per_seat_turn': 1, 'automatic_retries': 0},
                  'outcome': 'no_observed_feasible_mission'}
        if self.pending is not None:
            return {**report, 'outcome': 'pending_action_automatic_repeat_blocked',
                    'pending': copy.deepcopy(self.pending)}
        if not mission or mission['status'] not in {'proposal_ready', 'awaiting_observed_movement'}:
            return report
        if turn <= self._attempted_turn:
            return {**report, 'outcome': 'seat_turn_mission_action_already_attempted'}
        own = {u['unit_id']: u for u in state['get_units'] if _owner(u) == self.player_id}
        uid, eid = mission['unit_id'], mission['escort_id']
        if not uid or not eid or uid not in own or eid not in own:
            return {**report, 'outcome': 'mission_actor_unavailable'}
        actors = {uid, eid}
        # Automatic actions cannot inherit the model's one-turn recovery override.
        guards = {key for key, role in reserved_roles.items() if role == 'guard'}
        if any(key in recovery or key not in own or not _healthy(own[key])
               for key in actors | guards):
            return {**report, 'outcome': 'health_hold_priority'}
        if actors & tactical_ids:
            return {**report, 'outcome': 'explicit_model_tactics_priority'}
        if actors & attempted_ids:
            return {**report, 'outcome': 'mission_actor_already_attempted'}
        route = mission.get('route', [])
        action = 'move_unit' if route else 'found_city'
        actor = own[uid]
        args = {'unit_id': uid}
        if route:
            dest = route[0]
            # Advance the escort first when moving the settler would break the
            # one-hex observed support radius. Only one actor moves this turn.
            if _distance(own[eid]['coord'], dest) > 1:
                foreign = [u for u in state['get_units'] if _owner(u) != self.player_id]
                tiles = state['get_visible_map']['tiles']
                blocked = {key for key in tiles if any(_distance(key, u['coord']) <= 2
                                                       for u in foreign)}
                escort_route = policy._route(own[eid]['coord'], mission['site'], tiles, blocked)
                if (not escort_route or _distance(escort_route[0], own[uid]['coord']) > 1
                        or any(u['coord'] == escort_route[0] and u['unit_id'] not in actors
                               for u in state['get_units'])):
                    return {**report, 'outcome': 'no_observed_safe_escort_step'}
                actor, dest = own[eid], escort_route[0]
                args = {'unit_id': eid}
            if _distance(actor['coord'], dest) != 1:
                raise ValueError('settlement route must be a single adjacent step')
            if any(u['coord'] == dest and u['unit_id'] not in actors
                   for u in state['get_units']):
                return {**report, 'outcome': 'observed_occupied_mission_step'}
            args['dest'] = dest
        elif actor['coord'] != mission['site'] or _distance(actor['coord'], own[eid]['coord']) > 1:
            return {**report, 'outcome': 'founding_site_or_escort_not_observed'}
        actor_id = actor['unit_id']
        frozen = actor_id in untouched_frozen_ids
        if (_number(actor.get('movement')) or 0) <= 0 and not frozen:
            return {**report, 'outcome': 'observed_movement_spent'}
        identity = [match_id, self.player_id, turn, mission['mission_id'], action, args]
        args['idempotency_key'] = 'settle-' + hashlib.sha256(
            json.dumps(identity, sort_keys=True, separators=(',', ':')).encode()).hexdigest()[:40]
        self._attempted_turn = turn
        policy.record_mission_attempt(mission['mission_id'], turn)
        result = await execute(action, args)
        untouched_frozen_ids.discard(actor_id)
        if not isinstance(result, dict) or result.get('status') not in {'accepted', 'rejected'}:
            raise ValueError('settlement action returned no canonical facade status')
        self.pending = {'mission_id': mission['mission_id'], 'unit_id': actor_id,
                        'action': action, 'turn': turn, 'origin': actor['coord'],
                        'dest': args.get('dest'), 'site': mission['site'],
                        'status': result['status'], 'idempotency_key': args['idempotency_key'],
                        'previous_city_ids': sorted(c['city_id'] for c in state['get_cities']
                                                   if _owner(c) == self.player_id)}
        current = await refresh()
        policy.refresh(current)
        observed = self.observe(policy, current, turn)
        report.update(outcome=observed['outcome'], observation=observed, mission=policy.mission,
                      execution=[{'action': action, 'args': args, 'result': result,
                                  'movement_authority': 'untouched_opening_frozen_allowance'
                                  if frozen else 'observed_positive_movement'}])
        return report
