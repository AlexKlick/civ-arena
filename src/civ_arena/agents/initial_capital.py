"""Observed, bounded first-capital prerequisite; no native input outside the facade."""
from __future__ import annotations

import copy
import hashlib
import json

from civ_arena.agents.growth_policy import _distance, _healthy, _number, _owner
from civ_arena.agents.scouting import _LAND_COST


class InitialCapitalPlan:
    # Operational heuristics, not optimal strategy or additional match clocks.
    MAX_PLAN_TURNS = 6
    MAX_RELOCATIONS = 2
    MAX_GUARD_HOLDS = 2

    def __init__(self, player_id: int):
        if type(player_id) is not int or player_id < 0:
            raise ValueError('initial capital requires exact player identity')
        self.player_id = player_id
        self.mission = None
        self.pending = None
        self.completion = None
        self.disabled = False
        self.failure_reason = None
        self._settlers = []
        self._started_turn = None
        self._relocations = 0
        self._failed_edges = set()
        self._found_sites = set()
        self._resolution_required = False
        self._guard_holds = 0
        self._last_hold_turn = None
        self._history = []

    def summary(self):
        return {'mission': copy.deepcopy(self.mission), 'disabled': self.disabled,
                'observed_initial_settler_ids': list(self._settlers),
                'resolution_required': self._resolution_required,
                'failure_reason': self.failure_reason,
                'failed_edges': [list(edge) for edge in sorted(self._failed_edges)],
                'relocations_used': self._relocations, 'guard_holds': self._guard_holds,
                'retired_intents': copy.deepcopy(self._history),
                'limits': {'automatic_found_attempts_per_site': 1,
                           'plan_own_turns': self.MAX_PLAN_TURNS,
                           'total_relocations': self.MAX_RELOCATIONS,
                           'guard_hold_turns': self.MAX_GUARD_HOLDS},
                'meaning': 'No owned city: default is one guarded in-place founding. '
                    'Explicit adjacent relocation overrides that default. A move without '
                    'displacement at the next own-turn observation needs a founder-specific '
                    'resolution; unrelated tactics do not resolve it. '
                    'Receipts alone are not progress.'}

    def _actor(self, state):
        uid = self.mission['unit_id'] if self.mission else (
            self._settlers[0] if len(self._settlers) == 1 else None)
        return next((u for u in state['get_units'] if u['unit_id'] == uid
                     and _owner(u) == self.player_id and u.get('type') == 'SETTLER'), None)

    def _retire(self, reason):
        if self.mission:
            self._history.append({'mission': copy.deepcopy(self.mission),
                                  'pending': copy.deepcopy(self.pending), 'reason': reason})
        self.pending = None

    def observe(self, state, turn):
        own = {u['unit_id']: u for u in state['get_units'] if _owner(u) == self.player_id}
        self._settlers = sorted(uid for uid, u in own.items() if u.get('type') == 'SETTLER')
        cities = [c for c in state['get_cities'] if _owner(c) == self.player_id]
        if self.disabled:
            return
        mission, pending = self.mission, self.pending
        if mission and pending and pending['action'] == 'found_city' and (
                pending['status'] == 'accepted' and not pending.get('superseded')
                and mission['unit_id'] not in {u['unit_id'] for u in state['get_units']}
                and any(c['coord'] == mission['site']
                        and c['city_id'] not in pending['previous_cities'] for c in cities)):
            mission.update(status='founding_observed', observed_completion_turn=turn,
                           completion_basis='accepted_founder_consumed_new_owned_city_at_site',
                           observed_city_id=next(c['city_id'] for c in cities
                               if c['coord'] == mission['site']
                               and c['city_id'] not in pending['previous_cities']),
                           observed_player_id=self.player_id, founder_consumed=True)
            self.pending = None
            self.disabled = True
            return
        if cities:
            self.disabled = True
            if mission:
                mission['status'] = 'owned_city_observed_without_correlated_founding_receipt'
                if pending:
                    pending['superseded'] = True
            return
        if self._started_turn is None:
            self._started_turn = turn
        if len(self._settlers) != 1 or mission and mission['unit_id'] not in own:
            self.failure_reason = 'initial_owned_settler_unavailable_or_ambiguous'
            return
        if turn - self._started_turn >= self.MAX_PLAN_TURNS:
            self.failure_reason = 'initial_capital_progress_budget_exhausted'
            return
        if not mission:
            return
        actor = own[mission['unit_id']]
        if pending and pending['status'] != 'awaiting_explicit_receipt':
            if pending['action'] == 'move_unit':
                if pending['status'] == 'accepted' and actor['coord'] == mission['site']:
                    mission['relocation_observed_turn'] = turn
                    mission['status'] = 'at_selected_site'
                    self.pending = None
                elif pending['status'] == 'rejected' or turn > pending['turn']:
                    self._failed_edges.add((pending['origin'], mission['site']))
                    mission['status'] = 'founder_resolution_required'
                    mission['blocked_reason'] = 'relocation_rejected_or_displacement_unconfirmed'
                    self._resolution_required = True
            elif pending['status'] == 'rejected':
                mission['status'] = 'founder_resolution_required'
                mission['blocked_reason'] = 'founding_rejected_no_same_site_retry'
                self._resolution_required = True
            elif turn > pending['turn']:
                self.failure_reason = 'accepted_founding_still_unconfirmed'

    def review_reasons(self, turn):
        if self.disabled or not self._resolution_required:
            return []
        return ['initial_capital_choice_or_progress_review']

    def _guard(self, state, actor, site, recovery):
        if actor is None:
            return 'selected_settler_not_observed_owned'
        if actor['unit_id'] in recovery or not _healthy(actor):
            return 'health_hold_priority'
        tile = state['get_visible_map']['tiles'].get(site, {})
        if (tile.get('terrain') not in _LAND_COST or type(tile.get('owner_id')) is not int
                or tile['owner_id'] not in (-1, self.player_id) or tile.get('city_id')
                or any(_distance(site, c['coord']) < 4 for c in state['get_cities'])
                or any(_owner(u) != self.player_id and _distance(site, u['coord']) <= 2
                       for u in state['get_units'])):
            return 'site_guard_hold'
        return None

    def directive_error(self, state, directive, recovery):
        """Contextual validation supplements the generic strategy schema, before dispatch."""
        if self.disabled or self.failure_reason:
            return None
        actor = self._actor(state)
        if actor is None:
            return 'capital_settler_unavailable'
        order = next((o for o in directive['tactical_overrides']
                      if actor and o['unit_id'] == actor['unit_id']), None)
        if order is None:
            return 'capital_resolution_required' if self._resolution_required else None
        if order['action'] not in {'found_city', 'move', 'hold'}:
            return 'capital_requires_founder_action'
        if order['action'] == 'hold':
            return None if self._guard(state, actor, actor['coord'], recovery) else \
                'capital_hold_requires_observed_guard'
        site = order.get('dest', actor['coord'])
        if self._guard(state, actor, site, recovery):
            return 'capital_action_blocked_by_observed_guard'
        if order['action'] == 'move':
            if (_distance(actor['coord'], site) != 1
                    or (actor['coord'], site) in self._failed_edges
                    or self._relocations >= self.MAX_RELOCATIONS):
                return 'capital_relocation_unavailable_or_previously_failed'
        elif site in self._found_sites:
            return 'capital_founding_site_already_attempted'
        # An accepted founding request remains ambiguous until observation settles it.
        if self.pending and self.pending['action'] == 'found_city' and \
                self.pending['status'] == 'accepted':
            return 'capital_founding_awaits_observation'
        return None

    def prepare(self, state, directive, turn, recovery=None):
        if self.disabled or self.failure_reason:
            return
        recovery = recovery or {}
        error = self.directive_error(state, directive, recovery)
        if error:
            self.failure_reason = error
            return
        actor = self._actor(state)
        order = next((o for o in directive['tactical_overrides']
                      if o['unit_id'] == actor['unit_id']), None)
        if order or self.mission is None:
            self._retire('explicit_founder_revision' if order else 'procedural_default')
            site = order.get('dest', actor['coord']) if order else actor['coord']
            self.mission = {'mission_id': f'capital:{self.player_id}:{turn}:{actor["unit_id"]}',
                            'unit_id': actor['unit_id'], 'site': site,
                            'created_turn': turn, 'initial_turn': self._started_turn,
                            'selection_basis': {
                                'move': 'explicit_model_relocation_then_guarded_founding',
                                'found_city': 'explicit_model_founding',
                                'hold': 'explicit_guard_hold_then_controller_default',
                            }[order['action']] if order else 'controller_default_guarded_in_place',
                            'status': ('guard_hold' if order['action'] == 'hold' else
                                       'awaiting_explicit_receipt')
                                      if order else 'at_selected_site',
                            'found_attempted': False}
            self._resolution_required = False
            if order and order['action'] != 'hold':
                action = 'move_unit' if order['action'] == 'move' else 'found_city'
                self.pending = {'action': action, 'status': 'awaiting_explicit_receipt',
                                'turn': turn, 'origin': actor['coord'],
                                'previous_cities': [c['city_id'] for c in state['get_cities']
                                                    if _owner(c) == self.player_id]}

    def reserved_roles(self, state):
        actor = self._actor(state)
        return {actor['unit_id']: 'settler'} if not self.disabled and actor else {}

    def _hold(self, turn, reason):
        if self._last_hold_turn != turn:
            self._guard_holds += 1
            self._last_hold_turn = turn
        if self._guard_holds > self.MAX_GUARD_HOLDS:
            self.failure_reason = 'initial_capital_guard_hold_budget_exhausted'
        if self.mission:
            self.mission['status'] = reason

    async def run(self, state, *, turn, match_id, graph, tactical_ids, recovery,
                  untouched_frozen_ids, execute, refresh):
        mission = self.mission
        rows = [r for r in graph.get('execution', [])
                if mission and r['unit_id'] == mission['unit_id']]
        if self.pending and self.pending['status'] == 'awaiting_explicit_receipt':
            pending = self.pending
            row = next((r for r in rows if r['action'] == pending['action'] and
                        r['decision'].get('override') is True and
                        (r['action'] != 'move_unit'
                         or r['args'].get('dest') == mission['site'])), None)
            if row is None:
                self.failure_reason = 'selected_founder_action_not_dispatched'
            else:
                pending['status'] = row['status']
                mission['status'] = ('relocation_progress_unconfirmed'
                                     if row['action'] == 'move_unit'
                                     else 'founding_progress_unconfirmed')
                if row['action'] == 'move_unit':
                    self._relocations += 1
                else:
                    self._found_sites.add(mission['site'])
                    mission['found_attempted'] = True
        self.observe(state, turn)
        report = {'source': 'automatic_initial_capital', 'version': 2,
                  'mission': copy.deepcopy(mission), 'execution': [],
                  'outcome': 'no_automatic_capital_action'}
        if self.disabled or self.failure_reason or mission is None or self.pending:
            return report
        actor = self._actor(state)
        guard = self._guard(state, actor, mission['site'], recovery)
        if guard:
            self._hold(turn, guard)
            return {**report, 'mission': copy.deepcopy(mission), 'outcome': guard}
        uid = mission['unit_id']
        if uid in tactical_ids or rows:
            if mission.get('relocation_observed_turn') != turn:
                self._hold(turn, 'current_actor_action_priority')
            return {**report, 'outcome': 'current_actor_action_priority'}
        if actor['coord'] != mission['site']:
            self.failure_reason = 'selected_site_not_observed_after_resolution'
            return report
        frozen = uid in untouched_frozen_ids
        if (_number(actor.get('movement')) or 0) <= 0 and not frozen:
            self._hold(turn, 'observed_movement_spent')
            return {**report, 'outcome': 'observed_movement_spent'}
        if mission['site'] in self._found_sites:
            self.failure_reason = 'capital_founding_site_already_attempted'
            return report
        identity = [match_id, self.player_id, turn, mission['mission_id'], mission['site']]
        args = {'unit_id': uid, 'idempotency_key': 'capital-' + hashlib.sha256(
            json.dumps(identity, separators=(',', ':')).encode()).hexdigest()[:40]}
        mission['found_attempted'] = True
        self._found_sites.add(mission['site'])
        self.pending = {'action': 'found_city', 'status': 'input_outcome_unavailable', 'turn': turn,
                        'previous_cities': [c['city_id'] for c in state['get_cities']
                                            if _owner(c) == self.player_id]}
        result = await execute('found_city', args)
        untouched_frozen_ids.discard(uid)
        if not isinstance(result, dict) or result.get('status') not in {'accepted', 'rejected'}:
            raise ValueError('initial capital returned no canonical facade status')
        self.pending['status'] = result['status']
        mission['status'] = 'founding_progress_unconfirmed'
        current = await refresh()
        self.observe(current, turn)
        return {**report, 'mission': copy.deepcopy(mission), 'outcome': mission['status'],
                'execution': [{'action': 'found_city', 'args': args, 'result': result,
                               'authority': mission['selection_basis'],
                               'movement_authority': 'untouched_opening_frozen_allowance'
                               if frozen else 'observed_positive_movement'}]}

    def complete_turn(self, turn):
        if self.mission and self.mission.get('observed_completion_turn') == turn:
            self.mission['completed_turn'] = turn
            self.completion = copy.deepcopy(self.mission)
