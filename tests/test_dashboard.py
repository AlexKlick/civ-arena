import datetime as dt
import http.client
import json
import re
import threading
from pathlib import Path

import pytest

from civ_arena import dashboard as d

STAMP = '2026-09-05T01:00:00+00:00'
NOW = dt.datetime.fromisoformat(STAMP).timestamp()
ASSETS = Path(d.__file__).with_name('dashboard_static')
ERA_COUNTS = [11, 8, 7, 9, 8, 7, 8, 10]


def event(kind, **fields):
    return {'kind': kind, 'ts': STAMP, 'turn': 1, 'player_id': 0,
            'agent_id': 'seat0', **fields}


def write_run(root, events, summary=None, name='match-one', tail=b''):
    run = root / name
    run.mkdir()
    raw = b''.join((json.dumps(dict(row, seq=i)) + '\n').encode()
                   for i, row in enumerate(events)) + tail
    (run / 'events.jsonl').write_bytes(raw)
    if summary is not None:
        (run / 'summary.json').write_text(json.dumps(summary))
    return run


def start():
    return event('MATCH_START', config={'agents': [['seat0', 0, 'llm'], ['seat1', 1, 'llm']]})


def identity():
    return event('HEARTBEAT', audit='run_identity', identity={'config': {'agents': [
        {'agent_id': 'seat0', 'player_id': 0, 'llm': {'model_id': 'MiniMax-M3'}},
        {'agent_id': 'seat1', 'player_id': 1, 'llm': {'model_id': 'MiniMax-M3'}}]}})


def test_actual_notes_pair_fifo_by_agent_turn_and_tool_and_redact(tmp_path, monkeypatch):
    monkeypatch.setenv('EXAMPLE_API_KEY', 'very-private-value')
    events = [start(), identity(), event('LEASE_GRANT'),
              event('TOOL_CALL', tool='write_diary', args={'text': 'private very-private-value'}),
              event('TOOL_CALL', tool='write_diary', args={'text': 'second accepted'}),
              event('TOOL_RESULT', tool='write_diary', player_id=1, agent_id='seat1',
                    status='accepted'),
              event('TOOL_RESULT', tool='write_diary', status='rejected',
                    rejection={'password': 'sensitive', 'detail': 'very-private-value'}),
              event('TOOL_RESULT', tool='write_diary', status='accepted'),
              event('TOOL_CALL', tool='record_prediction',
                    args={'text': 'Grow in three turns', 'confidence': 65,
                          'nested': {'api_key': 'unknown-secret'}}),
              event('TOOL_RESULT', tool='record_prediction', status='accepted'),
              event('HEARTBEAT', audit='provider_request', agent='seat0', posts_sent=1)]
    write_run(tmp_path, events)
    result = d.DashboardStore(tmp_path).load('match-one', now=NOW)
    turn = result['turns'][0]
    assert [x['status'] for x in turn['calls']] == ['rejected', 'accepted', 'accepted']
    assert [x['text'] for x in turn['notes']] == ['second accepted', 'Grow in three turns']
    assert turn['notes'][1]['confidence'] == 65
    assert result['agents'][0]['model'] == 'MiniMax-M3'
    assert result['metrics']['requests'] == turn['requests'] == 1
    encoded = json.dumps(result)
    assert 'very-private-value' not in encoded and 'unknown-secret' not in encoded
    assert 'sensitive' not in encoded
    assert any('Unmatched TOOL_RESULT' in warning for warning in result['warnings'])


def test_partial_tail_recent_activity_and_stale_activity_are_distinct(tmp_path):
    write_run(tmp_path, [start()], tail=b'{"seq":1,"kind":')
    store = d.DashboardStore(tmp_path)
    assert store.load('match-one', now=NOW + 1)['status'] == 'running'
    result = store.load('match-one', now=NOW + 31)
    assert result['status'] == 'stalled'
    assert any('Partial trailing' in warning for warning in result['warnings'])
    assert any('not process-liveness proof' in warning for warning in result['warnings'])


@pytest.mark.parametrize('tail', [b'{bad}\n{}\n', b'{bad}\n', b'{"seq":7}\n'])
def test_malformed_interior_or_sequence_never_becomes_running(tmp_path, tail):
    write_run(tmp_path, [start()], tail=tail)
    result = d.DashboardStore(tmp_path).load('match-one', now=NOW)
    assert result['status'] == 'incomplete'


def test_terminal_summary_is_authoritative_and_failure_is_visible(tmp_path, monkeypatch):
    monkeypatch.setenv('TEST_SECRET', 'secret-in-exception')
    summary = {'clean': False, 'aborted': 'watchdog secret-in-exception', 'completed_rounds': 1}
    events = [start(), identity()]
    for pid in (0, 1):
        events += [event('HEARTBEAT', audit='completed_seat_turn', row={
            'turn': 1, 'player': pid, 'agent': f'seat{pid}',
            'lease_released': True, 'elapsed_s': 2.5})]
    events += [event('VIOLATION'), event('MATCH_END', summary=summary)]
    run = write_run(tmp_path, events, summary)
    result = d.DashboardStore(tmp_path).load('match-one', now=NOW)
    assert result['status'] == 'aborted'
    assert result['metrics'] == {'completed_rounds': 1, 'completed_seat_turns': 2,
                                 'requests': 0, 'violations': 1,
                                 'spectator_snapshots': 0, 'human_turns': 0}
    assert any('watchdog' in warning for warning in result['warnings'])
    assert 'secret-in-exception' not in json.dumps(result)
    (run / 'summary.json').write_text('{"clean":true}')
    assert d.DashboardStore(tmp_path).load('match-one', now=NOW)['status'] == 'incomplete'


def test_live_turn_end_waits_for_driver_completion_audit(tmp_path):
    write_run(tmp_path, [start(), identity(), event('LEASE_GRANT'), event('TURN_END')])
    result = d.DashboardStore(tmp_path).load('match-one', now=NOW)
    assert result['metrics']['completed_seat_turns'] == 0
    assert result['turns'][0]['status'] == 'active'


def test_out_of_order_completion_never_counts_a_clean_round(tmp_path):
    write_run(tmp_path, [start(), identity(), event('HEARTBEAT', audit='completed_seat_turn',
        row={'turn': 1, 'player': 1, 'agent': 'seat1', 'lease_released': True})])
    result = d.DashboardStore(tmp_path).load('match-one', now=NOW)
    assert result['metrics']['completed_rounds'] == 0
    assert any('order' in warning for warning in result['warnings'])
    assert result['status'] == 'incomplete'


@pytest.mark.parametrize('players', [[0, 0], [1, 0], [0]])
def test_impossible_clean_terminal_is_incomplete(tmp_path, players):
    summary = {'clean': True, 'aborted': None, 'completed_rounds': 1}
    events = [start(), identity()]
    events += [event('HEARTBEAT', audit='completed_seat_turn', row={
        'turn': 1, 'player': pid, 'agent': f'seat{pid}', 'lease_released': True})
        for pid in players]
    events.append(event('MATCH_END', summary=summary))
    write_run(tmp_path, events, summary)
    assert d.DashboardStore(tmp_path).load('match-one', now=NOW)['status'] == 'incomplete'


def test_log_and_response_bounds_are_explicit(tmp_path, monkeypatch):
    write_run(tmp_path, [start(), event('TOOL_CALL', tool='get_map', args={}),
                         event('TOOL_RESULT', tool='get_map', status='accepted',
                               observed={'long': 'x' * 20_000})])
    monkeypatch.setattr(d, 'MAX_RESPONSE_BYTES', 2500)
    result = d.DashboardStore(tmp_path).load('match-one', now=NOW)
    assert len(json.dumps(result).encode()) <= 2500
    assert any('limit' in warning for warning in result['warnings'])
    monkeypatch.setattr(d, 'MAX_LOG_BYTES', 20)
    assert d.DashboardStore(tmp_path).load('match-one', now=NOW)['status'] == 'incomplete'


def test_symlinks_and_non_regular_artifacts_do_not_escape_root(tmp_path):
    root, outside = tmp_path / 'runs', tmp_path / 'outside'
    root.mkdir()
    outside.mkdir()
    run = write_run(outside, [start()])
    (root / 'linked').symlink_to(run, target_is_directory=True)
    store = d.DashboardStore(root)
    with pytest.raises(d.InvalidRun):
        store.load('linked')
    local = root / 'local'
    local.mkdir()
    (local / 'events.jsonl').symlink_to(run / 'events.jsonl')
    assert store.load('local')['status'] == 'incomplete'
    for name in ('../outside', '/tmp', 'a/b', '..', '.', 'a\\b'):
        with pytest.raises(d.InvalidRun):
            store.load(name)


def test_real_http_is_loopback_read_only_and_serves_only_explicit_assets(tmp_path):
    root, assets = tmp_path / 'runs', tmp_path / 'assets'
    root.mkdir()
    assets.mkdir()
    run = write_run(root, [start()])
    before = (run / 'events.jsonl').read_bytes()
    (assets / 'index.html').write_text('<html>dashboard</html>')
    (assets / 'compare-core.js').write_text("'use strict';\n")
    (assets / 'private.txt').write_text('must-not-serve')
    server = d.create_server(root, port=0, static_root=assets)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        connection = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=3)
        for method, path, status in [('GET', '/', 200), ('HEAD', '/', 200),
                                     ('GET', '/compare-core.js', 200),
                                     ('GET', '/api/runs', 200),
                                     ('GET', '/api/run?id=match-one', 200),
                                     ('GET', '/api/run?id=../outside', 404),
                                     ('GET', '/api/run?id=%2Ftmp', 404),
                                     ('GET', '/api/run?id=a&id=b', 404),
                                     ('GET', '/private.txt', 404),
                                     ('GET', '/events.jsonl', 404),
                                     ('POST', '/api/run?id=match-one', 405),
                                     ('DELETE', '/api/run?id=match-one', 405)]:
            connection.request(method, path)
            response = connection.getresponse()
            body = response.read()
            assert response.status == status, (method, path, body)
            assert response.getheader('Access-Control-Allow-Origin') is None
            if method == 'HEAD':
                assert body == b''
        connection.request('GET', '/api/runs', headers={'Host': 'attacker.invalid'})
        response = connection.getresponse()
        assert response.status == 403
        response.read()
        connection.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
    assert (run / 'events.jsonl').read_bytes() == before
    assert sorted(p.name for p in run.iterdir()) == ['events.jsonl']
    with pytest.raises(ValueError, match='loopback'):
        d.create_server(root, host='0.0.0.0', port=0)


def get(root, paths, static_root=None):
    """Serve one real HTTP request per path and return (status, body) pairs."""
    server = d.create_server(root, port=0, static_root=static_root)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    results = []
    try:
        connection = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=3)
        for path in paths:
            connection.request('GET', path)
            response = connection.getresponse()
            results.append((response.status, response.read()))
        connection.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
    return results


def test_tech_tree_route_serves_the_validated_catalog_asset_and_not_the_file(tmp_path):
    write_run(tmp_path, [start()])
    (route, body), (asset, _) = get(tmp_path, ['/api/tech-tree', '/tech-tree.json'])
    assert (route, asset) == (200, 404)
    tree = json.loads(body)
    assert tree['version'] == 1 and tree['scope'] == 'base_source_catalog'
    assert tree['effective_ruleset'] == 'unverified'
    assert tree['group_semantics'] == 'unverified'
    assert tree['source']['name'] == 'base-source-catalog.json'
    assert len(tree['nodes']) == 68 and len(tree['edges']) == 90
    assert [sum(1 for node in tree['nodes'] if node['era'] == era)
            for era in tree['eras']] == ERA_COUNTS
    order = [(tree['eras'].index(node['era']), node['row'], node['id']) for node in tree['nodes']]
    assert order == sorted(order)
    assert tree['edges'] == sorted(tree['edges'])


@pytest.mark.parametrize('defect', ['unknown_edge', 'backward_edge', 'version', 'bool_version',
                                    'extra_field', 'extra_top_key', 'oversize', 'symlink'])
def test_unverifiable_tech_tree_assets_are_withheld(tmp_path, defect):
    assets, root = tmp_path / 'assets', tmp_path / 'runs'
    assets.mkdir()
    root.mkdir()
    write_run(root, [start()])
    tree = json.loads((ASSETS / 'tech-tree.json').read_text())
    if defect == 'unknown_edge':
        tree['edges'].append([tree['nodes'][0]['id'], 'NOT_A_TECHNOLOGY'])
    elif defect == 'backward_edge':
        tree['edges'].append([tree['nodes'][0]['id'], tree['nodes'][-1]['id']])
    elif defect == 'version':
        tree['version'] = 2
    elif defect == 'bool_version':
        # True == 1 in Python; a flag is not a version number.
        tree['version'] = True
    elif defect == 'extra_field':
        tree['nodes'][0]['note'] = 'unmodelled'
    elif defect == 'extra_top_key':
        tree['note'] = 'unmodelled'
    body = json.dumps(tree) + (' ' * (d.MAX_TECH_TREE_BYTES + 1) if defect == 'oversize' else '')
    if defect == 'symlink':
        (tmp_path / 'outside.json').write_text(body)
        (assets / 'tech-tree.json').symlink_to(tmp_path / 'outside.json')
    else:
        (assets / 'tech-tree.json').write_text(body)
    status, payload = get(root, ['/api/tech-tree'], static_root=assets)[0]
    assert (status, payload) == (404, b'{"error":"resource unavailable"}')


def test_the_comparison_core_module_reaches_neither_the_page_nor_the_network():
    code = (ASSETS / 'compare-core.js').read_text()
    for forbidden in ('document', 'fetch(', 'XMLHttpRequest', 'window.civArena.'):
        assert forbidden not in code, forbidden
    assert 'window.civArenaCompareCore = Core' in code


def test_the_journal_core_module_reaches_neither_the_page_nor_the_network():
    code = (ASSETS / 'journal-core.js').read_text()
    for forbidden in ('document', 'fetch(', 'XMLHttpRequest', 'window.civArena.'):
        assert forbidden not in code, forbidden
    assert 'window.civArenaJournalCore = Core' in code


def test_static_scripts_only_reach_same_origin_api_paths():
    scripts = sorted(ASSETS.glob('*.js'))
    assert len(scripts) >= 3
    inspected = []
    for script in scripts:
        code = script.read_text()
        for forbidden in ('innerHTML', 'eval(', 'document.write'):
            assert forbidden not in code, (script.name, forbidden)
        targets = []
        for match in re.finditer(r'fetch\(\s*([\'"`][^\'"`\n]*|[A-Za-z_$][\w$]*)', code):
            head = match[1]
            if head[0] in '\'"`':
                targets.append(head[1:])
                continue
            # A variable target is only allowed from a wrapper whose every call
            # site passes a literal API path.
            wrapper = re.search(rf'function\s+([A-Za-z_$][\w$]*)\(\s*{head}\s*\)', code)
            assert wrapper, (script.name, head)
            for call in re.finditer(rf'(?<!function ){wrapper[1]}\(\s*(.)', code):
                assert call[1] in '\'"`', (script.name, wrapper[1])
            targets += re.findall(rf'(?<!function ){wrapper[1]}\(\s*[\'"`]([^\'"`\n]*)', code)
        # A script that requests anything must yield inspectable targets; the
        # pure core module requests nothing at all.
        assert targets or 'fetch(' not in code, script.name
        inspected += targets
        for target in targets:
            assert target.startswith('/api/'), (script.name, target)
    assert inspected


def test_run_index_reuses_unchanged_parse_and_invalidates_on_append(tmp_path, monkeypatch):
    run = write_run(tmp_path, [start()])
    store = d.DashboardStore(tmp_path)
    original, calls = store.load, []
    def load(*args, **kwargs):
        calls.append(args)
        return original(*args, **kwargs)
    monkeypatch.setattr(store, 'load', load)
    assert len(store.list_runs()['runs']) == 1
    assert len(store.list_runs()['runs']) == 1
    assert len(calls) == 1
    with (run / 'events.jsonl').open('a') as stream:
        stream.write(json.dumps(dict(event('HEARTBEAT'), seq=1)) + '\n')
    store.list_runs()
    assert len(calls) == 2


@pytest.mark.parametrize('config', [{'agents': None}, {'agents': 5}, None])
def test_malformed_run_payload_does_not_poison_healthy_inventory(tmp_path, config):
    write_run(tmp_path, [start()], name='healthy')
    write_run(tmp_path, [event('MATCH_START', config=config)], name='malformed')
    store = d.DashboardStore(tmp_path)
    result = store.load('malformed', now=NOW)
    assert result['status'] == 'incomplete'
    assert any('Malformed' in warning for warning in result['warnings'])
    inventory = {row['id']: row for row in store.list_runs()['runs']}
    assert inventory['malformed']['status'] == 'incomplete'
    assert 'healthy' in inventory


def test_strategy_audit_keeps_seats_distinct_and_does_not_count_plans_as_actions(
        tmp_path, monkeypatch):
    monkeypatch.setenv('EXAMPLE_API_KEY', 'strategy-private-value')
    graph = {'decisions': [{'unit_id': 'u0:7', 'candidates': [
        {'dest': {'q': 1, 'r': 2}, 'probability': 0.75, 'score': 3.5}],
        'selected': {'action': 'move_unit', 'args': {'unit_id': 'u0:7'}}}],
        'execution': [{'unit_id': 'u0:7', 'outcome': 'submitted_observation_unchanged'}]}
    events = [start(), identity(), event('LEASE_GRANT'),
              event('HEARTBEAT', audit='strategy_execution', source='model',
                    last_decision_turn=1, directive={'note': 'strategy-private-value'}),
              event('HEARTBEAT', audit='strategy_graph', graph=graph),
              event('HEARTBEAT', audit='strategy_execution', source='autopilot',
                    player_id=1, agent_id='seat1', last_decision_turn=1, directive={})]
    write_run(tmp_path, events)
    result = d.DashboardStore(tmp_path).load('match-one', now=NOW)
    first, second = result['turns']
    assert first['strategy']['source'] == 'model'
    assert second['strategy']['source'] == 'autopilot'
    assert first['scouting_graph'] == graph
    assert second['scouting_graph'] is None
    assert first['calls'] == [] and first['requests'] == 0
    assert result['metrics']['completed_seat_turns'] == 0
    assert 'strategy-private-value' not in json.dumps(result)


def test_malformed_strategy_graph_is_visible_incomplete_evidence(tmp_path):
    write_run(tmp_path, [start(), event('HEARTBEAT', audit='strategy_graph', graph='invalid')])
    result = d.DashboardStore(tmp_path).load('match-one', now=NOW)
    assert result['status'] == 'incomplete'
    assert 'Scouting audit has no graph object.' in result['warnings']


def test_encoded_strategy_payload_preserves_probabilities_and_outer_seat_identity(tmp_path):
    payload = {'agent_id': 'seat1', 'player_id': 1, 'turn': 99,
               'graph': {'decisions': [{'candidates': [{'probability': 0.75}]}]}}
    write_run(tmp_path, [start(), event('HEARTBEAT', audit='strategy_graph',
                                      strategy_payload_json=json.dumps(payload))])
    result = d.DashboardStore(tmp_path).load('match-one', now=NOW)
    turn = result['turns'][0]
    assert (turn['agent_id'], turn['player_id'], turn['turn']) == ('seat0', 0, 1)
    assert turn['scouting_graph']['decisions'][0]['candidates'][0]['probability'] == 0.75


@pytest.mark.parametrize('payload', ['{bad', '[]', '{"graph":NaN}', 7])
def test_bad_encoded_strategy_payload_is_incomplete(tmp_path, payload):
    write_run(tmp_path, [start(), event('HEARTBEAT', audit='strategy_graph',
                                      strategy_payload_json=payload)])
    result = d.DashboardStore(tmp_path).load('match-one', now=NOW)
    assert result['status'] == 'incomplete'
    assert 'Strategy payload JSON is malformed.' in result['warnings']
