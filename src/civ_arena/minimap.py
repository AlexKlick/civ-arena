"""Offline, explicitly scoped viewer of retained player-projected model packets."""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import re
from copy import deepcopy
from pathlib import Path

from civ_arena.agents.llm.terrain_context import entity_rows, terrain_rows

MAX_BYTES = 32 * 1024 * 1024
KINDS = ('own_units', 'own_cities', 'visible_foreign_units', 'visible_foreign_cities')


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
    if not 1 <= len(packets) <= 256 or len(events) > 100000:
        raise ValueError('input record limit')
    event_index = {}
    run_ids = set()
    for event in events:
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
    if not ordered:
        raise ValueError('no packets for selected player')
    graph_events = []
    for e in events:
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
                    'packet_terrain_omitted': state.get('terrain_omitted'), 'graph': graph}
        seat['snapshots'].append(snapshot)
    result = {'version': 1, 'scope': 'combined_observation_preview' if spectator else 'player_only',
              'limits': ['Retained model packets, not full explored map or live engine state.',
                         'Receipt age is known; actual tile visibility/observation age is unknown.',
                         'Empty space is unsupplied; extents are not map bounds. Wrap is unknown.',
                         'Audit choices do not guarantee legality or confirmed displacement.',
                         'Selection weights are not success probabilities. No expansion forecast.'],
              'event_binding': {'status': 'matched_context_hash' if events else 'unverified',
                                'run_ids': [list(item) for item in sorted(run_ids)]},
              'axis': 'Increasing r is engine-grid north; screen north is a viewer convention.',
              'seats': [{k: v for k, v in seat.items() if k != 'terrain'}
                        for _, seat in sorted(seats.items())]}
    result['digest'] = digest(result)
    if len(canonical(result).encode()) > MAX_BYTES:
        raise ValueError('output exceeds 32 MiB')
    return result


def render(bundle: dict) -> str:
    if bundle.get('version') != 1 or bundle.get('digest') != digest(
            {k: v for k, v in bundle.items() if k != 'digest'}):
        raise ValueError('bundle digest/version mismatch')
    payload = canonical(bundle)
    if len(payload.encode()) > MAX_BYTES:
        raise ValueError('output exceeds 32 MiB')
    assets = Path(__file__).with_name('minimap_static')
    script = (assets / 'app.js').read_text()
    csp_hash = base64.b64encode(hashlib.sha256(script.encode()).digest()).decode()
    payload = payload.replace('&', '\\u0026').replace('<', '\\u003c').replace('>', '\\u003e')
    replacements = {'STYLE': (assets / 'style.css').read_text(),
                    'CSP_HASH': csp_hash, 'DATA': payload, 'SCRIPT': script}
    return re.sub(r'@@(STYLE|CSP_HASH|DATA|SCRIPT)@@',
                  lambda match: replacements[match[1]], (assets / 'index.html').read_text())



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
