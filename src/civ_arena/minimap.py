"""Offline, explicitly scoped viewer of retained player-projected model packets."""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import math
import re
from copy import deepcopy
from pathlib import Path

from civ_arena.agents.llm.terrain_context import entity_rows, terrain_rows

MAX_BYTES = 32 * 1024 * 1024
KINDS = ('own_units', 'own_cities', 'visible_foreign_units', 'visible_foreign_cities')

# Spectator world package bounds (M4 shared contract §3). The producer caps the
# canonical package at 256 KiB and these row counts; the viewer re-checks them so
# a hostile or corrupt log row can never reach the page as a "world".
MAX_WORLD_BYTES = 256 * 1024
WORLD_ROSTER_MAX = 64
WORLD_PLAYERS_MAX = 64
WORLD_CITIES_MAX = 256
WORLD_TILES_MAX = 4096
WORLD_DISAGREE_MAX = 64
WORLD_LIST_MAX = 128
WORLD_CITY_LIST_MAX = 64
WORLD_OWNER_KEY = re.compile(r'[0-9]{1,10}')
WORLD_ROSTER_ROW = frozenset({'player_id', 'civ_name', 'leader', 'is_major', 'is_barbarian',
                              'alive', 'level', 'kind', 'suzerain'})
WORLD_PLAYER_ROW = frozenset({'player_id', 'civ_name', 'gold', 'gold_per_turn', 'science',
                              'culture', 'faith', 'upkeep', 'era', 'researching', 'researched',
                              'civics'})
WORLD_PLAYER_NUMERIC = ('gold', 'gold_per_turn', 'science', 'culture', 'faith', 'upkeep')
WORLD_CITY_ROW = frozenset({'city_id', 'owner', 'q', 'r', 'name', 'population', 'is_capital',
                            'is_major', 'hp', 'max_hp', 'production_queue', 'buildings',
                            'districts'})
WORLD_TILE_ROW = frozenset({'q', 'r', 'terrain', 'feature', 'resource', 'improvement',
                            'district', 'river', 'city'})
WORLD_WORLD_KEY = 'spectator_world'
WORLD_DROPPED = ('Spectator world capture present but unusable; the omniscient territory '
                 'layer is withheld.')


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate JSON key')
        result[key] = value
    return result


def read_json(path):
    data = path.read_bytes()
    if len(data) > MAX_BYTES:
        raise ValueError('input exceeds 32 MiB')
    return json.loads(data, object_pairs_hook=unique_object)


def coord(value):
    if not isinstance(value, str) or not re.fullmatch(r'-?\d{1,5},-?\d{1,5}', value):
        raise ValueError('invalid axial coordinate')
    q, r = map(int, value.split(','))
    if value != f'{q},{r}' or max(abs(q), abs(r)) > 10000:
        raise ValueError('noncanonical or excessive coordinate')
    return value


def integer(value, name, minimum=0):
    if type(value) is not int or not minimum <= value <= 10**9:
        raise ValueError(f'invalid {name}')
    return value


def public_roster(state):
    """Closed copy of the packet's public majors roster; None when unsupplied.

    The roster only names alive majors, so owner IDs outside it are non-major
    (city-state or other) — the viewer must never promote them to a civ.
    """
    public = state.get('public')
    if not isinstance(public, dict) or 'players' not in public:
        return None
    players = public['players']
    if not isinstance(players, list) or len(players) > 64:
        raise ValueError('invalid public roster')
    roster = []
    for row in players:
        if not isinstance(row, dict) or type(row.get('alive')) is not bool or \
                not isinstance(row.get('civ_name'), str) or len(row['civ_name']) > 128:
            raise ValueError('invalid public roster')
        roster.append({'player_id': integer(row.get('player_id'), 'public roster player'),
                       'civ_name': row['civ_name'], 'alive': row['alive']})
    return roster


def research_summary(state):
    """Closed copy of the packet's own research context; None when unsupplied.

    Options are only the ones the packet recorded as observed; an empty or
    absent list is never a claim that nothing else could be picked. An absent
    researched list stays null for the same reason: unsupplied is not empty.
    """
    you = state.get('you')
    you = you if isinstance(you, dict) else {}
    supplied = 'research_options' in state
    if not supplied and 'researched' not in you and 'researching' not in you:
        return None
    researching = you.get('researching')
    if researching is not None and not isinstance(researching, str):
        raise ValueError('invalid research context')
    if not (isinstance(researching, str) and 1 <= len(researching) <= 64):
        researching = None
    researched = None
    if 'researched' in you:
        researched = you['researched']
        if not isinstance(researched, list) or len(researched) > 128:
            raise ValueError('invalid research context')
        for name in researched:
            if not isinstance(name, str) or not 1 <= len(name) <= 64:
                raise ValueError('invalid research context')
    options = None
    if supplied:
        rows = state['research_options']
        if not isinstance(rows, list) or len(rows) > 128:
            raise ValueError('invalid research context')
        options = []
        for row in rows:
            if not isinstance(row, dict):
                raise ValueError('invalid research context')
            tech_id, cost = row.get('tech_id'), row.get('cost')
            if not isinstance(tech_id, str) or not 1 <= len(tech_id) <= 64:
                raise ValueError('invalid research context')
            if type(cost) is not int or not 0 <= cost <= 10**9:
                raise ValueError('invalid research context')
            options.append({'tech_id': tech_id, 'cost': cost})
    sources = state.get('option_sources')
    origin = sources.get('research') if isinstance(sources, dict) else None
    if not isinstance(origin, str) or len(origin) > 64:
        origin = None
    return {'researching': researching,
            'researched': None if researched is None else list(researched),
            'options': options, 'options_source': origin}


def _world_int(value, name, minimum=0, maximum=10 ** 9):
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError(f'invalid world {name}')
    return value


def _world_number(value, name, minimum=-10 ** 9, maximum=10 ** 9):
    if type(value) not in (int, float) or not minimum <= value <= maximum \
            or not math.isfinite(value):
        raise ValueError(f'invalid world {name}')
    return value


def _world_text(value, name, limit=128):
    if not isinstance(value, str) or len(value) > limit:
        raise ValueError(f'invalid world {name}')
    return value


def _world_optional_text(value, name, limit=128):
    if value is None:
        return None
    return _world_text(value, name, limit)


def _world_optional_flag(value, name):
    if value is None:
        return None
    return _world_flag(value, name)


def _world_optional_int(value, name, minimum=0, maximum=10 ** 9):
    if value is None:
        return None
    return _world_int(value, name, minimum, maximum)


def _world_flag(value, name):
    if type(value) is not bool:
        raise ValueError(f'invalid world {name}')
    return value


def _world_keys(row, allowed, name):
    if not isinstance(row, dict) or not set(row) <= allowed:
        raise ValueError(f'invalid world {name}')
    return row


def validate_world(doc):
    """Closed copy of a spectator world package (M4 contract §3, schema v1).

    Blank stays unsupplied: an optional key may be absent but never silently
    re-typed, every coordinate must pass :func:`coord`, and the producer's row
    bounds are re-checked so a corrupt or hostile log row can never reach the
    page dressed as an omniscient world. Anything unusable raises; the caller
    attaches nothing rather than a partial world.
    """
    if not isinstance(doc, dict):
        raise ValueError('invalid world document')
    if len(canonical(doc).encode()) > MAX_WORLD_BYTES:
        raise ValueError('world exceeds size bound')
    # Amendment 3 item 2 + 9: world doc carries optional top-level
    # extras the viewer MUST tolerate — `digest_consistent` (spectate
    # carrier's bracket flag), `palette_confirmed` (M4 producer always
    # emits it). `owned_tiles_columns` may be absent when the byte cap
    # dropped the territory block.
    if set(doc) - {'schema', 'after_seat', 'contexts', 'game_era', 'grid', 'roster', 'players',
                   'cities', 'owned_tiles_columns', 'fog_audit', 'palette', 'palette_confirmed',
                   'truncated', 'read_ms', 'digest_consistent'}:
        raise ValueError('invalid world top-level keys')
    if doc.get('schema') != 1 or type(doc.get('schema')) is not int:
        raise ValueError('invalid world schema')
    _world_int(doc.get('after_seat'), 'after_seat', -1)
    contexts = doc.get('contexts')
    if not isinstance(contexts, dict) or set(contexts) != {'roster', 'tiles', 'palette'} \
            or contexts['roster'] != 'gamecore' or contexts['tiles'] != 'gamecore' \
            or contexts['palette'] not in ('ingame', 'absent'):
        raise ValueError('invalid world contexts')
    if 'game_era' in doc:
        _world_text(doc['game_era'], 'game_era', 64)
    grid = doc.get('grid')
    if not isinstance(grid, dict) or set(grid) != {'w', 'h'}:
        raise ValueError('invalid world grid')
    _world_int(grid.get('w'), 'grid w', 1, 10 ** 6)
    _world_int(grid.get('h'), 'grid h', 1, 10 ** 6)
    roster = doc.get('roster')
    if not isinstance(roster, list) or len(roster) > WORLD_ROSTER_MAX:
        raise ValueError('invalid world roster')
    for row in roster:
        # Amendment 3 item 2: roster fields are OPTIONAL — absent =
        # unsupplied. The producer keeps '?' unread off the wire
        # (Amendment 1: GetCivilizationLevelType missing), so level
        # never appears; is_major/is_barbarian/alive/civ_name/leader/
        # suzerain may all be absent when unread on a live probe.
        _world_keys(row, WORLD_ROSTER_ROW, 'roster row')
        _world_int(row.get('player_id'), 'roster player')
        _world_optional_text(row.get('civ_name'), 'roster civ_name')
        _world_optional_text(row.get('leader'), 'roster leader')
        _world_optional_text(row.get('level'), 'roster level', 64)
        _world_optional_text(row.get('kind'), 'roster kind', 64)
        _world_optional_flag(row.get('is_major'), 'roster is_major')
        _world_optional_flag(row.get('is_barbarian'), 'roster is_barbarian')
        _world_optional_flag(row.get('alive'), 'roster alive')
        _world_optional_int(row.get('suzerain'), 'roster suzerain', -1)
    players = doc.get('players')
    if not isinstance(players, list) or len(players) > WORLD_PLAYERS_MAX:
        raise ValueError('invalid world players')
    for row in players:
        _world_keys(row, WORLD_PLAYER_ROW, 'player row')
        _world_int(row.get('player_id'), 'player id')
        # Amendment 3 item 2: civ_name / era / researching are optional;
        # the producer omits researching when unread/none-active and
        # omits era when the GameInfo.Eras lookup failed.
        _world_optional_text(row.get('civ_name'), 'player civ_name')
        _world_optional_text(row.get('era'), 'player era', 64)
        _world_optional_text(row.get('researching'), 'player researching', 64)
        for field in WORLD_PLAYER_NUMERIC:
            if field in row:
                _world_number(row[field], f'player {field}')
        for field in ('researched', 'civics'):
            names = row.get(field)
            if names is None:
                continue
            if not isinstance(names, list) or len(names) > WORLD_LIST_MAX:
                raise ValueError(f'invalid world player {field}')
            for name in names:
                _world_text(name, f'player {field} entry', 64)
                if not name:
                    raise ValueError(f'invalid world player {field} entry')
    cities = doc.get('cities')
    if not isinstance(cities, list) or len(cities) > WORLD_CITIES_MAX:
        raise ValueError('invalid world cities')
    for row in cities:
        _world_keys(row, WORLD_CITY_ROW, 'city row')
        city_id = row.get('city_id')
        if not isinstance(city_id, str) or not 1 <= len(city_id) <= 128:
            raise ValueError('invalid world city_id')
        _world_int(row.get('owner'), 'city owner')
        for field in ('q', 'r'):
            _world_int(row.get(field), f'city {field}', -10000, 10000)
        if 'name' in row:
            _world_text(row['name'], 'city name')
        population = row.get('population')
        if population is not None:
            _world_int(population, 'city population')
        for field in ('is_capital', 'is_major'):
            if field in row:
                _world_flag(row[field], f'city {field}')
        if ('hp' in row) != ('max_hp' in row):
            raise ValueError('invalid world city hp pair')
        if 'hp' in row:
            hp, max_hp = row['hp'], row['max_hp']
            _world_int(hp, 'city hp', 0, 10 ** 6)
            _world_int(max_hp, 'city max_hp', 0, 10 ** 6)
            if hp > max_hp:
                raise ValueError('invalid world city hp pair')
        for field in ('production_queue', 'buildings', 'districts'):
            items = row.get(field)
            if items is None:
                continue
            if not isinstance(items, list) or len(items) > WORLD_CITY_LIST_MAX:
                raise ValueError(f'invalid world city {field}')
            for item in items:
                _world_text(item, f'city {field} entry')
    columns = doc.get('owned_tiles_columns')
    if columns is None:
        # Amendment 3 item 2: the byte cap may drop the territory block
        # first — territory layer degrades gracefully with the recorded
        # `truncated.world: true` already on the doc.
        seen, total = set(), 0
    elif isinstance(columns, dict):
        seen, total = set(), 0
        for owner, rows in columns.items():
            if not WORLD_OWNER_KEY.fullmatch(owner) or not isinstance(rows, list):
                raise ValueError('invalid world owned tile column')
            for row in rows:
                _world_keys(row, WORLD_TILE_ROW, 'owned tile row')
                q, r = row.get('q'), row.get('r')
                _world_int(q, 'tile q', -10000, 10000)
                _world_int(r, 'tile r', -10000, 10000)
                key = coord(f'{q},{r}')
                if key in seen:
                    raise ValueError('duplicate owned tile coordinate')
                seen.add(key)
                for field in ('terrain', 'feature', 'resource', 'improvement', 'district'):
                    if field in row:
                        _world_text(row[field], f'tile {field}', 64)
                if 'river' in row:
                    _world_flag(row['river'], 'tile river')
                if 'city' in row:
                    _world_int(row['city'], 'tile city', -1)
                total += 1
        if total > WORLD_TILES_MAX:
            raise ValueError('invalid world owned tile count')
    else:
        raise ValueError('invalid world owned tiles')
    fog = doc.get('fog_audit')
    if not isinstance(fog, dict) or set(fog) != {'requested', 'engine_visible',
                                                 'engine_not_visible', 'unavailable',
                                                 'disagree_coords'}:
        raise ValueError('invalid world fog audit')
    for field in ('requested', 'engine_visible', 'engine_not_visible', 'unavailable'):
        _world_int(fog.get(field), f'fog {field}')
    disagree = fog['disagree_coords']
    if not isinstance(disagree, list) or len(disagree) > WORLD_DISAGREE_MAX:
        raise ValueError('invalid world disagree coords')
    for value in disagree:
        coord(value)
    if 'palette_confirmed' in doc:
        # Amendment 3 item 9: when present, must be bool — the viewer
        # uses engine palette ints ONLY when this is true; absence or
        # false means the M1 owner-class fallback applies.
        _world_flag(doc['palette_confirmed'], 'palette_confirmed')
    if 'digest_consistent' in doc:
        # Amendment 3 item 2: the spectate carrier's bracket flag.
        _world_flag(doc['digest_consistent'], 'digest_consistent')
    if 'palette' in doc:
        palette = doc['palette']
        if not isinstance(palette, dict):
            raise ValueError('invalid world palette')
        for owner, colour in palette.items():
            if not WORLD_OWNER_KEY.fullmatch(owner) or not isinstance(colour, dict) \
                    or set(colour) != {'primary', 'secondary'}:
                raise ValueError('invalid world palette row')
            _world_int(colour['primary'], 'palette primary', 0, 2 ** 32 - 1)
            _world_int(colour['secondary'], 'palette secondary', 0, 2 ** 32 - 1)
    truncated = doc.get('truncated')
    truncated_keys = {'tiles', 'world', 'roster', 'cities'}
    if not isinstance(truncated, dict) \
            or not set(truncated) <= truncated_keys \
            or not {'tiles', 'world'} <= set(truncated):
        raise ValueError('invalid world truncation record')
    _world_flag(truncated.get('tiles'), 'truncated tiles')
    _world_flag(truncated.get('world'), 'truncated world')
    if 'roster' in truncated:
        _world_int(truncated['roster'], 'truncated roster')
    if 'cities' in truncated:
        _world_int(truncated['cities'], 'truncated cities')
    _world_number(doc.get('read_ms'), 'read_ms', 0, 10 ** 6)
    return deepcopy(doc)


def world_records(events, cutoff_turn):
    """World-carrying records at/before the bundle's turn cutoff, in stream order.

    A record is either the hotseat `spectator_world` audit (spectator scope) or a
    `SPECTATOR_SNAPSHOT` whose payload carries a `world` block. Anything else is
    inert here, exactly as it is everywhere else in this viewer.
    """
    found = []
    for event in events:
        if event.get('audit') == WORLD_WORLD_KEY:
            if event.get('visibility_scope') != 'spectator' or 'world' not in event:
                continue
        elif event.get('kind') != 'SPECTATOR_SNAPSHOT' or 'world' not in event:
            continue
        turn = event.get('turn')
        if type(turn) is not int or not 0 <= turn <= cutoff_turn:
            continue
        found.append(event)
    return found


def build(packets: list[dict], events: list[dict], *, player: int | None = None,
          spectator: bool = False) -> dict:
    """Materialize receipt history, never infer live visibility or actor persistence.

    A player export contains only that player's projected packets and audits.
    Spectator exports explicitly contain each supplied perspective; their union
    is observation history, not authoritative simultaneous world state.
    """
    if spectator == (player is not None):
        raise ValueError('choose exactly one player or explicit spectator mode')
    if player is not None:
        integer(player, 'player')
    # Amendment 3 item 5: spectate-only runs carry no strategy packets
    # (the player route still requires >= 1 packet — a player bundle
    # without any observation is not a bundle). The spectator route
    # may have ZERO packets and still render the territory layer
    # from spectator_world / SPECTATOR_SNAPSHOT.world records.
    if len(packets) > 256 or len(events) > 100000:
        raise ValueError('input record limit')
    # Amendment 3 item 1: spectator-scope records are inert for player
    # routes. The packet walk already drops them implicitly (spectator
    # audits carry player_id=None), but the event walk below feeds
    # graph_events / own_events / productive_cutoff and accepts whatever
    # player_id a hostile spectator audit carries. Strip them here,
    # BEFORE any player-id matching, so a spectator audit can never
    # influence a player bundle even if it impersonates a player_id.
    player_events = [e for e in events
                     if e.get('visibility_scope') != 'spectator']
    event_index = {}
    run_ids = set()
    for event in player_events:
        seq = integer(event['seq'], 'event sequence')
        if seq in event_index:
            raise ValueError('duplicate event sequence')
        event_index[seq] = event
        run_ids.add((event['match_id'], event['game_instance_id']))
    if len(run_ids) > 1:
        raise ValueError('events mix different runs')
    ordered, identities = [], set()
    for packet in packets:
        e, state = packet['event'], packet['projected_state']
        request = packet['request']
        context = request['user_context']
        if (len(context) != request['context_chars'] or
                hashlib.sha256(context.encode()).hexdigest() != request['context_sha256']):
            raise ValueError('packet context hash/length mismatch')
        marker = '\nController context (projected observations, axial coordinates):\n'
        if context.count(marker) != 1 or json.loads(context.split(marker)[1],
                object_pairs_hook=unique_object) != state:
            raise ValueError('projected state differs from supplied model context')
        pid = integer(e['player_id'], 'player')
        seq, turn = integer(e['seq'], 'sequence'), integer(e['turn'], 'turn', 1)
        if state['you']['player_id'] != pid or type(state['you']['player_id']) is not int:
            raise ValueError('packet player identity mismatch')
        if (pid, seq) in identities:
            raise ValueError('duplicate packet identity')
        identities.add((pid, seq))
        if spectator or pid == player:
            if events:
                source = event_index.get(seq, {})
                payload = json.loads(source.get('strategy_payload_json', '{}'),
                                     object_pairs_hook=unique_object)
                if (source.get('audit') != 'strategy_request' or
                        source.get('player_id') != pid or source.get('turn') != turn or
                        payload.get('context_sha256') != request['context_sha256']):
                    raise ValueError('packet is not bound to supplied event stream')
            ordered.append((seq, turn, pid, packet))
    ordered.sort(key=lambda item: item[:3])
    if not ordered and not spectator:
        raise ValueError('no packets for selected player')
    graph_events = []
    for e in player_events:
        if e.get('kind') != 'HEARTBEAT' or e.get('audit') != 'strategy_graph':
            continue
        if e.get('player_id') not in {item[2] for item in ordered}:
            continue
        integer(e['seq'], 'graph sequence')
        integer(e['turn'], 'graph turn', 1)
        payload = json.loads(e['strategy_payload_json'], object_pairs_hook=unique_object) \
            if 'strategy_payload_json' in e else e
        graph = payload['graph']
        if not isinstance(graph, dict) or len(canonical(graph).encode()) > 512 * 1024:
            raise ValueError('invalid or excessive strategy graph')
        if not isinstance(graph.get('directive', {}), dict):
            raise ValueError('invalid directive')
        for field in ('decisions', 'execution'):
            if not isinstance(graph.get(field, []), list) or len(graph.get(field, [])) > 1000:
                raise ValueError('invalid graph rows')
        for decision in graph.get('decisions', []):
            coord(decision['origin'])
            if not isinstance(decision.get('unit_id'), str):
                raise ValueError('invalid graph unit')
            candidates = decision.get('candidates', [])
            if not isinstance(candidates, list) or len(candidates) > 100:
                raise ValueError('invalid graph candidates')
            for candidate in candidates:
                coord(candidate['dest'])
            selected = decision.get('selected')
            if selected is not None and 'dest' in selected.get('args', {}):
                coord(selected['args']['dest'])
        if any(not isinstance(row, dict) for row in graph.get('execution', [])):
            raise ValueError('invalid execution row')
        graph_events.append((e, graph))
    seats = {}
    for seq, turn, pid, packet in ordered:
        state = packet['projected_state']
        seat = seats.setdefault(pid, {'player_id': pid, 'snapshots': [], 'terrain': {}})
        if seat['snapshots'] and turn < seat['snapshots'][-1]['turn']:
            raise ValueError('packet turns move backwards')
        provenance = {'seq': seq, 'turn': turn, 'player_id': pid,
                      'packet_sha256': digest(packet), 'ts': packet['event'].get('ts')}
        tiles = terrain_rows(state)
        if len(tiles) > 10000:
            raise ValueError('terrain row limit')
        seen = set()
        for tile in tiles:
            key = coord(tile['coord'])
            if key in seen:
                raise ValueError('duplicate terrain coordinate')
            seen.add(key)
            seat['terrain'][key] = {'observation': deepcopy(tile), 'receipt': provenance}
        if len(seat['terrain']) > 10000:
            raise ValueError('accumulated terrain limit')
        actors, ids = [], set()
        for kind in KINDS:
            rows = entity_rows(state, kind)
            if len(rows) > 1000:
                raise ValueError('actor row limit')
            for row in rows:
                coord(row['coord'])
                key = row['unit_id' if 'units' in kind else 'city_id']
                if not isinstance(key, str) or not 1 <= len(key) <= 128 or key in ids:
                    raise ValueError('invalid or duplicate actor ID')
                ids.add(key)
                actors.append({'kind': kind, 'id': key, 'observation': deepcopy(row),
                               'receipt': provenance})
        eligible = [(e, g) for e, g in graph_events
                    if e['player_id'] == pid and e['seq'] <= seq and e['turn'] <= turn]
        graph = None
        if eligible:
            e, g = max(eligible, key=lambda item: item[0]['seq'])
            graph = {'value': deepcopy(g), 'receipt': {'seq': e['seq'], 'turn': e['turn'],
                     'player_id': pid, 'event_sha256': digest(e), 'ts': e.get('ts')}}
        snapshot = {'seq': seq, 'turn': turn, 'receipt': provenance,
                    'you': deepcopy(state['you']), 'actors': actors,
                    'terrain': [deepcopy(seat['terrain'][k]) for k in sorted(seat['terrain'])],
                    'packet_terrain_count': len(tiles),
                    'packet_terrain_omitted': state.get('terrain_omitted'), 'graph': graph,
                    'public_players': public_roster(state),
                    'research': research_summary(state)}
        seat['snapshots'].append(snapshot)
    from civ_arena.productive_map import project
    for pid, seat in seats.items():
        # Codex r2 finding 1: pass player_events (the spectator-scope
        # filtered walk) so a hostile spectator audit impersonating a
        # seat's TOOL_CALL can never reach a player bundle's
        # productive_actions list. The graph/cutoff walks already use
        # player_events — this closes the last remaining leak path.
        seat['productive_actions'] = project(player_events, pid)
        own_events = [e for e in player_events if type(e.get('player_id')) is int
                      and e['player_id'] == pid and type(e.get('turn')) is int
                      and e['turn'] >= 1]
        latest = max(own_events, key=lambda e: e['seq']) if own_events else seat['snapshots'][-1]
        seat['productive_cutoff'] = {'seq': latest['seq'],
                                     'turn': max([e['turn'] for e in own_events] +
                                                 [seat['snapshots'][-1]['turn']])}
    result = {'version': 1, 'scope': 'combined_observation_preview' if spectator else 'player_only',
              'limits': ['Retained model packets, not full explored map or live engine state.',
                         'Receipt age is known; actual tile visibility/observation age is unknown.',
                         'Empty space is unsupplied; extents are not map bounds. Wrap is unknown.',
                         'Audit choices do not guarantee legality or confirmed displacement.',
                         'Selection weights are not success probabilities. No expansion forecast.',
                         'Production receipts prove historical queue/placement admission, '
                         'not completion.',
                         'Owner identity: seats and roster majors are named; other owner IDs '
                         'are non-major or unclassified, never inferred.',
                         'Research: researching, researched and recorded options come from the '
                         "packet's own context; options are shown only when the packet says "
                         'they were observed.'],
              'event_binding': {'status': 'matched_context_hash' if events else 'unverified',
                                'run_ids': [list(item) for item in sorted(run_ids)]},
              'axis': 'Increasing r is engine-grid north; screen north is a viewer convention.',
              'seats': [{k: v for k, v in seat.items() if k != 'terrain'}
                        for _, seat in sorted(seats.items())]}
    # The spectator world is selected only on the spectator route: the latest
    # world-carrying record at/before the bundle's as-of turn, validated whole.
    # A present-but-unusable world attaches nothing (never a partial world) and
    # says so once; player routes never look at spectator-scope records.
    world = None
    world_warning = False
    if spectator:
        # Amendment 3 item 5: when packets are present, the packet
        # cutoff stays authoritative — loosening would admit worlds
        # past the bundle turn. When packets are empty (pure spectate
        # run), the cutoff comes from the latest world record itself.
        packet_cutoff = max((turn for _, turn, _, _ in ordered), default=0)
        if packet_cutoff == 0:
            latest_world_turn = max(
                (event.get('turn', 0)
                 for event in world_records(events, 10 ** 9)
                 if isinstance(event.get('turn'), int)),
                default=0)
            cutoff = latest_world_turn
        else:
            cutoff = packet_cutoff
        candidates = world_records(events, cutoff)
        if candidates:
            latest = max(candidates, key=lambda event: event['seq'])
            try:
                doc = validate_world(latest['world'])
            except (ValueError, TypeError, RecursionError):
                world_warning = True
            else:
                world = {**doc, 'receipt': {
                    'seq': latest['seq'], 'turn': latest['turn'],
                    'source': WORLD_WORLD_KEY if latest.get('audit') == WORLD_WORLD_KEY
                    else 'SPECTATOR_SNAPSHOT', 'ts': latest.get('ts')}}
    if world is not None:
        result['world'] = world
    if world_warning:
        result['limits'].append(WORLD_DROPPED)
    result['digest'] = digest(result)
    if len(canonical(result).encode()) > MAX_BYTES:
        raise ValueError('output exceeds 32 MiB')
    return result


ASSETS = Path(__file__).with_name('minimap_static')
PALETTE_PATH = ASSETS / 'palette.json'


def load_palette() -> dict:
    """Presentation palette: the single source for fills, glyphs, owners and tokens.

    It is injected as a separate non-executable JSON block, never into the
    observation bundle, so digests stay observation-only and redaction never
    walks presentation strings.
    """
    palette = json.loads(PALETTE_PATH.read_bytes(), object_pairs_hook=unique_object)
    if palette.get('version') != 1:
        raise ValueError('unsupported palette version')
    return palette


def tokens_css(palette: dict) -> str:
    tokens = palette['tokens']
    if not isinstance(tokens, dict) or not 1 <= len(tokens) <= 256:
        raise ValueError('invalid palette tokens')
    parts = []
    for key, value in tokens.items():
        if (not re.fullmatch(r'[a-z][a-z0-9_]{0,31}', key) or not isinstance(value, str)
                or not re.fullmatch(r'#[0-9a-f]{6}(?:[0-9a-f]{2})?', value)):
            raise ValueError('invalid palette token')
        parts.append(f'--{key}:{value};')
    return ':root{' + ''.join(parts) + '}'


def script_source(assets: Path = ASSETS) -> str:
    """The one pinned script: pure geometry first, then the DOM renderer."""
    return (assets / 'geometry.js').read_text() + '\n' + (assets / 'app.js').read_text()


def escape_json_block(payload: str) -> str:
    return payload.replace('&', '\\u0026').replace('<', '\\u003c').replace('>', '\\u003e')


def render(bundle: dict) -> str:
    if bundle.get('version') != 1 or bundle.get('digest') != digest(
            {k: v for k, v in bundle.items() if k != 'digest'}):
        raise ValueError('bundle digest/version mismatch')
    payload = canonical(bundle)
    if len(payload.encode()) > MAX_BYTES:
        raise ValueError('output exceeds 32 MiB')
    palette = load_palette()
    script = script_source(ASSETS)
    csp_hash = base64.b64encode(hashlib.sha256(script.encode()).digest()).decode()
    replacements = {'STYLE': (ASSETS / 'style.css').read_text(), 'TOKENS': tokens_css(palette),
                    'CSP_HASH': csp_hash, 'DATA': escape_json_block(payload),
                    'PALETTE': escape_json_block(canonical(palette)), 'SCRIPT': script}
    return re.sub(r'@@(STYLE|TOKENS|CSP_HASH|DATA|PALETTE|SCRIPT)@@',
                  lambda match: replacements[match[1]], (ASSETS / 'index.html').read_text())



def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--packet', type=Path, action='append', required=True)
    parser.add_argument('--events', type=Path, help='Optional retained events JSONL, <=32 MiB')
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument('--player', type=int)
    group.add_argument('--spectator', action='store_true')
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--json-output', type=Path)
    args = parser.parse_args()
    try:
        packets = [read_json(path) for path in args.packet]
        events = []
        if args.events:
            data = args.events.read_bytes()
            if len(data) > MAX_BYTES:
                raise ValueError('events exceed 32 MiB')
            events = [json.loads(line, object_pairs_hook=unique_object)
                      for line in data.splitlines() if line.strip()]
        bundle = build(packets, events, player=args.player, spectator=args.spectator)
        html = render(bundle)
        # Exclusive creation prevents replacing source packets or prior evidence.
        with args.output.open('x') as output:
            output.write(html)
        if args.json_output:
            with args.json_output.open('x') as output:
                output.write(canonical(bundle) + '\n')
        print(canonical({'digest': bundle['digest'], 'scope': bundle['scope'],
                         'players': len(bundle['seats']), 'html_bytes': len(html.encode())}))
    except (KeyError, TypeError, ValueError, OSError, RecursionError) as exc:
        parser.exit(2, f'Cannot render minimap: {exc}\n')


if __name__ == '__main__':
    main()
