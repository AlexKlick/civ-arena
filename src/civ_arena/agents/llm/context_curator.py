"""Turn-local, player-projected context; gathering never spends a model request."""
from __future__ import annotations

import json
from typing import Any

from civ_arena.arena.referee import MatchAborted

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
    def __init__(self, facade: Any, player_id: int, budget: int, memory: str = ''):
        self.facade, self.player_id, self.budget = facade, player_id, budget
        self.state: dict[str, Any] = {'get_strategy': memory}
        self.dirty = {'get_visible_map', 'get_units', 'get_cities', 'get_overview'}
        self.production: dict[str, list] = {}
        self.production_dirty: set[str] = set()
        self.research_dirty = True
        self.research_force = False
        self.revision = 0
        self.focus: str | None = None

    async def read(self, name: str, **args) -> Any:
        result = await getattr(self.facade, name)(**args)
        if isinstance(result, dict) and (result.get('status') == 'rejected' or 'error' in result):
            raise MatchAborted(f'context read {name} unavailable: '
                               f'{result.get("rejection", result.get("error"))}')
        return result

    def own(self, kind: str) -> list[dict]:
        return [row for row in self.state.get(kind, [])
                if row.get('owner', row.get('owner_id')) == self.player_id]

    async def refresh(self) -> dict:
        previous_cities = {city['city_id'] for city in self.own('get_cities')}
        # Map reads update the live adapter's sight cache. Every subsequent
        # projected entity read must use that refreshed sight, including cities.
        if 'get_visible_map' in self.dirty:
            self.dirty.update({'get_units', 'get_cities'})
        for name in ('get_visible_map', 'get_units', 'get_cities', 'get_overview'):
            if name not in self.dirty:
                continue
            value = await self.read(name)
            expected = list if name in ('get_units', 'get_cities') else dict
            if not isinstance(value, expected):
                raise MatchAborted(f'context {name} has invalid shape')
            if isinstance(value, list) and any(not isinstance(row, dict) for row in value):
                raise MatchAborted(f'context {name} has invalid entity rows')
            self.state[name] = value
        self.dirty.clear()
        cities = self.own('get_cities')
        own_ids = {city['city_id'] for city in cities}
        self.production = {key: value for key, value in self.production.items() if key in own_ids}
        for city in cities:
            cid = city['city_id']
            if (self.revision and cid not in previous_cities):
                self.production_dirty.add(cid)
            if cid in self.production_dirty or (not city.get('production_queue')
                                                 and cid not in self.production):
                options = await self.read('get_available_production', city_id=cid)
                if not isinstance(options, list):
                    raise MatchAborted('context production options have invalid shape')
                self.production[cid] = options
        self.production_dirty.clear()
        researching = self.state.get('get_overview', {}).get('you', {}).get('researching')
        if self.research_dirty:
            # An existing choice does not need a repeated full technology catalog.
            if not researching or self.research_force:
                options = await self.read('get_available_research')
                if not isinstance(options, list):
                    raise MatchAborted('context research options have invalid shape')
                self.state['get_available_research'] = options
            else:
                self.state['get_available_research'] = []
            self.research_dirty = False
            self.research_force = False
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
            self.dirty.add('get_visible_map')
        if name in ('found_city', 'purchase', 'set_research'):
            self.dirty.add('get_overview')
        if name == 'fortify':
            self.dirty.add('get_units')
        if name == 'set_city_production':
            self.dirty.add('get_cities')
            if isinstance(result, dict) and result.get('status') == 'rejected':
                self.production_dirty.add(args['city_id'])
            else:
                self.production.pop(args['city_id'], None)
        if name == 'set_research':
            self.research_dirty = True
            self.research_force = True
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
        if self.dirty or self.production_dirty or self.research_dirty:
            return {'source': 'controller_context', 'state': 'Refresh follows this action batch.'}
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

    def render(self) -> str:
        units = self.state['get_units']
        cities = self.state['get_cities']
        unit_fields = ('unit_id', 'type', 'coord', 'movement', 'max_movement', 'hp',
                       'hp_bucket', 'strength', 'ranged_strength', 'fortified')
        city_fields = ('city_id', 'coord', 'population', 'production_queue', 'hp')
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
               'production_options': {cid: [selected(x, ('item_id', 'kind', 'cost', 'turns'))
                                            for x in options]
                                      for cid, options in sorted(self.production.items())},
               'terrain': [], 'terrain_omitted': 0,
               'scope': 'Known terrain is not a legal-move list. Observations follow the previous '
                        'action batch; accepted movement may leave position unchanged.'}
        tiles = self.state['get_visible_map'].get('tiles')
        if not isinstance(tiles, dict):
            raise MatchAborted('context map tiles have invalid shape')
        anchors = [row['coord'] for row in mine_u + mine_c]
        if self.focus:
            anchors.append(self.focus)
        ordered = sorted(tiles, key=lambda key: (min((distance(key, at) for at in anchors),
                                                     default=0), key))
        for key in ordered:
            if any(distance(key, at) <= 1 for at in anchors):
                doc['terrain'].append({'coord': key, **selected(
                    tiles[key], ('terrain', 'owner_id', 'city_id'))})
        required = len(doc['terrain'])
        doc['terrain_omitted'] = len(tiles) - required
        if len(CONTEXT_MARKER) + len(encode(doc)) > self.budget:
            raise MatchAborted('critical owned state and nearby terrain exceed context budget')
        included = {row['coord'] for row in doc['terrain']}
        for key in ordered:
            if len(doc['terrain']) >= max(48, required):
                break
            if key in included or not any(distance(key, at) <= 2 for at in anchors):
                continue
            row = {'coord': key, **selected(tiles[key], ('terrain', 'owner_id', 'city_id'))}
            doc['terrain'].append(row)
            doc['terrain_omitted'] -= 1
            if len(CONTEXT_MARKER) + len(encode(doc)) > self.budget:
                doc['terrain'].pop()
                doc['terrain_omitted'] += 1
                break
        return CONTEXT_MARKER + encode(doc)


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
