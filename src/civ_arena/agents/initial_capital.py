"""Bounded founding continuation for a model-selected initial settler destination.

An existing tactical move supplies the site; no site, route, or optimality is
invented here. Only the growth opt-in enables this fresh-match prerequisite.
"""
from __future__ import annotations

import copy
import hashlib
import json

from civ_arena.agents.growth_policy import _distance, _healthy, _number, _owner
from civ_arena.agents.scouting import _LAND_COST


class InitialCapitalPlan:
    MAX_PLAN_TURNS = 12

    def __init__(self, player_id: int):
        if type(player_id) is not int or player_id < 0:
            raise ValueError('initial capital requires exact player identity')
        self.player_id = player_id
        self.mission = None
        self.pending = None
        self.completion = None
        self.disabled = False
        self._reviewed = False
        self._settlers = []

    def summary(self):
        return {'mission': copy.deepcopy(self.mission), 'disabled': self.disabled,
                'observed_initial_settler_ids': list(self._settlers),
                'limits': {'automatic_found_attempts': 1,
                           'plan_own_turns': self.MAX_PLAN_TURNS,
                           'additional_choice_or_block_reviews': 1},
                'meaning': 'First explicit cityless SETTLER move selects a fixed capital site. '
                    'Observed arrival enables one later guarded founding attempt, not a retry '
                    'or proof of optimality. No production catalog or spare escort is required.'}

    def observe(self, state, turn):
        own = {u['unit_id']: u for u in state['get_units'] if _owner(u) == self.player_id}
        self._settlers = sorted(uid for uid, u in own.items() if u.get('type') == 'SETTLER')
        cities = [c for c in state['get_cities'] if _owner(c) == self.player_id]
        mission, pending = self.mission, self.pending
        if mission and pending and pending['action'] == 'found_city' and (
                pending['status'] == 'accepted' and not pending.get('superseded')
                and not any(u['unit_id'] == mission['unit_id'] for u in state['get_units'])
                and any(
                    c['coord'] == mission['site'] and c['city_id'] not in pending['previous_cities']
                    for c in cities)):
            mission.update(status='founding_observed', observed_completion_turn=turn,
                           completion_basis='accepted_founder_consumed_new_owned_city_at_site')
            self.pending = None
            self.disabled = True
            return
        if cities:
            self.disabled = True
            if mission and mission.get('status') != 'founding_observed':
                mission['status'] = 'owned_city_observed_without_automatic_founding_receipt'
                if pending:
                    pending['superseded'] = True
            return
        if not mission or self.disabled:
            return
        if mission.get('blocked_reason'):
            return
        if turn - mission['created_turn'] >= self.MAX_PLAN_TURNS:
            mission.update(status='expired', blocked_reason='initial_capital_plan_age_exhausted')
            return
        actor = own.get(mission['unit_id'])
        if pending:
            if pending.get('superseded'):
                mission.update(status='blocked', blocked_reason='intervening_actor_action')
            elif pending['status'] == 'rejected':
                mission.update(status='blocked',
                               blocked_reason='request_rejected_no_automatic_retry')
            elif pending['action'] == 'move_unit' and pending['status'] == 'accepted':
                if actor and actor['coord'] == mission['site']:
                    mission['relocation_observed_turn'] = turn
                    self.pending = None
                elif actor is None:
                    mission.update(status='blocked',
                                   blocked_reason='selected_actor_not_observed_owned')
            if self.pending is not None:
                return
        if actor is None or actor.get('type') != 'SETTLER':
            mission.update(status='blocked', blocked_reason='selected_settler_not_observed_owned')
        else:
            mission['status'] = ('at_selected_site' if actor['coord'] == mission['site']
                                 else 'selected_site_not_observed')

    def review_reasons(self, turn):
        if self.disabled or self._reviewed or turn <= 1:
            return []
        if self.mission is None:
            needed = bool(self._settlers)
        else:
            needed = (bool(self.mission.get('blocked_reason'))
                      or self.mission['status'] in {'site_guard_hold', 'selected_site_not_observed'}
                      or self.mission.get('last_guard_hold_turn', turn) < turn
                      or self.pending is not None and turn - self.mission['created_turn'] >= 2)
        if needed:
            self._reviewed = True
            return ['initial_capital_choice_or_progress_review']
        return []

    def prepare(self, state, directive, turn):
        """Capture the existing model order before its ordinary scouting dispatch."""
        if self.disabled or self.mission is not None or len(self._settlers) != 1:
            return
        uid = self._settlers[0]
        order = next((o for o in directive['tactical_overrides']
                      if o['unit_id'] == uid and o['action'] == 'move'), None)
        actor = next((u for u in state['get_units']
                      if u['unit_id'] == uid and _owner(u) == self.player_id), None)
        if (order is None or actor is None or order['dest'] not in state['get_visible_map']['tiles']
                or _distance(actor['coord'], order['dest']) != 1):
            return
        self.mission = {'mission_id': f'capital:{self.player_id}:{turn}:{uid}',
                        'unit_id': uid, 'site': order['dest'], 'created_turn': turn,
                        'selection_basis': 'explicit_model_initial_settler_move',
                        'status': 'awaiting_model_move_receipt', 'found_attempted': False}
        self.pending = {'action': 'move_unit', 'status': 'awaiting_model_move_receipt'}

    def reserved_roles(self, state):
        if self.disabled or not self.mission:
            return {}
        uid = self.mission['unit_id']
        return {uid: 'settler'} if any(u['unit_id'] == uid and _owner(u) == self.player_id
                                      for u in state['get_units']) else {}

    async def run(self, state, *, turn, match_id, graph, tactical_ids, recovery,
                  untouched_frozen_ids, execute, refresh):
        mission = self.mission
        rows = [r for r in graph.get('execution', [])
                if mission and r['unit_id'] == mission['unit_id']]
        if mission and self.pending:
            if self.pending['status'] == 'awaiting_model_move_receipt':
                receipt = next((r for r in rows if r['action'] == 'move_unit'
                                and r['args'].get('dest') == mission['site']
                                and r['decision'].get('override') is True), None)
                if receipt is None:
                    mission.update(status='blocked', blocked_reason='selected_move_not_dispatched')
                else:
                    self.pending['status'] = receipt['status']
                    mission['status'] = 'relocation_progress_unconfirmed'
            elif rows:
                self.pending['superseded'] = True
        self.observe(state, turn)
        report = {'source': 'automatic_initial_capital', 'version': 1,
                  'mission': copy.deepcopy(mission), 'execution': [],
                  'outcome': 'no_automatic_capital_action'}
        if (self.disabled or mission is None or mission.get('blocked_reason')
                or self.pending or mission['found_attempted'] or turn <= mission['created_turn']):
            return report
        uid = mission['unit_id']
        if uid in tactical_ids or rows:
            return {**report, 'outcome': 'current_actor_action_priority'}
        actor = next((u for u in state['get_units']
                      if u['unit_id'] == uid and _owner(u) == self.player_id), None)
        if actor is None or actor['coord'] != mission['site']:
            return report
        if uid in recovery or not _healthy(actor):
            return {**report, 'outcome': 'health_hold_priority'}
        tile = state['get_visible_map']['tiles'].get(mission['site'], {})
        if (tile.get('terrain') not in _LAND_COST or type(tile.get('owner_id')) is not int
                or tile['owner_id'] not in (-1, self.player_id) or tile.get('city_id')
                or any(_distance(mission['site'], c['coord']) < 4 for c in state['get_cities'])
                or any(_owner(u) != self.player_id and _distance(mission['site'], u['coord']) <= 2
                       for u in state['get_units'])):
            mission['status'] = 'site_guard_hold'
            mission['last_guard_hold_turn'] = turn
            return {**report, 'mission': copy.deepcopy(mission), 'outcome': 'site_guard_hold'}
        frozen = uid in untouched_frozen_ids
        if (_number(actor.get('movement')) or 0) <= 0 and not frozen:
            return {**report, 'outcome': 'observed_movement_spent'}
        identity = [match_id, self.player_id, turn, mission['mission_id'], mission['site']]
        args = {'unit_id': uid, 'idempotency_key': 'capital-' + hashlib.sha256(
            json.dumps(identity, separators=(',', ':')).encode()).hexdigest()[:40]}
        mission['found_attempted'] = True
        result = await execute('found_city', args)
        untouched_frozen_ids.discard(uid)
        if not isinstance(result, dict) or result.get('status') not in {'accepted', 'rejected'}:
            raise ValueError('initial capital returned no canonical facade status')
        self.pending = {'action': 'found_city', 'status': result['status'],
                        'previous_cities': [c['city_id'] for c in state['get_cities']
                                            if _owner(c) == self.player_id]}
        mission['status'] = 'founding_progress_unconfirmed'
        current = await refresh()
        self.observe(current, turn)
        return {**report, 'mission': copy.deepcopy(mission), 'outcome': mission['status'],
                'execution': [{'action': 'found_city', 'args': args, 'result': result,
                               'movement_authority': 'untouched_opening_frozen_allowance'
                               if frozen else 'observed_positive_movement'}]}

    def complete_turn(self, turn):
        if self.mission and self.mission.get('observed_completion_turn') == turn:
            self.mission['completed_turn'] = turn
            self.completion = copy.deepcopy(self.mission)
