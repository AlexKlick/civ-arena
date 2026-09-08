"""Behavioral contracts for the match room's comparison projections."""
import json

import pytest

from civ_arena import dashboard as d
from civ_arena import dashboard_compare as c
from test_dashboard import NOW, STAMP, event, identity, start, write_run
from test_minimap import bind, source

RESEARCH_NOTE = ("Research sets come from each seat's own retained request packets. Seats are "
                 'asynchronous, so the two as-of turns can differ. Retained data, not live.')
TIMELINE_NOTE = ('Per-turn rows come from retained audits, tool results and request packets. '
                 'Gaps are unsupplied values, never interpolated. Provider requests are '
                 'recorded POST attempts, not decisions.')
CATALOG_NOTE = ('Tree layout comes from the base source catalog (scope base_source_catalog, '
                'effective ruleset unverified, group semantics unverified). It is not the '
                'effective ruleset of this match.')


def packet_event(pid, turn, seq, researched, researching, options=None,
                 options_source='observed', gold=None, units=0, cities=0, civ_name=None,
                 projected_id=None):
    """A bound HEARTBEAT strategy_request, built through the map packet helpers.

    `researched=None` builds a packet that never supplied a researched list;
    `projected_id` overrides the seat the projected state claims to be.
    """
    you = {'civ_name': civ_name or f'CIVILIZATION_P{pid}', 'gold': gold,
           'player_id': pid if projected_id is None else projected_id,
           'researching': researching}
    if researched is not None:
        you['researched'] = list(researched)
    state = {'you': you,
             'own_units': [{'unit_id': f'u{pid}:{index}'} for index in range(units)],
             'own_cities': [{'city_id': f'c{pid}:{index}'} for index in range(cities)],
             'option_sources': {'production': {}, 'research': options_source},
             'public': {'players': [], 'turn': turn}}
    if options is not None:
        state['research_options'] = [{'tech_id': tech, 'cost': cost} for tech, cost in options]
    packet = bind({'event': {'player_id': pid, 'seq': seq, 'turn': turn},
                   'projected_state': state})
    return dict(source(packet), agent_id=f'seat{pid}', ts=STAMP)


def retune(packet, **payload_changes):
    """Rewrite a packet's recorded custody fields to simulate a broken packet."""
    payload = json.loads(packet['strategy_payload_json'])
    payload.update(payload_changes)
    return dict(packet, strategy_payload_json=json.dumps(payload))


def audit(turn, pid, **row):
    return event('HEARTBEAT', audit='completed_seat_turn', turn=turn, player_id=pid,
                 agent_id=f'seat{pid}', row={'turn': turn, 'player': pid, 'agent': f'seat{pid}',
                                             'lease_released': True, **row})


def lease(turn, pid):
    return event('LEASE_GRANT', turn=turn, player_id=pid, agent_id=f'seat{pid}')


def load(tmp_path, events, now=NOW):
    write_run(tmp_path, events)
    return d.DashboardStore(tmp_path).load('match-one', now=now)


def seat_row(result, pid, turn):
    seat = next(s for s in result['timeline']['seats'] if s['player_id'] == pid)
    return next(row for row in seat['rows'] if row['turn'] == turn)


def test_research_sets_and_diff_come_from_each_seats_own_latest_packet(tmp_path):
    result = load(tmp_path, [
        start(), identity(),
        packet_event(0, 1, 10, [], 'POTTERY', options=[('POTTERY', 25), ('MINING', 25)]),
        packet_event(1, 1, 11, [], 'MINING'),
        packet_event(1, 2, 12, ['MINING'], None),
        packet_event(0, 3, 13, ['POTTERY'], 'MINING'),
        packet_event(0, 3, 14, ['POTTERY', 'MINING'], 'WRITING', gold=42)])
    research = result['research']
    first, second = research['seats']
    assert (first['player_id'], first['agent_id'], first['packets']) == (0, 'seat0', 3)
    assert first['civ_name'] == 'CIVILIZATION_P0'
    # The repair attempt with the higher recorded sequence is the seat's state.
    assert first['latest']['turn'] == 3 and first['latest']['gold'] == 42
    assert first['latest']['researched'] == ['MINING', 'POTTERY']
    assert first['latest']['researched_count'] == 2
    assert first['latest']['researching'] == 'WRITING'
    assert first['latest']['research_options'] == []
    assert first['latest']['options_source'] == 'observed'
    assert second['latest']['turn'] == 2 and second['packets'] == 2
    assert research['diff']['shared'] == ['MINING']
    assert research['diff']['only'] == {'0': ['POTTERY'], '1': []}
    assert research['diff']['counts'] == {'shared': 1, 'only': {'0': 1, '1': 0}}
    assert [row['turn'] for row in research['diff']['as_of']] == [3, 2]
    assert research['incomplete'] is False
    assert research['note'] == RESEARCH_NOTE
    assert research['catalog'] == {'route': '/api/tech-tree', 'note': CATALOG_NOTE}
    assert result['timeline']['note'] == TIMELINE_NOTE


def test_first_packet_options_are_kept_and_unobserved_options_stay_empty(tmp_path):
    result = load(tmp_path, [
        start(), identity(),
        packet_event(0, 1, 10, [], None, options=[('POTTERY', 25), ('MINING', None)]),
        packet_event(1, 1, 11, [], None, options=None,
                     options_source='not_requested_active_choice')])
    first, second = result['research']['seats']
    assert first['latest']['research_options'] == [{'tech_id': 'POTTERY', 'cost': 25},
                                                   {'tech_id': 'MINING', 'cost': None}]
    assert second['latest']['research_options'] == []
    assert second['latest']['options_source'] == 'not_requested_active_choice'


def test_history_records_only_changed_sets_and_reports_a_visible_shrink(tmp_path):
    result = load(tmp_path, [
        start(), identity(),
        packet_event(0, 1, 10, [], None),
        packet_event(0, 2, 11, ['POTTERY'], None),
        packet_event(0, 3, 12, ['POTTERY'], None),
        packet_event(0, 4, 13, ['POTTERY', 'MINING'], None),
        packet_event(0, 5, 14, ['POTTERY'], None)])
    history = result['research']['seats'][0]['history']
    assert [row['turn'] for row in history] == [1, 2, 4, 5]
    assert [row['added'] for row in history] == [[], ['POTTERY'], ['MINING'], []]
    assert [row['removed'] for row in history] == [[], [], [], ['MINING']]
    assert [row['count'] for row in history] == [0, 1, 2, 1]
    assert 'Researched set shrank between packets.' in result['warnings']
    assert c.researched_at(history, 1) == set()
    assert c.researched_at(history, 3) == {'POTTERY'}
    assert c.researched_at(history, 4) == {'POTTERY', 'MINING'}
    assert c.researched_at(history, 9) == {'POTTERY'}


def test_research_names_are_redacted_bounded_and_never_silently_dropped(tmp_path, monkeypatch):
    monkeypatch.setenv('EXAMPLE_API_KEY', 'MAP_TEST_SECRET')
    names = [f'TECH_{index:03d}' for index in range(129)]
    result = load(tmp_path, [
        start(), identity(),
        packet_event(0, 1, 10, names, 'PROJECT_MAP_TEST_SECRET_NAME'),
        packet_event(1, 1, 11, ['POTTERY', 7, None, {'tech': 'x'}], 'A' * 60)])
    first, second = result['research']['seats']
    assert len(first['latest']['researched']) == 128
    assert first['latest']['researched_count'] == 129
    assert len(first['history'][0]['added']) == 128
    assert first['history'][0]['count'] == 129
    assert 'Researched name limit reached; later names omitted.' in result['warnings']
    assert first['latest']['researching'] == 'PROJECT_[redacted]_NAME'
    assert 'MAP_TEST_SECRET' not in json.dumps(result)
    assert second['latest']['researched'] == ['POTTERY']
    assert second['latest']['researched_count'] == 1
    assert 'Non-text entries dropped from a researched list.' in result['warnings']
    assert second['latest']['researching'] == 'A' * 40


def test_a_packet_without_a_researched_list_is_not_a_research_observation(tmp_path):
    result = load(tmp_path, [
        start(), identity(),
        packet_event(0, 1, 10, ['POTTERY'], 'MINING'),
        packet_event(0, 2, 11, None, 'WRITING'),
        packet_event(1, 1, 12, ['POTTERY'], None)])
    seat = result['research']['seats'][0]
    # The packet is counted, but it removes nothing and replaces nothing.
    assert seat['packets'] == 2
    assert seat['latest']['turn'] == 1
    assert seat['latest']['researched'] == ['POTTERY']
    assert seat['latest']['researched_count'] == 1
    assert seat['latest']['researching'] == 'MINING'
    assert [row['turn'] for row in seat['history']] == [1]
    assert [row['removed'] for row in seat['history']] == [[]]
    assert 'Research packet has no researched list.' in result['warnings']
    assert 'Researched set shrank between packets.' not in result['warnings']
    assert result['research']['diff']['counts'] == {'shared': 1, 'only': {'0': 0, '1': 0}}


def test_a_boolean_projected_seat_identity_never_binds_a_packet(tmp_path):
    # False == 0 in Python; only a real int may claim seat 0.
    result = load(tmp_path, [start(), identity(),
                             packet_event(0, 1, 10, ['POTTERY'], None, projected_id=False)])
    assert result['research']['incomplete'] is True
    assert 'Research packet could not be bound.' in result['warnings']
    assert result['research']['seats'][0]['packets'] == 0
    assert result['research']['seats'][0]['latest'] is None


def test_capped_history_keeps_a_baseline_row_instead_of_dropping_the_older_set(tmp_path):
    events = [start(), identity()]
    for index in range(170):
        events.append(packet_event(0, index + 1, 100 + index,
                                   [f'TECH_{item:03d}' for item in range(index + 1)], None))
    result = load(tmp_path, events)
    history = result['research']['seats'][0]['history']
    assert len(history) == c.MAX_HISTORY == 160
    baseline = history[0]
    assert baseline['baseline'] is True
    assert baseline['turn'] == 11 and baseline['seq'] == 12 and baseline['count'] == 11
    assert baseline['added'] == [f'TECH_{item:03d}' for item in range(11)]
    assert baseline['removed'] == []
    assert all(row['baseline'] is False for row in history[1:])
    assert [row['turn'] for row in history[1:3]] == [12, 13]
    assert c.researched_at(history, 11) == set(baseline['added'])
    assert c.researched_at(history, 13) == {f'TECH_{item:03d}' for item in range(13)}
    assert 'Research history limit reached; older rows omitted.' in result['warnings']


def test_folded_baseline_names_are_never_a_turns_recorded_acquisition(tmp_path):
    events = [start(), identity()]
    for index in range(170):
        events.append(lease(index + 1, 0))
        events.append(packet_event(0, index + 1, 100 + index,
                                   [f'TECH_{item:03d}' for item in range(index + 1)], None))
    result = load(tmp_path, events)
    history = result['research']['seats'][0]['history']
    baseline = history[0]
    assert baseline['baseline'] is True and len(baseline['added']) == 11
    rows = {row['turn']: row['tech_added'] for row in result['timeline']['seats'][0]['rows']}
    # The folded names were acquired before this turn, not during it.
    assert rows[baseline['turn']] == []
    assert not any(name in rows[baseline['turn']] for name in baseline['added'])
    assert rows[12] == ['TECH_011'] and rows[170] == ['TECH_169']
    assert sum(len(added) for added in rows.values()) == sum(
        len(row['added']) for row in history if row['baseline'] is False) == 159


def test_unbound_packets_mark_research_incomplete_without_dirtying_the_journal(tmp_path):
    good = packet_event(0, 1, 10, ['POTTERY'], None)
    bad_digest = retune(packet_event(0, 2, 11, ['POTTERY', 'MINING'], None),
                        context_sha256='0' * 64)
    wrong_seat = dict(packet_event(1, 2, 12, ['SAILING'], None),
                      player_id=0, agent_id='seat0')
    result = load(tmp_path, [start(), identity(), lease(1, 0), good, bad_digest, wrong_seat])
    assert result['research']['incomplete'] is True
    assert 'Research packet could not be bound.' in result['warnings']
    # A broken request packet is not evidence that the journal is broken.
    assert result['status'] == 'running'
    assert not any('Malformed' in warning for warning in result['warnings'])
    assert [turn['turn'] for turn in result['turns']] == [1]
    assert result['research']['seats'][0]['latest']['researched'] == ['POTTERY']
    assert result['research']['seats'][0]['packets'] == 1


def test_timeline_prefers_audits_then_tools_then_packets_and_keeps_gaps_null(tmp_path):
    events = [start(), identity(), lease(1, 0),
              event('TOOL_CALL', tool='get_overview', args={}),
              event('TOOL_RESULT', tool='get_overview', status='accepted',
                    observed={'gold': 7, 'techs': 2, 'researching': 'WRITING'}),
              event('TOOL_CALL', tool='get_units', args={}),
              event('TOOL_RESULT', tool='get_units', status='accepted',
                    observed={'own_units': 3, 'foreign_units': []}),
              event('TOOL_CALL', tool='get_cities', args={}),
              event('TOOL_RESULT', tool='get_cities', status='rejected',
                    observed={'own_cities': 99}),
              packet_event(0, 1, 20, ['POTTERY'], 'MINING', gold=1, units=1, cities=1),
              audit(1, 0, elapsed_s=2.5, allowed_mutations=4, violations=1),
              lease(2, 1),
              packet_event(1, 2, 21, ['POTTERY', 'MINING'], 'SAILING', gold=9, units=5, cities=2),
              lease(3, 1)]
    result = load(tmp_path, events)
    observed = seat_row(result, 0, 1)
    assert observed['gold'] == 7 and observed['sources']['gold'] == 'tool'
    assert observed['techs'] == 2 and observed['sources']['techs'] == 'tool'
    assert observed['researching'] == 'WRITING'
    assert observed['units'] == 3 and observed['sources']['units'] == 'tool'
    # A rejected observation is not an observation; the packet is the fallback.
    assert observed['cities'] == 1 and observed['sources']['cities'] == 'packet'
    assert observed['elapsed_s'] == 2.5 and observed['sources']['elapsed_s'] == 'audit'
    assert observed['allowed_mutations'] == 4 and observed['violations'] == 1
    assert observed['sources']['allowed_mutations'] == 'audit'
    assert observed['calls'] == 3 and observed['sources']['calls'] == 'events'
    assert observed['requests'] == 0 and observed['sources']['requests'] == 'events'
    assert observed['tech_added'] == ['POTTERY']
    assert observed['status'] == 'completed'
    packet_row = seat_row(result, 1, 2)
    assert packet_row['gold'] == 9 and packet_row['sources']['gold'] == 'packet'
    assert packet_row['techs'] == 2 and packet_row['sources']['techs'] == 'packet'
    assert packet_row['units'] == 5 and packet_row['cities'] == 2
    assert packet_row['researching'] == 'SAILING'
    assert packet_row['sources']['researching'] == 'packet'
    assert packet_row['tech_added'] == ['MINING', 'POTTERY']
    empty = seat_row(result, 1, 3)
    for field in ('gold', 'techs', 'researching', 'units', 'cities', 'elapsed_s',
                  'allowed_mutations', 'violations'):
        assert empty[field] is None and empty['sources'][field] is None, field
    assert empty['calls'] == 0 and empty['sources']['calls'] == 'events'
    assert result['timeline']['turns'] == [1, 2, 3]
    assert [row['key'] for row in result['timeline']['series']] == [
        'elapsed_s', 'requests', 'calls', 'allowed_mutations', 'gold', 'techs', 'units', 'cities']
    assert [row['label'] for row in result['timeline']['series']][:2] == [
        'Seat turn time (s)', 'Provider requests (recorded POST attempts)']


def test_timeline_counts_every_recorded_tool_call_beyond_the_display_slice(tmp_path):
    events = [start(), identity()]
    for _ in range(130):
        events.append(event('TOOL_CALL', tool='get_visible_map', args={}))
    for _ in range(130):
        events.append(event('TOOL_RESULT', tool='get_visible_map', status='accepted'))
    result = load(tmp_path, events)
    assert len(result['turns'][0]['calls']) == d.MAX_CALLS == 128
    assert seat_row(result, 0, 1)['calls'] == 130


def test_violation_marks_and_row_counts_are_bounded_with_a_visible_warning(tmp_path):
    events = [start(), identity()]
    for turn in range(1, 71):
        events.append(event('VIOLATION', turn=turn, player_id=0, watchdog={
            'kind': 'UNAUTHORIZED_ACTION', 'detail': f'mutation {turn}', 'mutations': []}))
    result = load(tmp_path, events)
    marks = result['timeline']['violations']
    assert len(marks) == 64
    assert marks[0]['turn'] == 7 and marks[-1]['turn'] == 70
    assert marks[0]['kind'] == 'UNAUTHORIZED_ACTION'
    assert marks[0]['detail'] == 'mutation 7'
    assert marks[0]['player_id'] == 0 and marks[0]['seq'] == 8
    assert 'Violation mark limit reached; older marks omitted.' in result['warnings']


def test_timeline_rows_are_bounded_per_seat(tmp_path):
    events = [start(), identity()] + [lease(turn, 0) for turn in range(1, 251)]
    result = load(tmp_path, events)
    rows = result['timeline']['seats'][0]['rows']
    assert len(rows) == 240
    assert rows[0]['turn'] == 11 and rows[-1]['turn'] == 250
    assert 'Timeline row limit reached; older rows omitted.' in result['warnings']


def strategy(turn, pid, directive, source_name='model', reasons=None):
    return event('HEARTBEAT', audit='strategy_execution', turn=turn, player_id=pid,
                 agent_id=f'seat{pid}', strategy_payload_json=json.dumps({
                     'source': source_name, 'reasons': reasons or [], 'last_decision_turn': turn,
                     'directive': directive, 'cadence': {}, 'persistence': {}, 'seed': 1,
                     'opening_frozen_unit_ids': []}))


def directive(policy='frontier', research=('POTTERY',), overrides=()):
    return {'production_preferences': ['WARRIOR'], 'research_preferences': list(research),
            'scouting': {'policy': policy, 'selection': 'weighted', 'temperature': 1.0,
                         'unit_types': ['SCOUT'], 'weights': {'frontier': 1}},
            'tactical_overrides': list(overrides), 'version': 1}


def test_strategy_delta_reports_first_record_then_stability_then_changed_fields(tmp_path):
    result = load(tmp_path, [
        start(), identity(),
        strategy(1, 0, directive()),
        strategy(2, 0, directive()),
        strategy(3, 0, directive(policy='defensive', research=('MINING',),
                                 overrides=({'unit_id': 'u0:1'}, {'unit_id': 'u0:2'})),
                 reasons=['new_visible_contact'])])
    deltas = {turn['turn']: turn['strategy_delta'] for turn in result['turns']}
    assert deltas[1]['changed'] is None and deltas[1]['previous_turn'] is None
    assert deltas[1]['changed_fields'] == [] and deltas[1]['source'] == 'model'
    assert deltas[1]['last_decision_turn'] == 1
    assert deltas[2]['changed'] is False and deltas[2]['previous_turn'] == 1
    assert deltas[2]['changed_fields'] == []
    assert deltas[3]['changed'] is True and deltas[3]['previous_turn'] == 2
    assert deltas[3]['changed_fields'] == ['research_preferences', 'scouting.policy']
    assert deltas[3]['tactical_overrides'] == 2
    assert deltas[3]['reasons'] == ['new_visible_contact']


def test_directive_field_names_are_redacted_before_they_are_displayed(tmp_path, monkeypatch):
    monkeypatch.setenv('EXAMPLE_API_KEY', 'DIRECTIVE_SECRET')
    before = directive()
    before['DIRECTIVE_SECRET_flag'] = 1
    before['scouting']['DIRECTIVE_SECRET_weight'] = 1
    after = json.loads(json.dumps(before))
    after['DIRECTIVE_SECRET_flag'] = 2
    after['scouting']['DIRECTIVE_SECRET_weight'] = 2
    result = load(tmp_path, [start(), identity(),
                             strategy(1, 0, before), strategy(2, 0, after)])
    delta = next(turn['strategy_delta'] for turn in result['turns'] if turn['turn'] == 2)
    assert delta['changed_fields'] == ['[redacted]_flag', 'scouting.[redacted]_weight']
    assert 'DIRECTIVE_SECRET' not in json.dumps(result)


def test_directive_field_list_is_bounded_with_a_visible_warning(tmp_path):
    before = {f'field_{index:02d}': index for index in range(40)}
    after = {key: value + 1 for key, value in before.items()}
    result = load(tmp_path, [start(), identity(),
                             strategy(1, 0, before), strategy(2, 0, after)])
    delta = next(turn['strategy_delta'] for turn in result['turns'] if turn['turn'] == 2)
    assert delta['changed'] is True
    assert len(delta['changed_fields']) == c.MAX_DIRECTIVE_FIELDS == 32
    assert delta['changed_fields'][0] == 'field_00'
    assert delta['changed_fields'][-1] == 'field_31'
    assert 'Directive field list limit reached; later fields omitted.' in result['warnings']


def economy(turn, pid, seq_tool, args, result_doc, policy=None):
    payload = {'tool': seq_tool, 'args': args, 'result': result_doc, 'source': 'autopilot'}
    if policy is not None:
        payload['production_policy'] = policy
    return event('HEARTBEAT', audit='strategy_economy', turn=turn, player_id=pid,
                 agent_id=f'seat{pid}', strategy_payload_json=json.dumps(payload))


def test_economy_rows_carry_candidate_reasons_rejections_and_a_row_bound(tmp_path):
    policy = {'candidates': [
        {'item_id': 'WARRIOR', 'kind': 'unit', 'eligible': True, 'reason': 'defense_gap'},
        {'item_id': 'MONUMENT', 'kind': 'building', 'eligible': False, 'reason': 'no_slot'}]}
    events = [start(), identity(), lease(1, 0),
              economy(1, 0, 'set_research', {'tech_id': 'POTTERY'},
                      {'status': 'accepted', 'rejection': None,
                       'result': {'tool': 'set_research', 'detail': 'POTTERY'}}),
              economy(1, 0, 'set_city_production', {'city_id': 'c0:1', 'item_id': 'WARRIOR'},
                      {'status': 'rejected', 'rejection': {'code': 'busy'},
                       'result': {'tool': 'set_city_production', 'detail': None}}, policy),
              lease(2, 0)]
    events += [economy(2, 0, 'set_research', {'tech_id': f'T{index}'},
                       {'status': 'accepted', 'rejection': None, 'result': {}})
               for index in range(20)]
    result = load(tmp_path, events)
    rows = {turn['turn']: turn['economy'] for turn in result['turns']}
    first, second = rows[1]
    assert (first['tool'], first['item'], first['status']) == (
        'set_research', 'POTTERY', 'accepted')
    assert first['outcome'] == 'POTTERY' and first['rejection'] is None
    assert first['candidates'] is None and first['eligible'] is None and first['reason'] is None
    assert (second['item'], second['city_id']) == ('WARRIOR', 'c0:1')
    assert second['status'] == 'rejected' and second['rejection'] == '{"code":"busy"}'
    assert second['reason'] == 'defense_gap' and second['outcome'] is None
    assert second['candidates'] == 2 and second['eligible'] == 1
    assert len(rows[2]) == 16
    assert 'Economy row limit reached; later rows omitted.' in result['warnings']


def test_growth_copies_an_allowlist_and_stays_null_without_a_record(tmp_path):
    assessment = {'mode': 'defend', 'minimum_military_count': 2, 'planning_strength_target': 40,
                  'known_threat_strength_sum': 10.5, 'healthy_local_defense_strength_sum': 50,
                  'quiet_observed_turns': 1, 'quiet_turns_required': 3,
                  'confirmed_local_barbarian_ids': ['u2:1'], 'unknown_threat_strength_ids': [],
                  'unclassified_or_nonbarbarian_contact_ids': ['u3:1', 'u3:2'],
                  'settlement_mission': {'status': 'awaiting_settler', 'site': '7,18',
                                         'mission_id': 'settle:0:1:7,18'},
                  'uncertainties': ['fog_threats_unobserved'], 'secret_notes': 'not copied'}
    result = load(tmp_path, [
        start(), identity(), lease(1, 0), lease(2, 0),
        event('HEARTBEAT', audit='strategy_growth', turn=1, player_id=0, agent_id='seat0',
              strategy_payload_json=json.dumps({'assessment': assessment,
                                                'reserved_roles': {'u0:1': 'settler'},
                                                'source': 'autopilot'}))])
    growth = {turn['turn']: turn['growth'] for turn in result['turns']}
    assert growth[2] is None
    assert growth[1] == {
        'mode': 'defend', 'minimum_military_count': 2, 'planning_strength_target': 40,
        'known_threat_strength_sum': 10.5, 'healthy_local_defense_strength_sum': 50,
        'threats': {'confirmed_barbarian': 1, 'unknown': 0, 'unclassified': 2},
        'quiet': [1, 3], 'settlement_mission': {'status': 'awaiting_settler', 'site': '7,18'},
        'uncertainties': ['fog_threats_unobserved'], 'reserved_roles': {'u0:1': 'settler'}}
    assert 'not copied' not in json.dumps(result)


def test_absent_threat_lists_are_unknown_not_observed_zeroes(tmp_path):
    result = load(tmp_path, [
        start(), identity(), lease(1, 0),
        event('HEARTBEAT', audit='strategy_growth', turn=1, player_id=0, agent_id='seat0',
              strategy_payload_json=json.dumps({
                  'assessment': {'mode': 'expand', 'confirmed_local_barbarian_ids': []},
                  'source': 'autopilot'}))])
    growth = result['turns'][0]['growth']
    assert growth['threats'] == {'confirmed_barbarian': 0, 'unknown': None, 'unclassified': None}
    assert growth['quiet'] == [None, None]
    assert growth['settlement_mission'] is None


def filler(turn, seq=0):
    return {'turn': turn, 'seq': seq, 'text': 'x' * 400}


def comparison_payload():
    return {'turns': [filler(1)], 'warnings': [],
            'research': {'seats': [{'history': [filler(1, 1), filler(2, 2)]}]},
            'timeline': {'turns': [1, 2], 'seats': [{'rows': [filler(1)]},
                                                    {'rows': [filler(2)]}]}}


def test_bound_response_trims_comparison_rows_only_after_turns(monkeypatch):
    payload = comparison_payload()
    monkeypatch.setattr(d, 'MAX_RESPONSE_BYTES', 1600)
    result = d.bound_response(payload)
    assert result['turns'] == []
    assert result['timeline']['seats'][0]['rows'] == []
    assert [row['turn'] for row in result['timeline']['seats'][1]['rows']] == [2]
    assert result['timeline']['turns'] == [2]
    assert len(result['research']['seats'][0]['history']) == 2
    assert result['warnings'] == ['Response size limit reached; older turns omitted.',
                                  'Response size limit reached; older comparison rows omitted.']
    monkeypatch.setattr(d, 'MAX_RESPONSE_BYTES', 100)
    payload = comparison_payload()
    with pytest.raises(ValueError, match='size limit'):
        d.bound_response(payload)
    assert payload['research']['seats'][0]['history'] == []
    assert payload['timeline']['seats'][0]['rows'] == []


def test_empty_projection_still_carries_every_comparison_key():
    payload = d.project_events([], [], d.Redactor())
    assert payload['research']['seats'] == []
    assert payload['research']['diff'] == {'as_of': [], 'shared': [], 'only': {},
                                           'counts': {'shared': 0, 'only': {}}}
    assert payload['research']['incomplete'] is False
    assert payload['research']['catalog']['route'] == '/api/tech-tree'
    assert payload['timeline']['turns'] == []
    assert payload['timeline']['seats'] == []
    assert payload['timeline']['violations'] == []
    assert len(payload['timeline']['series']) == 8


def test_seat_turns_always_carry_comparison_keys(tmp_path):
    result = load(tmp_path, [start(), identity(), lease(1, 0)])
    turn = result['turns'][0]
    assert turn['strategy_delta'] is None and turn['economy'] == [] and turn['growth'] is None
