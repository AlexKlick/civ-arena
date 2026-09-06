"""Behavioral contracts for the offline retained-observation map."""
import hashlib
import json
import re
from copy import deepcopy

import pytest

from civ_arena.agents.llm.terrain_context import compact_entities, compact_terrain
from civ_arena.minimap import build, canonical, digest, main, render


def packet(pid=0, seq=10, turn=1, tile='0,0'):
    state = {'you': {'player_id': pid}, 'terrain': [{'coord': tile, 'terrain': 'PLAINS',
             'owner_id': pid, 'native_terrain': {'biome': 'TUNDRA', 'hills': False}}],
             'own_units': [{'unit_id': f'u{pid}:1', 'coord': tile, 'type': 'SCOUT'}],
             'own_cities': [{'city_id': f'c{pid}:1', 'coord': tile,
                             'production_queue': ['SETTLER']}],
             'visible_foreign_units': [], 'visible_foreign_cities': [], 'terrain_omitted': 12}
    return bind({'event': {'player_id': pid, 'seq': seq, 'turn': turn},
                 'projected_state': state})


def bind(p):
    context = '{}\nController context (projected observations, axial coordinates):\n' + canonical(
        p['projected_state'])
    p['request'] = {'user_context': context, 'context_chars': len(context),
                    'context_sha256': hashlib.sha256(context.encode()).hexdigest()}
    return p


def source(p):
    return {**p['event'], 'kind': 'HEARTBEAT', 'audit': 'strategy_request',
            'match_id': 'test-match', 'game_instance_id': 'test-instance',
            'strategy_payload_json': canonical(p['request'])}


def graph(pid=0, seq=5, turn=1):
    return {'kind': 'HEARTBEAT', 'audit': 'strategy_graph', 'seq': seq, 'turn': turn,
            'player_id': pid, 'match_id': 'test-match', 'game_instance_id': 'test-instance',
            'graph': {'decisions': [{'unit_id': f'u{pid}:1', 'origin': '0,0',
                                    'candidates': [{'dest': '0,1', 'probability': .5}],
                                    'selected': {'args': {'dest': '0,1'}}}]}}


def test_player_export_has_no_other_player_receipts_or_graph():
    p0, p1 = packet(), packet(1, 11, tile='900,900')
    g1 = graph(1, 6)
    g1['graph']['secret'] = 'private-other-seat'
    result = build([p1, p0], [source(p0), source(p1), graph(), g1], player=0)
    encoded = canonical(result)
    assert '900,900' not in encoded and 'private-other-seat' not in encoded
    assert len(result['seats']) == 1
    assert result['event_binding']['status'] == 'matched_context_hash'


def test_spectator_separates_disagreeing_receipts_without_world_claim():
    p0, p1 = packet(), packet(1, 11)
    p1['projected_state']['terrain'][0]['terrain'] = 'DESERT'
    result = build([p0, bind(p1)], [], spectator=True)
    assert result['scope'] == 'combined_observation_preview'
    assert [s['snapshots'][0]['terrain'][0]['observation']['owner_id']
            for s in result['seats']] == [0, 1]
    assert result['event_binding']['status'] == 'unverified'


def test_history_preserves_receipt_age_but_not_old_actors_or_future_terrain():
    p1, p2 = packet(), packet(seq=20, turn=2, tile='1,0')
    p2['projected_state']['own_units'] = []
    result = build([bind(p2), p1], [], player=0)
    a, b = result['seats'][0]['snapshots']
    assert len(a['terrain']) == 1 and len(b['terrain']) == 2
    assert b['terrain'][0]['receipt']['seq'] == 10
    assert all(row['kind'] != 'own_units' for row in b['actors'])
    assert b['packet_terrain_omitted'] == 12


def test_graph_never_reads_future_audit_and_preserves_original_receipt():
    p = packet()
    result = build([p], [source(p), graph(seq=4), graph(seq=12)], player=0)
    g = result['seats'][0]['snapshots'][0]['graph']
    assert g['receipt']['seq'] == 4
    assert g['receipt']['event_sha256'] == digest(graph(seq=4))


def test_deterministic_and_no_mutation():
    p, g = packet(), graph()
    original = deepcopy((p, g))
    a = build([p], [source(p), g], player=0)
    assert a == build([p], [g, source(p)], player=0)
    a['seats'][0]['snapshots'][0]['terrain'][0]['observation']['owner_id'] = 99
    assert (p, g) == original


def test_compact_rows_retain_null_false_zero_and_native_biome():
    p = packet()
    p['projected_state']['own_units'][0]['movement'] = 0
    p['projected_state'] = compact_entities(compact_terrain(p['projected_state']))
    result = build([bind(p)], [], player=0)['seats'][0]['snapshots'][0]
    assert result['actors'][0]['observation']['movement'] == 0
    assert result['terrain'][0]['observation']['native_terrain']['hills'] is False
    assert result['terrain'][0]['observation']['native_terrain']['biome'] == 'TUNDRA'


@pytest.mark.parametrize('kind', ['hash', 'state', 'player', 'duplicate', 'coord', 'backwards',
                                  'event_hash', 'mixed_runs', 'duplicate_events', 'graph_coord'])
def test_rejects_inconsistent_or_ambiguous_inputs(kind):
    p = packet()
    packets, events = [p], []
    if kind == 'hash':
        p['request']['context_sha256'] = 'bad'
    elif kind == 'state':
        p['projected_state']['terrain'][0]['terrain'] = 'DESERT'
    elif kind == 'player':
        p['event']['player_id'] = 1
    elif kind == 'duplicate':
        packets.append(deepcopy(p))
    elif kind == 'coord':
        p['projected_state']['terrain'][0]['coord'] = '01,0'
        bind(p)
    elif kind == 'backwards':
        packets.append(packet(seq=20, turn=2))
        packets.append(packet(seq=30, turn=1))
    elif kind == 'event_hash':
        events = [source(p)]
        events[0]['strategy_payload_json'] = '{}'
    elif kind == 'mixed_runs':
        events = [source(p), graph()]
        events[1]['game_instance_id'] = 'other'
    elif kind == 'duplicate_events':
        events = [source(p), source(p)]
    elif kind == 'graph_coord':
        events = [source(p), graph()]
        events[1]['graph']['decisions'][0]['origin'] = 'bogus'
    with pytest.raises(ValueError):
        build(packets, events, player=0)


@pytest.mark.parametrize('args', [{}, {'player': 0, 'spectator': True}, {'player': 2}])
def test_explicit_scope_required(args):
    with pytest.raises(ValueError):
        build([packet()], [], **args)


def test_html_custody_and_hostile_source_labels():
    p = packet()
    hostile = '</script><script>window.pwned=1</script>@@SCRIPT@@ & <img src=x>'
    p['projected_state']['own_units'][0]['type'] = hostile
    result = build([bind(p)], [], player=0)
    html = render(result)
    embedded = re.search(r'<script id="data" type="application/json">(.*?)</script>', html,
                         re.DOTALL)[1]
    assert json.loads(embedded) == result
    assert hostile not in html
    assert 'connect-src \'none\'' in html
    assert 'textContent' in html and 'innerHTML' not in html and 'fetch(' not in html
    result['scope'] = 'changed'
    with pytest.raises(ValueError, match='digest'):
        render(result)


def test_cli_refuses_overwrite_and_duplicate_json_before_mutating(tmp_path, monkeypatch):
    p, out = tmp_path / 'packet.json', tmp_path / 'preview.html'
    p.write_text(canonical(packet()))
    out.write_text('preserved')
    monkeypatch.setattr('sys.argv', ['minimap', '--packet', str(p), '--player', '0',
                                    '--output', str(out)])
    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == 2 and out.read_text() == 'preserved'
    out.unlink()
    p.write_text('{"x":1,"x":2}')
    with pytest.raises(SystemExit):
        main()
    assert not out.exists()


@pytest.mark.parametrize('coordinate', ['0,-1', '-2,0', '10000,10000'])
def test_negative_and_bounded_coordinate_projection_is_exact(coordinate):
    p = packet(tile=coordinate)
    s = build([p], [], player=0)['seats'][0]['snapshots'][0]
    assert s['terrain'][0]['observation']['coord'] == coordinate
    assert s['actors'][0]['observation']['coord'] == coordinate


def test_null_ownership_unknown_native_and_false_barbarian_stay_distinct():
    p = packet()
    tile = p['projected_state']['terrain'][0]
    tile['owner_id'] = None
    tile['native_terrain'] = None
    p['projected_state']['own_units'][0]['is_barbarian'] = False
    a = build([bind(p)], [], player=0)
    row = a['seats'][0]['snapshots'][0]
    assert row['terrain'][0]['observation']['owner_id'] is None
    assert row['terrain'][0]['observation']['native_terrain'] is None
    assert row['actors'][0]['observation']['is_barbarian'] is False
    del tile['owner_id']
    assert build([bind(p)], [], player=0)['digest'] != a['digest']
