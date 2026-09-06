"""Observed completions cannot supply a missing or invalid match roster."""
import copy

import pytest

from civ_arena import dashboard as d
from test_dashboard import NOW, event, write_run


def declared(count):
    return [[f'seat{pid}', pid, 'llm'] for pid in range(count)]


def identity(roster):
    return event('HEARTBEAT', audit='run_identity', identity={'config': {'agents': [
        {'agent_id': aid, 'player_id': pid, 'policy': policy} for aid, pid, policy in roster]}})


def complete(tmp_path, count, declarations, *, legacy=False):
    summary = {'clean': True, 'aborted': None, 'completed_rounds': 1}
    records = list(declarations)
    if legacy:
        records.extend(event('TURN_END', player_id=pid, agent_id=f'seat{pid}')
                       for pid in range(count))
    else:
        records.extend(event('HEARTBEAT', audit='completed_seat_turn', row={
            'turn': 1, 'player': pid, 'agent': f'seat{pid}', 'lease_released': True})
            for pid in range(count))
    records.append(event('MATCH_END', summary=summary))
    write_run(tmp_path, records, summary)
    return d.DashboardStore(tmp_path).load('match-one', now=NOW)


@pytest.mark.parametrize('count', [2, 3, 4])
@pytest.mark.parametrize('source', ['missing', 'missing_identity_config', 'empty', 'partial',
                                    'duplicate_player', 'duplicate_agent', 'bad_player',
                                    'bad_entry'])
def test_audited_rounds_refuse_missing_or_malformed_authoritative_roster(tmp_path, count, source):
    roster = declared(count)
    if source in ('missing', 'missing_identity_config'):
        declarations = [event('MATCH_START')]
        if source == 'missing_identity_config':
            declarations.append(event('HEARTBEAT', audit='run_identity', identity={}))
    else:
        if source == 'empty':
            roster = []
        elif source == 'partial':
            roster = roster[:1]
        elif source == 'duplicate_player':
            roster[-1][1] = roster[0][1]
        elif source == 'duplicate_agent':
            roster[-1][0] = roster[0][0]
        elif source == 'bad_player':
            roster[-1][1] = True
        elif source == 'bad_entry':
            roster[-1] = 'not a seat'
        declarations = [event('MATCH_START', config={'agents': roster})]
    view = complete(tmp_path, count, declarations)
    assert view['status'] == 'incomplete'
    assert view['metrics']['completed_rounds'] == 0
    assert 'Completed-seat audits require an explicit valid configured roster.' in view['warnings']


@pytest.mark.parametrize('count', [2, 3, 4])
@pytest.mark.parametrize('source', ['start', 'identity', 'both'])
def test_complete_explicit_roster_supports_each_existing_declaration_surface(
        tmp_path, count, source):
    roster = declared(count)
    declarations = [event('MATCH_START', **({'config': {'agents': roster}}
                                          if source != 'identity' else {}))]
    if source != 'start':
        declarations.append(identity(roster))
    view = complete(tmp_path, count, declarations)
    assert view['status'] == 'completed'
    assert view['metrics']['completed_rounds'] == 1
    assert view['warnings'] == []


@pytest.mark.parametrize('variant', ['changed_seat', 'expanded', 'partial_union'])
def test_declarations_cannot_expand_change_or_assemble_a_roster(tmp_path, variant):
    first, second = declared(4), declared(4)
    if variant == 'changed_seat':
        second[-1][1] = 5
    elif variant == 'expanded':
        first = first[:2]
    else:
        first, second = first[:1], second[1:]
    view = complete(tmp_path, 4, [event('MATCH_START', config={'agents': first}), identity(second)])
    assert view['status'] == 'incomplete'
    assert view['metrics']['completed_rounds'] == 0


def test_complete_configured_roster_cannot_be_replaced_by_observed_subset(tmp_path):
    # Three observed completions do not constitute a round of a four-seat match.
    view = complete(tmp_path, 3, [event('MATCH_START', config={'agents': declared(4)})])
    assert view['status'] == 'incomplete'
    assert view['metrics']['completed_rounds'] == 0


def test_legacy_two_seat_display_keeps_explicit_missing_audit_warning(tmp_path):
    view = complete(tmp_path, 2, [event('MATCH_START')], legacy=True)
    assert view['status'] == 'completed'
    assert view['metrics']['completed_rounds'] == 1
    assert 'Legacy TURN_END counts; driver completion/lease audit unavailable.' in view['warnings']


def test_legacy_observations_cannot_invent_four_seat_rounds(tmp_path):
    view = complete(tmp_path, 4, [event('MATCH_START')], legacy=True)
    assert view['status'] == 'incomplete'
    assert view['metrics']['completed_rounds'] == 0


def test_malformed_identity_cannot_overwrite_a_valid_start_roster(tmp_path):
    event_identity = identity(declared(4))
    event_identity['identity']['config']['agents'][-1] = copy.deepcopy(
        event_identity['identity']['config']['agents'][0])
    view = complete(tmp_path, 4, [event('MATCH_START', config={'agents': declared(4)}),
                                  event_identity])
    assert view['status'] == 'incomplete'
    assert view['metrics']['completed_rounds'] == 0
