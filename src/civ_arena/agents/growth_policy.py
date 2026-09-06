"""Opt-in, projected-state growth policy and persistent settlement proposals.

This foundation never executes a game action. Strength sums and city spacing are
bounded planning heuristics, not combat odds, engine legality or yield forecasts.
"""
from __future__ import annotations

import copy
import math
from collections import deque
from dataclasses import dataclass

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
    if cap['build_charges'] is not None and cap['build_charges'] > 0:
        return 'builder'
    if cap['combat'] is not None and cap['ranged'] is not None:
        return 'military' if max(cap['combat'], cap['ranged']) > 0 else 'other_civilian'
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
    min_city_spacing: int = 4
    max_route: int = 12

    def __post_init__(self):
        bounds = {'quiet_turns': (1, 10), 'threat_radius': (1, 10), 'military_cap': (1, 32),
                  'guards_per_city': (0, 3), 'max_cities': (1, 32), 'mission_ttl': (1, 30),
                  'min_city_spacing': (3, 8), 'max_route': (1, 24)}
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

    def __init__(self, player_id: int, *, controls: GrowthControls | None = None):
        if type(player_id) is not int or player_id < 0:
            raise ValueError('growth policy requires exact player identity')
        self.player_id = player_id
        self.controls = controls or GrowthControls()
        self._completed = 0
        self._turn = None
        self._mode = 'grow'
        self._quiet = 0
        self._capabilities = {}
        self._mission = None
        self._assessment = None

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

    def begin_turn(self, state, *, turn: int, catalogs: dict[str, list] | None = None):
        if type(turn) is not int or turn != self._completed + 1 or self._turn is not None:
            raise ValueError('growth policy requires consecutive completed own turns')
        self._state(state)
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

    def _plan_settlement(self, state):
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
        if mission and self._turn - mission['created_turn'] >= self.controls.mission_ttl:
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
        candidates = []
        sites = ([mission['site']] if mission else
                 sorted((site for site in tiles
                         if _distance(origin, site) <= self.controls.max_route),
                        key=lambda site: (_distance(origin, site), site)))
        route_probes = 0
        for site in sites:
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
                # Tile coverage is an information score only; no yield is fabricated.
                coverage = sum(f'{q},{r}' in tiles for q, r in neighbors(*coordinate(site)))
                candidates.append((-coverage, len(route), site, route))
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
        mission['unit_id'] = settler['unit_id'] if settler else None
        guards = set()
        guarded = True
        for city in sorted(mine, key=lambda c: c['city_id']):
            local = [u for u in military if u['unit_id'] not in guards
                     and _distance(u['coord'], city['coord']) <= 2]
            if len(local) < self.controls.guards_per_city:
                guarded = False
            guards.update(u['unit_id'] for u in local[:self.controls.guards_per_city])
        spare = [u for u in military if u['unit_id'] not in guards] if guarded else []
        escort = next((u for u in spare if _distance(u['coord'], origin) <= 1), None)
        mission.update(route=route, escort_id=escort['unit_id'] if escort else None,
                       proposal=None, uncertainties=['site_legality_unverified',
                       'yield_resource_freshwater_unobserved',
                       'wrap_and_routes_are_not_engine_paths'])
        if escort is None:
            mission['status'] = 'awaiting_healthy_spare_escort'
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
        needs_defense = (shortfall > 0 or mode == 'defend' and production_power_gap > 0)
        rank = {}
        for row in candidates:
            item = row['item_id']
            role = 'building' if row['kind'] == 'building' else _role(capability(catalog[item]))
            eligible, reason = False, 'growth_role_not_observed_or_not_needed'
            if role == 'building':
                eligible, reason = True, 'infrastructure_opportunity'
            elif item == 'SCOUT' and not base['scout_role_assigned']:
                reason = 'scouting_role_disabled'
            elif role == 'military':
                eligible = needs_defense and occupied_slots < self.controls.military_cap
                reason = 'aggregate_defense_gap' if eligible else 'minimum_army_satisfied_or_capped'
            elif role == 'builder':
                eligible = civilian['builder'] < len([c for c in state['get_cities']
                                                   if _owner(c) == player_id])
                reason = 'infrastructure_builder_gap' if eligible else 'builder_target_satisfied'
            elif role == 'settler':
                eligible = (mode == 'grow' and self._mission is not None
                            and self._mission['status'] == 'awaiting_settler'
                            and civilian['settler'] == 0)
                reason = 'feasible_escorted_expansion' if eligible else 'expansion_not_ready'
            cap = directive.get('unit_targets', {}).get(item) if row['kind'] == 'unit' else None
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
                   'available_infrastructure': sorted(item for item, row in catalog.items()
                                                       if row['kind'] == 'building'),
                   'mission': self.mission, 'execution': 'proposal_only_no_game_actions'})
        return out
