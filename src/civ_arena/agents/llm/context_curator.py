"""Turn-local, player-projected context; gathering never spends a model request."""
from __future__ import annotations

import json
from typing import Any

from civ_arena.agents.llm.terrain_context import compact_entities, compact_terrain
from civ_arena.agents.llm.terrain_evidence import cold_biome_evidence
from civ_arena.arena.referee import MatchAborted
from civ_arena.game.terrain_metadata import terrain_fields

BASIC_READS = frozenset({'get_units', 'get_cities', 'get_overview', 'get_visible_map',
                         'get_available_research', 'get_available_production', 'get_strategy'})
GAME_ACTIONS = frozenset({'move_unit', 'attack', 'fortify', 'found_city', 'purchase',
                          'set_research', 'set_city_production'})
CONTEXT_MARKER = 'Controller context (projected observations, axial coordinates):\n'
DETAIL_SCHEMA = {
    'name': 'inspect_context',
    'description': 'Optional focused detail for a strategic decision: supply exactly one of '
        'city_id (production choices for an owned city with an existing queue), or coord '
        '(nearby already known terrain). Basic current state is supplied automatically.',
    'input_schema': {'type': 'object', 'properties': {
        'city_id': {'type': 'string'}, 'coord': {'type': 'string'}}}}


def encode(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False)


def coord(value: str) -> tuple[int, int]:
    q, r = value.split(',')
    return int(q), int(r)


def distance(a: str, b: str) -> int:
    aq, ar = coord(a)
    bq, br = coord(b)
    return max(abs(aq-bq), abs(ar-br), abs(aq+ar-bq-br))


def selected(row: dict, fields: tuple[str, ...]) -> dict:
    return {key: row[key] for key in fields if key in row}


class ContextCurator:
    def __init__(self, facade: Any, player_id: int, budget: int, memory: str = '', *,
                 research_building_briefing: bool = False):
        if type(research_building_briefing) is not bool:
            raise ValueError('research briefing opt-in must be boolean')
        self.research_building_briefing = research_building_briefing
        self.facade, self.player_id, self.budget = facade, player_id, budget
        self.state: dict[str, Any] = {'get_strategy': memory}
        self.dirty = {'get_visible_map', 'get_units', 'get_cities', 'get_overview'}
        self.production: dict[str, list] = {}
        self.production_dirty: set[str] = set()
        self.production_focus: set[str] = set()
        self.research_dirty = True
        self.research_source = 'deferred'
        self.research_force = False
        self.revision = 0
        self.focus: str | None = None
        self.last_render_audit: dict | None = None

    async def read(self, name: str, **args) -> Any:
        result = await getattr(self.facade, name)(**args)
        if isinstance(result, dict) and (result.get('status') == 'rejected' or 'error' in result):
            raise MatchAborted(f'context read {name} unavailable: '
                               f'{result.get("rejection", result.get("error"))}')
        return result

    def own(self, kind: str) -> list[dict]:
        return [row for row in self.state.get(kind, [])
                if row.get('owner', row.get('owner_id')) == self.player_id]

    async def refresh(self, *, include_options: bool = True,
                      include_overview: bool = True) -> dict:
        if include_options and not include_overview:
            raise ValueError('option refresh requires current overview')
        previous_cities = {city['city_id'] for city in self.own('get_cities')}
        # Map reads update the live adapter's sight cache. Every subsequent
        # projected entity read must use that refreshed sight, including cities.
        if 'get_visible_map' in self.dirty:
            self.dirty.update({'get_units', 'get_cities'})
        for name in ('get_visible_map', 'get_units', 'get_cities', 'get_overview'):
            if name not in self.dirty or (name == 'get_overview' and not include_overview):
                continue
            value = await self.read(name)
            expected = list if name in ('get_units', 'get_cities') else dict
            if not isinstance(value, expected):
                raise MatchAborted(f'context {name} has invalid shape')
            if isinstance(value, list) and any(not isinstance(row, dict) for row in value):
                raise MatchAborted(f'context {name} has invalid entity rows')
            self.state[name] = value
            self.dirty.discard(name)
        cities = self.own('get_cities')
        own_ids = {city['city_id'] for city in cities}
        self.production = {key: value for key, value in self.production.items() if key in own_ids}
        self.production_dirty.intersection_update(own_ids)
        self.production_focus.intersection_update(own_ids)
        for city in cities:
            cid = city['city_id']
            if (self.revision and cid not in previous_cities):
                self.production_dirty.add(cid)
            wanted = not city.get('production_queue') or cid in self.production_focus
            if not wanted:
                self.production.pop(cid, None)
                self.production_dirty.discard(cid)
            if include_options and wanted and (cid in self.production_dirty
                                                or cid not in self.production):
                self.production.pop(cid, None)
                options = await self.read('get_available_production', city_id=cid)
                if not isinstance(options, list):
                    raise MatchAborted('context production options have invalid shape')
                self.production[cid] = options
                self.production_dirty.discard(cid)
        # Do not expose a pre-action catalog as current while its refresh is deferred.
        for cid in self.production_dirty:
            self.production.pop(cid, None)
        researching = self.state.get('get_overview', {}).get('you', {}).get('researching')
        if self.research_dirty and include_options:
            # An existing choice does not need a repeated full technology catalog.
            if not researching or self.research_force:
                self.state.pop('get_available_research', None)
                self.state.pop('research_building_unlocks', None)
                if self.research_building_briefing:
                    from civ_arena.game.civ6.research_briefing import project
                    value = await self.read('get_available_research',
                                            research_building_briefing=True)
                    value = project(value, self.player_id)
                    options = value['options']
                    snapshot_turn = value['building_unlocks']['snapshot']['turn']
                    observed_turn = self.state['get_overview'].get('turn')
                    if observed_turn is not None and snapshot_turn != observed_turn:
                        raise MatchAborted('research briefing observation turn changed')
                    self.state['research_building_unlocks'] = value['building_unlocks']
                else:
                    options = await self.read('get_available_research')
                if not isinstance(options, list):
                    raise MatchAborted('context research options have invalid shape')
                self.state['get_available_research'] = options
                self.research_source = 'observed'
            else:
                self.state['get_available_research'] = []
                self.state.pop('research_building_unlocks', None)
                self.research_source = 'not_requested_active_choice'
            self.research_dirty = False
            self.research_force = False
        elif self.research_dirty:
            self.state.pop('get_available_research', None)
            self.state.pop('research_building_unlocks', None)
        self.state['get_available_production'] = {'by_city': self.production}
        self.revision += 1
        return self.state

    async def execute(self, name: str, args: dict) -> Any:
        if name not in GAME_ACTIONS:
            raise ValueError('curator executor accepts game actions only')
        result = await getattr(self.facade, name)(**args)
        self.action_result(name, args, result)
        return result

    def action_result(self, name: str, args: dict, result: Any) -> dict:
        """Invalidate on attempts, not acceptance; raw engine detail stays in the audit."""
        if name in ('move_unit', 'attack', 'found_city', 'purchase'):
            self.dirty.update({'get_visible_map', 'get_overview'})
            # Native rewards, combat and purchases may change economic choices.
            # Defer these catalogs during scouting, then query only current demand.
            self.production_dirty.update(self.production)
            self.research_dirty = True
        if name in ('found_city', 'purchase', 'set_research'):
            self.dirty.add('get_overview')
        if name == 'fortify':
            self.dirty.add('get_units')
        if name == 'set_city_production':
            self.dirty.add('get_cities')
            if isinstance(args.get('item_id'), str) and args['item_id'].startswith('DISTRICT_'):
                # Placement affects the map and city economics outside the old digest.
                self.dirty.update({'get_visible_map', 'get_overview'})
                self.production_dirty.update(self.production)
                self.research_dirty = True
            if isinstance(result, dict) and result.get('status') == 'rejected':
                self.production_dirty.add(args['city_id'])
            else:
                self.production.pop(args['city_id'], None)
        if name == 'set_research':
            self.research_dirty = True
            self.research_force = (isinstance(result, dict)
                                   and result.get('status') == 'rejected')
        if name == 'purchase':
            self.production_dirty.update(self.production)
        if not isinstance(result, dict):
            raise MatchAborted(f'context action {name} returned no status')
        # Neither result.detail nor raw mutation.pos uses the facade's coordinate
        # contract. Never ask the model to infer canonical coordinates from them.
        out = selected(result, ('status', 'rejection', 'duplicate', 'unmoved_units'))
        out['tool'] = name
        out['state'] = 'See the controller observation after this batch; acceptance alone '
        out['state'] += 'does not establish that the requested destination was reached.'
        return out

    async def inspect(self, args: dict) -> dict:
        if set(args) == {'city_id'} and isinstance(args['city_id'], str):
            cid = args['city_id']
            if cid not in {city['city_id'] for city in self.own('get_cities')}:
                return {'status': 'rejected', 'rejection': 'not_an_observed_owned_city'}
            self.production_focus.add(cid)
            if cid not in self.production:
                self.production_dirty.add(cid)
        elif set(args) == {'coord'} and isinstance(args['coord'], str):
            if args['coord'] not in self.state['get_visible_map']['tiles']:
                return {'status': 'rejected', 'rejection': 'tile_not_known'}
            self.focus = args['coord']
        else:
            return {'status': 'rejected', 'rejection': 'supply_exactly_city_id_or_coord'}
        return {'status': 'accepted', 'detail': 'Focused detail is included in controller context.'}

    def cached_read(self, name: str, args: dict) -> dict:
        # Old model habits remain harmless: no new RPC and no stale intermediate
        # state after an action. The next request carries the refreshed snapshot.
        if self.dirty or self.research_dirty or (
                name == 'get_available_production' and args.get('city_id')
                in self.production_dirty):
            return {'source': 'controller_context', 'state': 'Refresh follows this action batch.'}
        option_source = None
        if name == 'get_available_production':
            option_source = self.choice_status()['production'].get(
                args.get('city_id'), 'not_an_observed_owned_city')
        elif name == 'get_available_research':
            option_source = self.choice_status()['research']
        if option_source is not None and option_source != 'observed':
            return {'source': 'controller_cache', 'revision': self.revision,
                    'option_source': option_source,
                    'hint': 'Unqueried options are not an observed empty list.'}
        value = self.production.get(args.get('city_id'), []) if name == 'get_available_production' \
            else self.state.get(name)
        if name == 'get_visible_map':
            value = {'state': 'Known terrain is included in controller context.'}
        payload = {'source': 'controller_cache', 'revision': self.revision, 'value': value,
                   'hint': 'Use controller context and act; basic reads need no tool call.'}
        if len(encode(payload)) > 512:
            payload.pop('value')
            payload['state'] = 'Full current decision state is in controller context.'
        return payload

    def choice_status(self) -> dict:
        """Distinguish queried-empty from deliberately unqueried active choices."""
        research = 'deferred' if self.research_dirty else self.research_source
        return {'research': research, 'production': {
            city['city_id']: ('deferred' if city['city_id'] in self.production_dirty else
                             'observed' if city['city_id'] in self.production else
                             'not_requested_active_choice' if city.get('production_queue')
                             else 'deferred')
            for city in self.own('get_cities')}}

    def render(self, *, adaptive_task: str | None = None,
               target_chars: int | None = None) -> str:
        if adaptive_task is not None and adaptive_task not in ('strategy', 'economy', 'contact'):
            raise ValueError('unsupported adaptive briefing task')
        if self.research_building_briefing and adaptive_task is None:
            raise ValueError('research building briefing requires adaptive context')
        if self.dirty:
            raise MatchAborted('controller context requires post-action refresh')
        units = self.state['get_units']
        cities = self.state['get_cities']
        unit_fields = ('unit_id', 'type', 'coord', 'movement', 'max_movement', 'hp', 'max_hp',
                       'health_valid', 'hp_bucket', 'strength', 'ranged_strength', 'fortified',
                       'is_barbarian')
        city_fields = ('city_id', 'coord', 'population', 'production_queue', 'hp')
        production_fields = ('item_id', 'kind', 'cost', 'turns', 'placements')
        mine_u = self.own('get_units')
        mine_c = self.own('get_cities')
        overview = self.state['get_overview']
        doc = {'revision': self.revision, 'own_units': [selected(x, unit_fields) for x in mine_u],
               'own_cities': [selected(x, city_fields) for x in mine_c],
               'visible_foreign_units': [selected(x, unit_fields)
                                         for x in units if x not in mine_u],
               'visible_foreign_cities': [selected(x, city_fields)
                                          for x in cities if x not in mine_c],
               'you': overview.get('you', {}), 'public': overview.get('public', {}),
               'research_options': self.state.get('get_available_research', []),
               'option_sources': self.choice_status(),
               'production_options': {cid: [selected(x, production_fields)
                                            for x in options]
                                      for cid, options in sorted(self.production.items())},
               'terrain': [], 'terrain_omitted': 0,
               'native_terrain_scope': 'Optional source type/biome/hills; absent or null means '
                    'unknown. terrain remains the normalized movement class. '
                    'Known terrain may be remembered; no map limits or latitude are supplied.',
               'scope': 'Known terrain is not a legal-move list. Observations follow the previous '
                        'action batch; accepted movement may leave position unchanged.'}
        if self.research_building_briefing:
            doc['research_building_unlocks'] = self.state.get('research_building_unlocks', {
                'status': 'deferred' if self.research_dirty else self.research_source,
                'action_authority': 'none'})
            # Metadata is conditional, not a substitute for unqueried city internals.
            doc['research_building_city_conditions'] = {
                city['city_id']: {'built_or_pillaged': 'unknown',
                    'prerequisite_buildings_satisfied': 'unknown',
                    'city_center_complete': 'unknown', 'river_and_terrain_requirements': 'unknown',
                    'future_native_build_legality': 'unknown',
                    'current_supported_catalog_source': 'observed' if city['city_id'] in
                    self.production else 'not_requested_active_queue_or_deferred'}
                for city in mine_c}
        tiles = self.state['get_visible_map'].get('tiles')
        if not isinstance(tiles, dict):
            raise MatchAborted('context map tiles have invalid shape')
        anchors = [row['coord'] for row in mine_u + mine_c]
        if anchors and any('native_terrain' in tile for tile in tiles.values()):
            reference = (sorted(mine_c, key=lambda row: row['city_id'])[0]['coord'] if mine_c
                         else sorted(mine_u, key=lambda row: row['unit_id'])[0]['coord'])
            doc['cold_biome_evidence'] = cold_biome_evidence(tiles, reference)
        if self.focus:
            anchors.append(self.focus)
        if adaptive_task == 'contact':
            anchors.extend(row['coord'] for row in units + cities if row not in mine_u + mine_c)
        focus_anchors = ([row['coord'] for row in mine_c] if adaptive_task == 'economy'
                         else anchors) or anchors
        if adaptive_task is not None:
            doc['briefing_scope'] = {'task': adaptive_task,
                'tactical_overrides_allowed': adaptive_task != 'economy',
                'actors': 'all projected actors retained',
                'mandatory_terrain': 'known adjacent tiles of all owned actors; '
                    'contact review also includes observed contacts',
                'optional_terrain': 'known radius-two tiles; unrelated distant map omitted'}
        ordered = sorted(tiles, key=lambda key: (min((distance(key, at) for at in focus_anchors),
                                                     default=0), key))
        for key in ordered:
            if any(distance(key, at) <= 1 for at in anchors):
                doc['terrain'].append({'coord': key, **terrain_fields(tiles[key]),
                    **selected(tiles[key], ('owner_id', 'city_id'))})
        required = len(doc['terrain'])
        doc['terrain_omitted'] = len(tiles) - required
        compact = len(CONTEXT_MARKER) + len(encode(doc)) > self.budget
        compact_actors = False

        def rendered() -> str:
            if adaptive_task is not None:
                variants = (doc, compact_terrain(doc), compact_entities(doc),
                            compact_entities(compact_terrain(doc)))
                return min((CONTEXT_MARKER + encode(view) for view in variants), key=len)
            view = compact_terrain(doc) if compact else doc
            return CONTEXT_MARKER + encode(compact_entities(view) if compact_actors else view)

        if adaptive_task is None and compact and len(rendered()) > self.budget:
            compact_actors = True
        if adaptive_task is None and len(rendered()) > self.budget:
            raise MatchAborted('critical owned state and nearby terrain exceed context budget')
        mandatory_chars = len(rendered())
        included = {row['coord'] for row in doc['terrain']}
        for key in ordered:
            if adaptive_task is None and len(doc['terrain']) >= max(48, required):
                break
            if key in included or not any(distance(key, at) <= 2 for at in anchors):
                continue
            row = {'coord': key, **terrain_fields(tiles[key]),
                   **selected(tiles[key], ('owner_id', 'city_id'))}
            doc['terrain'].append(row)
            doc['terrain_omitted'] -= 1
            limit = self.budget if adaptive_task is None else target_chars
            if limit is not None and len(rendered()) > limit:
                doc['terrain'].pop()
                doc['terrain_omitted'] += 1
                break
        result = rendered()
        if adaptive_task is not None:
            self.last_render_audit = {
                'task': adaptive_task, 'soft_target_chars': target_chars,
                'mandatory_context_chars': mandatory_chars, 'context_chars': len(result),
                'mandatory_terrain_rows': required, 'retained_terrain_rows': len(doc['terrain']),
                'terrain_omitted': doc['terrain_omitted'],
                'owned_units': len(mine_u), 'owned_cities': len(mine_c),
                'visible_contacts': len(units) + len(cities) - len(mine_u) - len(mine_c),
                'focus_coordinates': sorted(set(focus_anchors)),
                'expanded': target_chars is not None and mandatory_chars > target_chars,
                'expansion_reason': 'mandatory relevant observations retained beyond soft target'
                    if target_chars is not None and mandatory_chars > target_chars else None}
        return result


def replace_context(messages: list[dict], content: list[dict], snapshot: str) -> None:
    """Retain exact assistant/tool pairing; supersede only controller-authored state."""
    for message in messages:
        blocks = message.get('content')
        if message['role'] != 'user' or not isinstance(blocks, list):
            continue
        message['content'] = [dict(block) for block in blocks if not (
            block.get('type') == 'text' and block.get('text', '').startswith(CONTEXT_MARKER))]
        for block in message['content']:
            if block.get('type') == 'tool_result':
                try:
                    payload = json.loads(block['content'])
                except (ValueError, TypeError):
                    continue
                if isinstance(payload, dict) and payload.get('source') in (
                        'controller_cache', 'controller_context'):
                    block['content'] = encode({'source': 'controller_context', 'superseded': True})
    messages[:] = [m for m in messages if m.get('content') != []]
    content.append({'type': 'text', 'text': snapshot})
