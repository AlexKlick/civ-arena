"""Opt-in, projected-state growth policy and persistent settlement proposals.

This foundation never executes a game action. Strength sums and city spacing are
bounded planning heuristics, not combat odds, engine legality or yield forecasts.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
from collections import deque
from dataclasses import asdict, dataclass

from civ_arena.agents.strategy_directive import coordinate
from civ_arena.game.sim.state import hex_dist, neighbors

_LAND = frozenset({'PLAINS', 'GRASSLAND', 'DESERT', 'HILL', 'FOREST', 'JUNGLE', 'MARSH'})
_CAP_KEYS = {'combat', 'ranged', 'domain', 'found_city', 'build_charges'}
_TERMINAL_MISSIONS = frozenset({'city_observed_at_site', 'expired',
                                'settler_no_longer_observed_owned'})


def _distance(a, b):
    return hex_dist(coordinate(a), coordinate(b))


def _owner(row):
    return row.get('owner_id', row.get('owner'))


def _number(value):
    return value if type(value) in (int, float) and math.isfinite(value) and value >= 0 else None


def _power(unit):
    values = [_number(unit.get(key)) for key in ('strength', 'ranged_strength')]
    return max(values) if all(value is not None for value in values) else None


def _healthy(unit):
    hp, maximum = _number(unit.get('hp')), _number(unit.get('max_hp'))
    return (unit.get('health_valid') is True and hp is not None and maximum is not None
            and maximum > 0 and hp <= maximum and hp / maximum >= .9)


def capability(row):
    """Only an explicit closed native observation establishes a production role."""
    cap = row.get('unit_capabilities')
    if cap is None:
        return None
    if not isinstance(cap, dict) or set(cap) != _CAP_KEYS:
        raise ValueError('unit capabilities require the closed native field set')
    for key in ('combat', 'ranged', 'build_charges'):
        if cap[key] is not None and (type(cap[key]) is not int or not 0 <= cap[key] <= 10000):
            raise ValueError('invalid native unit capability number')
    if cap['domain'] not in (None, 'DOMAIN_LAND', 'DOMAIN_SEA', 'DOMAIN_AIR'):
        raise ValueError('invalid native domain')
    if cap['found_city'] is not None and type(cap['found_city']) is not bool:
        raise ValueError('invalid native founding capability')
    return copy.deepcopy(cap)


def _role(cap):
    if cap is None:
        return 'unknown'
    if cap['domain'] != 'DOMAIN_LAND':
        return 'unsupported_domain' if cap['domain'] else 'unknown'
    if cap['found_city'] is True:
        return 'settler'
    if (cap['combat'] is not None and cap['ranged'] is not None
            and max(cap['combat'], cap['ranged']) > 0):
        return 'military'
    if cap['build_charges'] is not None and cap['build_charges'] > 0:
        return 'builder'
    if cap['combat'] is not None and cap['ranged'] is not None:
        return 'other_civilian'
    return 'unknown'


def _armed(cap):
    return cap is not None and any(cap[key] is not None and cap[key] > 0
                                   for key in ('combat', 'ranged'))


def _unknown_arms(cap):
    return cap is None or any(cap[key] is None for key in ('combat', 'ranged'))


@dataclass(frozen=True)
class GrowthControls:
    quiet_turns: int = 3
    threat_radius: int = 5
    military_cap: int = 8
    guards_per_city: int = 1
    max_cities: int = 4
    mission_ttl: int = 12
    production_wait_review_turns: int = 60
    min_city_spacing: int = 4
    max_route: int = 12
    unstarted_replans: int = 3

    def __post_init__(self):
        bounds = {'quiet_turns': (1, 10), 'threat_radius': (1, 10), 'military_cap': (1, 32),
                  'guards_per_city': (0, 3), 'max_cities': (1, 32), 'mission_ttl': (1, 30),
                  'min_city_spacing': (3, 8), 'max_route': (1, 24),
                  'production_wait_review_turns': (1, 120)}
        bounds['unstarted_replans'] = (0, 8)
        for key, (low, high) in bounds.items():
            value = getattr(self, key)
            if type(value) is not int or not low <= value <= high:
                raise ValueError(f'invalid bounded growth control {key}')


class GrowthPolicy:
    """One fresh-match, player-bound policy; explicit begin/complete turn lifecycle.

    Reservations/queues affect production only, never healthy field defense or
    escort feasibility. A mission survives tactical/recovery interruptions; its
    proposal is revalidated from each current observation and is never dispatched.
    """

    def __init__(self, player_id: int, *, controls: GrowthControls | None = None,
                 mission_execution: bool = False):
        if type(player_id) is not int or player_id < 0:
            raise ValueError('growth policy requires exact player identity')
        if type(mission_execution) is not bool:
            raise ValueError('mission_execution must be boolean')
        self.mission_execution = mission_execution
        self.player_id = player_id
        self.controls = controls or GrowthControls()
        self._completed = 0
        self._turn = None
        self._mode = 'grow'
        self._quiet = 0
        self._capabilities = {}
        self._mission = None
        self._assessment = None
        self._completed_missions = []
        self._replan_observations = set()
        self._founder_reservations = {}
        self._preparation = None
        self._founder_training = None

    @property
    def assessment(self):
        return copy.deepcopy(self._assessment)

    @property
    def mission(self):
        return copy.deepcopy(self._mission)

    def _state(self, state):
        units, cities = state.get('get_units'), state.get('get_cities')
        tiles = state.get('get_visible_map', {}).get('tiles')
        if (not isinstance(units, list) or len(units) > 512 or not isinstance(cities, list)
                or len(cities) > 128 or not isinstance(tiles, dict) or len(tiles) > 20000):
            raise ValueError('growth requires bounded projected entities and terrain')
        for rows, key in ((units, 'unit_id'), (cities, 'city_id')):
            ids = []
            for row in rows:
                if (not isinstance(row, dict) or not isinstance(row.get(key), str)
                        or type(_owner(row)) is not int):
                    raise ValueError('invalid growth entity identity')
                coordinate(row.get('coord'))
                ids.append(row[key])
            if len(set(ids)) != len(ids):
                raise ValueError('duplicate growth entity identity')
        for key, row in tiles.items():
            coordinate(key)
            if not isinstance(row, dict):
                raise ValueError('invalid growth tile')
        own = [unit for unit in units if _owner(unit) == self.player_id]
        mine = [city for city in cities if _owner(city) == self.player_id]
        if len(mine) > 32:
            raise ValueError('growth exceeds owned city bound')
        return units, cities, own, mine, tiles

    def _risk(self, state):
        units, _, own, mine, _ = self._state(state)
        local = [unit for unit in units if _owner(unit) != self.player_id
                 and any(_distance(unit['coord'], c['coord']) <= self.controls.threat_radius
                         for c in mine)]
        threats = [unit for unit in local if unit.get('is_barbarian') is True]
        defenders = [unit for unit in own if (_power(unit) or 0) > 0
                     and _role(self._capabilities.get(unit.get('type'))) == 'military'
                     and any(_distance(unit['coord'], c['coord']) <= self.controls.threat_radius
                             for c in mine)]
        healthy = [unit for unit in defenders if _healthy(unit)]
        known_threat = sum(_power(unit) or 0 for unit in threats)
        goal = min(self.controls.military_cap,
                   max(self.controls.guards_per_city * len(mine),
                       len(threats) + 1 if threats else 0))
        return {'confirmed_local_barbarian_ids': sorted(u['unit_id'] for u in threats),
                'unclassified_or_nonbarbarian_contact_ids': sorted(u['unit_id'] for u in local
                                                                  if u not in threats),
                'known_threat_strength_sum': known_threat,
                'unknown_threat_strength_ids': sorted(u['unit_id'] for u in threats
                                                     if _power(u) is None),
                'healthy_local_defender_ids': sorted(u['unit_id'] for u in healthy),
                'healthy_local_defense_strength_sum': sum(_power(u) for u in healthy),
                'health_unverified_or_recovering_ids': sorted(u['unit_id'] for u in defenders
                                                             if u not in healthy),
                'minimum_military_count': goal,
                'planning_strength_target': max(20 * len(mine), math.ceil(1.25 * known_threat)),
                'uncertainties': ['war_and_opponent_intent_unobserved',
                                  'strength_sum_is_not_combat_odds', 'fog_threats_unobserved']}

    def _learn_catalogs(self, state, catalogs):
        if catalogs is not None:
            if not isinstance(catalogs, dict) or len(catalogs) > 32:
                raise ValueError('growth catalogs exceed city bound')
            own_cities = {c['city_id'] for c in state['get_cities']
                          if _owner(c) == self.player_id}
            observed = {}
            for cid, rows in sorted(catalogs.items()):
                if cid not in own_cities or not isinstance(rows, list) or len(rows) > 256:
                    raise ValueError('growth catalog must belong to observed owned city')
                for row in rows:
                    if row.get('kind') != 'unit':
                        continue
                    cap = capability(row)
                    if cap is not None:
                        item = row.get('item_id')
                        if not isinstance(item, str) or not item:
                            raise ValueError('growth capability missing exact item ID')
                        if item in observed and observed[item] != cap:
                            raise ValueError('conflicting current native unit capabilities')
                        observed[item] = cap
            if len(set(self._capabilities) | set(observed)) > 1024:
                raise ValueError('growth capability memory exceeds bound')
            self._capabilities.update(observed)

    def refresh(self, state, *, catalogs=None):
        """Revalidate within the active turn without advancing the quiet clock."""
        if self._turn is None:
            raise ValueError('growth refresh requires active turn')
        self._learn_catalogs(state, catalogs)
        risk = self._risk(state)
        if risk['confirmed_local_barbarian_ids']:
            self._mode, self._quiet = 'defend', 0
        self._plan_settlement(state)
        self._assessment = {**risk, 'turn': self._turn, 'mode': self._mode,
                            'quiet_observed_turns': self._quiet,
                            'quiet_turns_required': self.controls.quiet_turns,
                            'settlement_mission': self.mission}
        return self.assessment

    def reserved_roles(self, state):
        """Retain nearby land guards even while recovering; no extra action authority."""
        _, _, own, mine, _ = self._state(state)
        roles = {}
        mission = self._mission
        if mission and mission['status'] not in _TERMINAL_MISSIONS:
            for key, role in (('unit_id', 'settler'), ('escort_id', 'escort')):
                if (role == 'escort' and self.mission_execution
                        and self._unstarted(mission) and not self._founder_queued(state)):
                    continue
                if any(u['unit_id'] == mission.get(key) for u in own):
                    roles[mission[key]] = role
        elif self._preparation and self._preparation['status'] == 'awaiting_site_survey':
            escort = self._preparation['escort_id']
            if any(u['unit_id'] == escort for u in own):
                roles[escort] = 'escort'
            roles.update({u['unit_id']: 'settler' for u in own
                          if _role(self._capabilities.get(u.get('type'))) == 'settler'})
        for city in sorted(mine, key=lambda c: c['city_id']):
            local = sorted((u for u in own if u['unit_id'] not in roles
                            and _role(self._capabilities.get(u.get('type'))) == 'military'
                            and _distance(u['coord'], city['coord']) <= 2),
                           key=lambda u: (not _healthy(u), -(_power(u) or 0), u['unit_id']))
            for unit in local[:self.controls.guards_per_city]:
                roles[unit['unit_id']] = 'guard'
        return roles

    def summary(self):
        """Compact observed capabilities, not a claim about unqueried catalogs."""
        assessment = self.assessment or {}
        mission = self.mission
        columns = ['item_id', 'domain', 'combat', 'ranged', 'found_city', 'build_charges']
        rows = [[item, *(cap[key] for key in columns[1:])]
                for item, cap in sorted(self._capabilities.items())[:16]]
        return {'enabled': True, 'mode': self._mode, 'controls': asdict(self.controls),
                'known_threat_strength': assessment.get('known_threat_strength_sum'),
                'healthy_local_defense_strength': assessment.get(
                    'healthy_local_defense_strength_sum'),
                'confirmed_local_barbarians': len(assessment.get(
                    'confirmed_local_barbarian_ids', [])),
                'quiet_observed_turns': self._quiet,
                'mission': {key: mission.get(key) for key in
                    ('mission_id', 'site', 'status', 'unit_id', 'escort_id', 'created_turn',
                     'travel_started_turn', 'last_confirmed_progress_turn', 'expiry_basis',
                     'observed_settler_queued', 'production_wait_review_due',
                     'replan_outcome', 'replans', 'site_feasibility_issues')}
                    if mission else None,
                'capability_columns': columns, 'observed_capabilities': rows,
                'capabilities_omitted': max(0, len(self._capabilities) - len(rows)),
                'preparatory_training': copy.deepcopy(self._preparation),
                'founder_training': copy.deepcopy(self._founder_training),
                'meaning': 'Observed catalog memory, not current availability. '
                    'Waiting reasons are feasibility holds, not optimality. '
                    'Unknown contacts do not establish war; strength sums are not odds.'}

    def confirm_movement_observation(self, mission_id, turn):
        if self._turn != turn or not self._mission or self._mission['mission_id'] != mission_id:
            raise ValueError('movement confirmation requires active matching mission')
        if self._mission['status'] not in _TERMINAL_MISSIONS:
            self._mission['last_confirmed_progress_turn'] = turn

    def confirm_founding_observation(self, mission_id, turn):
        if (self._turn != turn or not self._mission
                or self._mission['mission_id'] != mission_id
                or self._mission['status'] != 'city_observed_at_site'):
            raise ValueError('founding confirmation requires matching observed city mission')
        self._mission['completion_basis'] = (
            'accepted_found_city_then_new_owned_city_and_consumed_settler')
        self._mission['completed_turn'] = turn

    def begin_turn(self, state, *, turn: int, catalogs: dict[str, list] | None = None):
        if type(turn) is not int or turn != self._completed + 1 or self._turn is not None:
            raise ValueError('growth policy requires consecutive completed own turns')
        self._state(state)
        self._replan_observations.clear()
        self._founder_reservations.clear()
        self._learn_catalogs(state, catalogs)
        risk = self._risk(state)
        if risk['confirmed_local_barbarian_ids']:
            self._mode, self._quiet = 'defend', 0
        elif self._mode == 'defend':
            self._quiet += 1
            if self._quiet >= self.controls.quiet_turns:
                self._mode = 'grow'
        self._turn = turn
        self._assessment = {**risk, 'turn': turn, 'mode': self._mode,
                            'quiet_observed_turns': self._quiet,
                            'quiet_turns_required': self.controls.quiet_turns}
        self._plan_settlement(state)
        self._assessment['settlement_mission'] = self.mission
        return self.assessment

    def complete_turn(self, turn):
        if type(turn) is not int or self._turn != turn:
            raise ValueError('growth completion requires active own turn')
        self._completed, self._turn = turn, None
        if (self.mission_execution and self._mission
                and self._mission.get('completed_turn') == turn):
            if (self._preparation and
                    self._preparation.get('mission_id') == self._mission['mission_id']):
                self._preparation['status'] = 'completed'
            if (self._founder_training and
                    self._founder_training.get('mission_id') == self._mission['mission_id']):
                self._founder_training.update(status='completed', review_due=False,
                                              completed_turn=turn)
            self._completed_missions = (self._completed_missions + [self.mission])[-8:]
            self._mission = None

    def _route(self, origin, site, tiles, blocked):
        todo, previous, depths = deque([origin]), {origin: None}, {origin: 0}
        while todo and len(previous) <= 2048:
            at = todo.popleft()
            if at == site:
                route = []
                while previous[at] is not None:
                    route.append(at)
                    at = previous[at]
                return list(reversed(route)) if len(route) <= self.controls.max_route else None
            if depths[at] >= self.controls.max_route:
                continue
            for q, r in sorted(neighbors(*coordinate(at))):
                key = f'{q},{r}'
                tile = tiles.get(key, {})
                if (key in previous or key in blocked or tile.get('terrain') not in _LAND
                        or type(tile.get('owner_id')) is not int
                        or tile['owner_id'] not in (-1, self.player_id)):
                    continue
                if len(previous) >= 2048:
                    continue
                previous[key] = at
                depths[key] = depths[at] + 1
                todo.append(key)
        return None

    @staticmethod
    def _unstarted(mission):
        return (mission is None or mission['status'] not in _TERMINAL_MISSIONS
                and mission.get('unit_id') is None and 'travel_started_turn' not in mission
                and 'last_confirmed_progress_turn' not in mission
                and 'training_accepted_turn' not in mission
                and not mission.get('execution_attempted'))

    def _training_pending(self):
        return bool(self._founder_training and self._founder_training['status'] != 'completed')

    def training_review_reasons(self, turn):
        """Schedule at most one model review per unresolved accepted training receipt."""
        if self._turn != turn:
            raise ValueError('training review requires active matching own turn')
        if (not self._training_pending() or not self._founder_training.get('review_due')
                or 'review_reported_turn' in self._founder_training):
            return []
        self._founder_training['review_reported_turn'] = turn
        return ['founder_training_outcome_unavailable']

    def _observe_founder_training(self, state):
        """Observe progress; a missing queue never cancels accepted production authority."""
        if not self._training_pending():
            return
        receipt = self._founder_training
        _, _, own, mine, _ = self._state(state)
        producer = next((c for c in mine if c['city_id'] == receipt['city_id']), None)
        queue = producer.get('production_queue') or [] if producer else []
        queue = [queue] if isinstance(queue, str) else queue
        queued = receipt['item_id'] in queue
        founders = sorted(u['unit_id'] for u in own
                          if _role(self._capabilities.get(u.get('type'))) == 'settler'
                          and u['unit_id'] not in receipt['owned_founder_ids_before'])
        if queued:
            receipt['queue_observed_turn'] = self._turn
        if not receipt.get('observed_founder_id') and founders:
            receipt.update(observed_founder_id=founders[0], founder_observed_turn=self._turn)
        founder_present = receipt.get('observed_founder_id') in founders
        missing = not queued and not founder_present and (
            self._turn > receipt['accepted_turn'] or 'queue_observed_turn' in receipt)
        receipt.update(status='founder_observed' if founder_present else
                       'queued_observed' if queued else
                       'outcome_unavailable' if missing else 'accepted_unresolved',
                       review_due=missing, observed_queue_matches=queued,
                       observed_founder_present=founder_present,
                       observed_producer_owned=producer is not None)

    def _founder_queued(self, state, reservations=None):
        reservations = {**self._founder_reservations, **(reservations or {})}
        for city in state['get_cities']:
            if _owner(city) != self.player_id:
                continue
            queue = city.get('production_queue') or []
            queue = [queue] if isinstance(queue, str) else queue
            promised = [*queue, (reservations or {}).get(city['city_id'])]
            if any(_role(self._capabilities.get(item)) == 'settler' for item in promised):
                return True
        return False

    def reserve_founder_production(self, state, city_id, item_id, *, preparatory=False):
        if self._turn is None or city_id not in {c['city_id'] for c in self._state(state)[3]}:
            raise ValueError('founder reservation requires active owned city')
        if _role(self._capabilities.get(item_id)) == 'settler':
            if self.mission_execution and self._training_pending():
                receipt = self._founder_training
                if (receipt['accepted_turn'] == self._turn and receipt['city_id'] == city_id
                        and receipt['item_id'] == item_id):
                    self._founder_reservations[city_id] = item_id
                    return True  # The same accepted same-turn reservation is idempotent.
                raise ValueError('accepted founder training remains unresolved; no replacement')
            if preparatory:
                escort = self._preparatory_escort(state, city_id)
                if not self._unstarted(self._mission) or escort is None:
                    raise ValueError('preparatory reservation lost unsent guarded growth readiness')
                self._preparation = {'status': 'awaiting_site_survey', 'city_id': city_id,
                    'escort_id': escort, 'training_accepted_turn': self._turn,
                    'retired_unsent_intent': {key: self._mission.get(key)
                        for key in ('mission_id', 'site', 'status')} if self._mission else None,
                    'authority': 'production_only_no_selected_travel_site'}
                self._mission = None
            elif self._mission:
                self._mission['training_accepted_turn'] = self._turn
                self._mission['training_city_id'] = city_id
            if self.mission_execution:
                self._founder_training = {'status': 'accepted_unresolved',
                    'city_id': city_id, 'item_id': item_id, 'accepted_turn': self._turn,
                    'mission_id': self._mission['mission_id'] if self._mission else None,
                    'owned_founder_ids_before': sorted(u['unit_id'] for u in self._state(state)[2]
                        if _role(self._capabilities.get(u.get('type'))) == 'settler'),
                    'review_due': False,
                    'authority': 'accepted_training_until_confirmed_settlement_closure'}
                self._observe_founder_training(state)
            self._founder_reservations[city_id] = item_id
            if self._assessment is not None:
                self._assessment['settlement_mission'] = self.mission
            return True
        return False

    def record_mission_attempt(self, mission_id, turn):
        if self._turn != turn or not self._mission or self._mission['mission_id'] != mission_id:
            raise ValueError('mission attempt requires active matching identity')
        self._mission['execution_attempted'] = True

    def _preparatory_escort(self, state, city_id):
        """Training readiness only; this grants no route or founding authority."""
        _, _, own, mine, _ = self._state(state)
        military = sorted((u for u in own if _healthy(u) and (_power(u) or 0) > 0
                           and _role(self._capabilities.get(u.get('type'))) == 'military'),
                          key=lambda u: (-(_power(u) or 0), u['unit_id']))
        guards = set()
        for city in sorted(mine, key=lambda c: c['city_id']):
            local = [u for u in military if u['unit_id'] not in guards
                     and _distance(u['coord'], city['coord']) <= 2]
            if len(local) < self.controls.guards_per_city:
                return None
            guards.update(u['unit_id'] for u in local[:self.controls.guards_per_city])
        origin = next(c['coord'] for c in mine if c['city_id'] == city_id)
        return next((u['unit_id'] for u in military if u['unit_id'] not in guards
                     and _distance(u['coord'], origin) <= 1), None)

    def _plan_settlement(self, state):
        self._observe_founder_training(state)
        units, cities, own, mine, tiles = self._state(state)
        mission = self._mission
        if mission and mission['status'] in _TERMINAL_MISSIONS:
            mission['proposal'] = None
            return
        if mission and any(c['coord'] == mission['site'] and _owner(c) == self.player_id
                           for c in cities):
            mission.update(status='city_observed_at_site', proposal=None,
                           completion_basis='observed_owned_city_not_causal_receipt')
            return
        if mission:
            if self.mission_execution:
                active = mission.get('travel_started_turn')
                basis = mission.get('last_confirmed_progress_turn', active)
                if active is not None:
                    if self._turn - basis >= self.controls.mission_ttl:
                        mission.update(status='expired', proposal=None,
                                       expiry_basis='no_confirmed_travel_progress')
                        return
                else:
                    queued = []
                    for city in mine:
                        queue = city.get('production_queue') or []
                        queued.extend([queue] if isinstance(queue, str) else queue)
                    productive = any(_role(self._capabilities.get(item)) == 'settler'
                                     for item in queued)
                    mission['observed_settler_queued'] = productive
                    mission['production_wait_review_due'] = (not productive and
                        self._turn - mission.get('initial_planning_turn', mission['created_turn'])
                        >= self.controls.production_wait_review_turns)
            elif self._turn - mission['created_turn'] >= self.controls.mission_ttl:
                mission.update(status='expired', proposal=None)
                return
        if mission and mission.get('unit_id') and not any(
                u['unit_id'] == mission['unit_id'] for u in own):
            mission.update(status='settler_no_longer_observed_owned', proposal=None)
            return
        settlers = sorted([u for u in own if _role(self._capabilities.get(u.get('type')))
                           == 'settler'], key=lambda u: u['unit_id'])
        military = sorted([u for u in own if _healthy(u) and (_power(u) or 0) > 0
                           and _role(self._capabilities.get(u.get('type'))) == 'military'],
                          key=lambda u: (-(_power(u) or 0), u['unit_id']))
        if not mine or len(mine) >= self.controls.max_cities:
            if mission:
                mission.update(status='city_limit_or_no_origin', proposal=None)
            return
        if self._mode == 'defend':
            if mission:
                mission.update(status='suspended_local_threat', proposal=None)
            return
        settler = next((u for u in settlers if mission and u['unit_id'] == mission.get('unit_id')),
                       settlers[0] if settlers else None)
        origin = (settler['coord'] if settler else
                  sorted(mine, key=lambda c: c['city_id'])[0]['coord'])
        blocked = {key for key in tiles if any(
            _distance(key, u['coord']) <= 2 for u in units if _owner(u) != self.player_id)}
        foreign = [u for u in units if _owner(u) != self.player_id]
        sites = ([mission['site']] if mission else
                 sorted((site for site in tiles
                         if _distance(origin, site) <= self.controls.max_route),
                        key=lambda site: (_distance(origin, site), site)))
        def candidates_for(choices):
            candidates, route_probes = [], 0
            for site in choices:
                tile = tiles.get(site, {})
                if (tile.get('terrain') not in _LAND or tile.get('owner_id') != -1
                        or tile.get('city_id') or site in blocked
                        or any(_distance(site, c['coord']) < self.controls.min_city_spacing
                               for c in cities)
                        or any(_distance(site, u['coord']) <= 2 for u in foreign)):
                    continue
                if route_probes >= 64:
                    break
                route_probes += 1
                route = self._route(origin, site, tiles, blocked)
                if route is not None:
                    coverage = sum(f'{q},{r}' in tiles for q, r in neighbors(*coordinate(site)))
                    candidates.append((-coverage, len(route), site, route))
            return candidates

        candidates = candidates_for(sites)
        replanning = None
        if (not candidates and mission and self.mission_execution and self._unstarted(mission)
                and not settlers and not self._founder_queued(state)
                and not self._training_pending()):
            tile = tiles.get(mission['site'], {})
            mission['site_feasibility_issues'] = (
                ['site_ownership_unavailable'] if 'owner_id' not in tile else
                ['site_or_route_fails_current_guards'])
            history = mission.get('replans', [])
            observation = hashlib.sha256(json.dumps(
                [origin, tiles, [(u['unit_id'], _owner(u), u['coord']) for u in units],
                 [(c['city_id'], _owner(c), c['coord']) for c in cities]],
                sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()
            if len(history) >= self.controls.unstarted_replans:
                mission['replan_outcome'] = 'unstarted_replan_limit_reached'
            elif (observation not in self._replan_observations
                  and len(self._replan_observations) < 2):
                self._replan_observations.add(observation)
                previous_sites = {r['from_site'] for r in history} | {mission['site']}
                alternatives = sorted((site for site in tiles if site not in previous_sites
                                       and _distance(origin, site) <= self.controls.max_route),
                                      key=lambda site: (_distance(origin, site), site))
                candidates = candidates_for(alternatives)
                mission['replan_outcome'] = 'no_current_feasible_alternative'
                if candidates:
                    selected_site = min(candidates)[2]
                    replanning = {'initial_planning_turn': mission.get(
                        'initial_planning_turn', mission['created_turn']),
                        'replan_outcome': 'unstarted_intent_retargeted',
                        'replans': [*history, {'turn': self._turn, 'from_site': mission['site'],
                            'to_site': selected_site, 'previous_mission_id': mission['mission_id'],
                            'basis': 'current_observed_site_and_route_guards'}]}
                    mission = None
        if not candidates:
            if mission:
                mission.update(status='site_or_route_not_currently_observed_feasible',
                               proposal=None)
            return
        _, _, site, route = min(candidates)
        if mission is None:
            mission = {'mission_id': f'settle:{self.player_id}:{self._turn}:{site}',
                       'site': site, 'created_turn': self._turn, 'unit_id': None,
                       'escort_id': None, 'status': 'planned', 'proposal': None}
            self._mission = mission
            if replanning:
                mission.update(replanning)
            if self._preparation and self._preparation['status'] == 'awaiting_site_survey':
                self._preparation.update(status='site_selected', mission_id=mission['mission_id'])
                mission['training_accepted_turn'] = self._preparation['training_accepted_turn']
                mission['training_city_id'] = self._preparation['city_id']
            if self._training_pending() and self._founder_training['mission_id'] is None:
                self._founder_training['mission_id'] = mission['mission_id']
                mission['training_accepted_turn'] = self._founder_training['accepted_turn']
                mission['training_city_id'] = self._founder_training['city_id']
        mission['unit_id'] = settler['unit_id'] if settler else None
        if self.mission_execution and settler:
            mission.setdefault('travel_started_turn', self._turn)
        guards = set()
        guarded = True
        escort_committed = (self.mission_execution and
                            (not self._unstarted(mission) or self._founder_queued(state)))
        for city in sorted(mine, key=lambda c: c['city_id']):
            local = [u for u in military if u['unit_id'] not in guards
                     and not (escort_committed and u['unit_id'] == mission.get('escort_id'))
                     and _distance(u['coord'], city['coord']) <= 2]
            if len(local) < self.controls.guards_per_city:
                guarded = False
            guards.update(u['unit_id'] for u in local[:self.controls.guards_per_city])
        spare = [u for u in military if u['unit_id'] not in guards] if guarded else []
        escort = next((u for u in spare if _distance(u['coord'], origin) <= 1), None)
        if escort_committed and mission.get('escort_id'):
            # Preserve the assigned mission identity through a health pause. A
            # missing/invalid escort becomes an explicit hold, never a new order.
            escort = next((u for u in own if u['unit_id'] == mission['escort_id']
                           and _role(self._capabilities.get(u.get('type'))) == 'military'), None)
            if escort and (not guarded or _distance(escort['coord'], origin) > 1):
                mission.update(status='awaiting_guard_or_adjacent_escort', proposal=None)
                return
        mission.update(route=route, escort_id=escort['unit_id'] if escort else None,
                       proposal=None, uncertainties=['site_legality_unverified',
                       'yield_resource_freshwater_unobserved',
                       'wrap_and_routes_are_not_engine_paths'])
        if escort is None:
            mission['status'] = 'awaiting_healthy_spare_escort'
        elif not _healthy(escort):
            mission['status'] = 'awaiting_verified_escort_health'
        elif settler is None:
            mission['status'] = 'awaiting_settler'
        elif not _healthy(settler):
            mission['status'] = 'awaiting_verified_settler_health'
        elif (_number(settler.get('movement')) or 0) <= 0:
            mission['status'] = 'awaiting_observed_movement'
        elif not route:
            mission.update(status='proposal_ready', proposal={'action': 'found_city',
                           'args': {'unit_id': settler['unit_id']}})
        elif (_number(settler.get('movement')) or 0) > 0:
            mission.update(status='proposal_ready', proposal={'action': 'move_unit',
                           'args': {'unit_id': settler['unit_id'], 'dest': route[0]}})
        else:
            mission['status'] = 'awaiting_observed_movement'

    def adjust_production(self, base, *, state, player_id, city_id, options, directive,
                          reservations=None):
        if self._turn is None or player_id != self.player_id:
            raise ValueError('growth production requires active matching player turn')
        for reserved_city, item in (reservations or {}).items():
            if reserved_city in {c['city_id'] for c in state['get_cities']
                                 if _owner(c) == player_id}:
                self.reserve_founder_production(state, reserved_city, item)
        reservations = {**self._founder_reservations, **(reservations or {})}
        risk = self._risk(state)
        # Newly observed danger in an economy refresh enters defend immediately.
        if risk['confirmed_local_barbarian_ids']:
            self._mode, self._quiet = 'defend', 0
        mode = self._mode
        self._plan_settlement(state)
        self._assessment = {**risk, 'turn': self._turn, 'mode': mode,
                            'quiet_observed_turns': self._quiet,
                            'quiet_turns_required': self.controls.quiet_turns,
                            'settlement_mission': self.mission}
        catalog = {row['item_id']: row for row in options}
        out = copy.deepcopy(base)
        candidates = out['candidates']
        owned = [u for u in state['get_units'] if _owner(u) == player_id]
        queued_count, land_queued_count, future_power = 0, 0, 0
        uncertain_slots = []
        caps = {**self._capabilities, **{item: capability(row) for item, row in catalog.items()
                                       if row['kind'] == 'unit'}}
        field_count = sum((_power(u) or 0) > 0 or _armed(caps.get(u.get('type')))
                          for u in owned)
        land_field_count = sum(_role(caps.get(u.get('type'))) == 'military' for u in owned)
        civilian = {role: sum(_role(caps.get(u.get('type'))) == role for u in owned)
                    for role in ('settler', 'builder')}
        for unit in owned:
            cap = caps.get(unit.get('type'))
            if _power(unit) is None and not _armed(cap) and _unknown_arms(cap):
                uncertain_slots.append({'unit_id': unit['unit_id']})
        for city in state['get_cities']:
            if _owner(city) != player_id:
                continue
            queue = city['production_queue']
            queue = [queue] if isinstance(queue, str) and queue else queue
            reserved = (reservations or {}).get(city['city_id'])
            promised = list(queue) + ([reserved] if reserved and reserved not in queue else [])
            for item in promised:
                cap = caps.get(item)
                if _role(cap) in civilian:
                    civilian[_role(cap)] += 1
                if _armed(cap):
                    queued_count += 1
                    if _role(cap) == 'military':
                        land_queued_count += 1
                        future_power += max(cap['combat'], cap['ranged'])
                elif _unknown_arms(cap) and catalog.get(item, {}).get('kind') != 'building':
                    uncertain_slots.append({'city_id': city['city_id'], 'item_id': item})
        total_military = field_count + queued_count
        occupied_slots = total_military + len(uncertain_slots)
        land_military = land_field_count + land_queued_count
        shortfall = max(0, risk['minimum_military_count'] - land_military)
        power_gap = max(0, risk['planning_strength_target']
                        - risk['healthy_local_defense_strength_sum'])
        production_power_gap = max(0, power_gap - future_power)
        escort_gap = (self.mission_execution and mode == 'grow' and self._mission is not None
                      and self._mission['status'] == 'awaiting_healthy_spare_escort'
                      and land_military < self.controls.guards_per_city * len(
                          [c for c in state['get_cities'] if _owner(c) == player_id]) + 1)
        needs_defense = (shortfall > 0 or mode == 'defend' and production_power_gap > 0
                         or escort_gap)
        preparatory_escort = self._preparatory_escort(state, city_id) if (
            self.mission_execution and mode == 'grow' and self._unstarted(self._mission)
            and not self._training_pending()
            and civilian['settler'] == 0 and 0 < len(
                [c for c in state['get_cities'] if _owner(c) == player_id])
            < self.controls.max_cities) else None
        rank = {}
        for row in candidates:
            item = row['item_id']
            role = 'building' if row['kind'] == 'building' else _role(capability(catalog[item]))
            eligible, reason = False, 'growth_role_not_observed_or_not_needed'
            if role == 'building':
                eligible, reason = True, 'infrastructure_opportunity'
            elif item == 'SCOUT' and not base['scout_role_assigned']:
                reason = 'scouting_role_disabled'
            elif self.mission_execution and item == 'SCOUT':
                eligible = row['effective'] < directive.get('unit_targets', {}).get('SCOUT', 2)
                reason = 'bounded_scouting_inventory' if eligible else 'scout_target_satisfied'
            elif role == 'military':
                eligible = needs_defense and occupied_slots < self.controls.military_cap
                reason = 'aggregate_defense_gap' if eligible else 'minimum_army_satisfied_or_capped'
            elif role == 'builder':
                eligible = civilian['builder'] < len([c for c in state['get_cities']
                                                   if _owner(c) == player_id])
                reason = 'infrastructure_builder_gap' if eligible else 'builder_target_satisfied'
            elif role == 'settler':
                feasible = (mode == 'grow' and self._mission is not None
                            and self._mission['status'] == 'awaiting_settler'
                            and civilian['settler'] == 0 and not self._training_pending()
                            and 'training_accepted_turn' not in self._mission)
                eligible = feasible or preparatory_escort is not None
                reason = ('feasible_escorted_expansion' if feasible else
                          'bounded_preparatory_founder_reserve' if eligible
                          else 'accepted_founder_training_unresolved' if self._training_pending()
                          else 'expansion_not_ready')
            if (row['kind'] == 'unit' and _armed(capability(catalog[item]))
                    and occupied_slots >= self.controls.military_cap):
                eligible, reason = False, 'empire_military_capacity_satisfied'
            cap = directive.get('unit_targets', {}).get(item) if row['kind'] == 'unit' else None
            if self.mission_execution and item == 'SCOUT' and cap is None:
                cap = 2
            if cap is not None and row['effective'] >= cap:
                eligible, reason = False, 'explicit_unit_target_satisfied'
            row.update(eligible=eligible, reason=reason, growth_role=role, target=cap,
                       target_basis='explicit_per_item_cap_plus_aggregate_growth_policy')
            if eligible:
                priority = (0 if role == 'military' and needs_defense else
                            1 if role == 'settler' else 2)
                efficiency = 0.0
                if role == 'military':
                    observed = capability(catalog[item])
                    turns = _number(catalog[item].get('turns'))
                    if turns is not None and turns > 0:
                        power = max(observed['combat'], observed['ranged'])
                        efficiency = min(power, max(20, production_power_gap)) / turns
                    row['bounded_strength_per_reported_turn'] = efficiency
                preference = directive['production_preferences'].index(item) \
                    if item in directive['production_preferences'] else 999
                rank[item] = (priority, -efficiency, preference, item)
        chosen = min(rank, key=rank.get) if rank else None
        out.update(version=2, item_id=chosen, reason='growth_policy_choice' if chosen else
                   'no_eligible_growth_production', growth={'mode': mode, **risk,
                   'military_owned_queued': total_military,
                   'land_military_owned_queued': land_military,
                   'military_cap': self.controls.military_cap, 'count_shortfall': shortfall,
                   'unclassified_capacity_reservations': uncertain_slots,
                   'military_capacity_slots': occupied_slots, 'civilian_inventory': civilian,
                   'healthy_strength_gap': power_gap, 'queued_reserved_strength': future_power,
                   'production_strength_gap': production_power_gap,
                   'production_defense_needed': needs_defense,
                   'bounded_expansion_escort_gap': bool(escort_gap),
                   'preparatory_founder_reserve': {'limit_owned_queued_reserved': 1,
                       'observed_guarded_spare_escort_id': preparatory_escort,
                       'selected': any(row['item_id'] == chosen and row['reason'] ==
                                       'bounded_preparatory_founder_reserve' for row in candidates),
                       'meaning': 'Training capacity only; no route or founding authority.'},
                   'available_infrastructure': sorted(item for item, row in catalog.items()
                                                       if row['kind'] == 'building'),
                   'production_option_scope': {'supported_kinds': ['unit', 'building'],
                       'unrepresented_native_choices': ['district_placement', 'city_projects'],
                       'native_availability_of_unrepresented_choices': 'unobserved',
                       'no_eligible_choice_requires': None if chosen else
                           'qualify_productive_capability_or_wait_for_observed_safe_growth'},
                   'mission': self.mission, 'execution': 'proposal_only_no_game_actions'})
        return out
