"""Comparison projections for the read-only match room.

Pure functions over already-read events. Nothing here contacts the game, a
provider or the network, and nothing here reaches a model packet. Every value
is a retained observation: a seat's own request packet, a driver audit row or an
accepted tool result. Missing values stay null; they are never interpolated,
averaged or carried forward across turns.
"""

from __future__ import annotations

import hashlib
import json
import math

from civ_arena.dashboard_map import CONTEXT_MARKER

MAX_RESEARCHED = 128
MAX_HISTORY = 160
MAX_OPTIONS = 32
MAX_ROWS = 240
MAX_MARKS = 64
MAX_ECONOMY = 16
MAX_ROLES = 32
MAX_REASONS = 16
MAX_UNCERTAINTIES = 16
MAX_DIRECTIVE_FIELDS = 32
NAME_CHARS = 40

RESEARCH_NOTE = ("Research sets come from each seat's own retained request packets. Seats are "
                 'asynchronous, so the two as-of turns can differ. Retained data, not live.')
TIMELINE_NOTE = ('Per-turn rows come from retained audits, tool results and request packets. '
                 'Gaps are unsupplied values, never interpolated. Provider requests are '
                 'recorded POST attempts, not decisions.')
CATALOG_NOTE = ('Tree layout comes from the base source catalog (scope base_source_catalog, '
                'effective ruleset unverified, group semantics unverified). It is not the '
                'effective ruleset of this match.')
TECH_TREE_ROUTE = '/api/tech-tree'

PACKET_UNBOUND = 'Research packet could not be bound.'
RESEARCH_SHRANK = 'Researched set shrank between packets.'
RESEARCH_MISSING = 'Research packet has no researched list.'
RESEARCH_NON_TEXT = 'Non-text entries dropped from a researched list.'
RESEARCH_CAPPED = 'Researched name limit reached; later names omitted.'
HISTORY_CAPPED = 'Research history limit reached; older rows omitted.'
OPTIONS_CAPPED = 'Research option limit reached; later options omitted.'
UNKNOWN_SEAT = 'Research packet for an unconfigured seat was ignored.'
ROWS_CAPPED = 'Timeline row limit reached; older rows omitted.'
MARKS_CAPPED = 'Violation mark limit reached; older marks omitted.'
ECONOMY_CAPPED = 'Economy row limit reached; later rows omitted.'
ROLES_CAPPED = 'Reserved role limit reached; later roles omitted.'
DIRECTIVE_CAPPED = 'Directive field list limit reached; later fields omitted.'
STRATEGY_MALFORMED = 'Comparison strategy payload JSON is malformed.'

SERIES = (('elapsed_s', 'Seat turn time (s)'),
          ('requests', 'Provider requests (recorded POST attempts)'),
          ('calls', 'Tool calls'),
          ('allowed_mutations', 'Allowed mutations'),
          ('gold', 'Gold'),
          ('techs', 'Techs researched'),
          ('units', 'Own units'),
          ('cities', 'Own cities'))
OBSERVING_TOOLS = ('get_overview', 'get_units', 'get_cities')
ROW_FIELDS = ('gold', 'techs', 'researching', 'units', 'cities', 'requests', 'calls',
              'allowed_mutations', 'violations', 'elapsed_s')


def dashboard():
    """The reader is imported lazily; dashboard.py imports this module."""
    from civ_arena import dashboard as module
    return module


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)


def warn(warnings, message):
    if message not in warnings:
        warnings.append(message)


def count(value):
    """An observed integer. A bool is a flag, never a measurement."""
    return value if type(value) is int else None


def number(value):
    if type(value) is int or (type(value) is float and math.isfinite(value)):
        return value
    return None


def name(value, redactor):
    return redactor.text(value)[:NAME_CHARS] if isinstance(value, str) else None


def text(value, redactor):
    return redactor.text(value) if isinstance(value, str) else None


def length(value):
    return len(value) if isinstance(value, list) else None


def packet_state(event, warnings):
    """Return a request packet's projected state, or None when it cannot be bound.

    Custody is checked before anything is displayed: the recorded character
    count and digest must match the context, the context must carry exactly one
    projection marker, and the projected seat must be the event's own seat.
    """
    try:
        payload = event.get('strategy_payload_json')
        if isinstance(payload, str | bytes):
            payload = dashboard().parse_json(payload)
        if not isinstance(payload, dict):
            raise ValueError('request payload is not an object')
        if type(event.get('player_id')) is not int or type(event.get('turn')) is not int \
                or type(event.get('seq')) is not int:
            raise ValueError('request packet has no seat turn identity')
        context = payload.get('user_context')
        if not isinstance(context, str):
            raise ValueError('request packet has no context')
        if type(payload.get('context_chars')) is not int \
                or payload['context_chars'] != len(context):
            raise ValueError('recorded context length does not match the context')
        digest = hashlib.sha256(context.encode()).hexdigest()
        if payload.get('context_sha256') != digest:
            raise ValueError('recorded context digest does not match the context')
        if context.count(CONTEXT_MARKER) != 1:
            raise ValueError('context has no single projection marker')
        state = dashboard().parse_json(context.split(CONTEXT_MARKER)[1])
        if not isinstance(state, dict) or not isinstance(state.get('you'), dict):
            raise ValueError('projected state is not an object')
        # A bool is not a seat identity: it must never bind by equalling 0 or 1.
        if type(state['you'].get('player_id')) is not int \
                or state['you']['player_id'] != event['player_id']:
            raise ValueError('projected state belongs to another seat')
        return state
    except (ValueError, TypeError, KeyError, RecursionError, UnicodeError):
        warn(warnings, PACKET_UNBOUND)
        return None


def seat_packets(events, warnings):
    """Group bound request packets by seat, keeping the recorded order."""
    packets, unbound = {}, False
    for event in events:
        if event.get('kind') != 'HEARTBEAT' or event.get('audit') != 'strategy_request':
            continue
        state = packet_state(event, warnings)
        if state is None:
            unbound = True
            continue
        packets.setdefault(event['player_id'], []).append((event, state))
    return packets, unbound


def tech_names(you, warnings, redactor):
    """The packet's researched set, or None when the packet did not supply one.

    An absent or unusable list is unknown, not empty: returning a set here would
    read as a seat that has researched nothing.
    """
    raw = you.get('researched')
    if not isinstance(raw, list):
        warn(warnings, RESEARCH_MISSING)
        return None
    names, dropped = set(), False
    for entry in raw:
        label = name(entry, redactor)
        if label is None:
            dropped = True
        else:
            names.add(label)
    if dropped:
        warn(warnings, RESEARCH_NON_TEXT)
    return names


def research_options(state, warnings, redactor):
    raw = state.get('research_options')
    if not isinstance(raw, list):
        return []
    rows = []
    for entry in raw:
        tech = name(entry.get('tech_id'), redactor) if isinstance(entry, dict) else None
        if tech is None:
            continue
        rows.append({'tech_id': tech, 'cost': count(entry.get('cost'))})
    if len(rows) > MAX_OPTIONS:
        warn(warnings, OPTIONS_CAPPED)
    return rows[:MAX_OPTIONS]


def options_source(state, redactor):
    sources = state.get('option_sources')
    return name(sources.get('research'), redactor) if isinstance(sources, dict) else None


def fold_history(rows):
    """Every name still held after replaying the given history rows in order."""
    names = set()
    for row in rows:
        names = (names | set(row['added'])) - set(row['removed'])
    return names


def capped_history(history, warnings):
    """Truncate to the newest rows, keeping one synthetic baseline row.

    Dropping rows blindly would make an old set look like a fresh discovery at
    the oldest retained turn. The dropped rows are folded into one row that
    carries the last dropped packet's identity and is marked `baseline`, so a
    reader can see that nothing before it was retained.
    """
    if len(history) <= MAX_HISTORY:
        return history
    warn(warnings, HISTORY_CAPPED)
    dropped, kept = history[:len(history) - MAX_HISTORY + 1], history[-(MAX_HISTORY - 1):]
    names, last = sorted(fold_history(dropped)), history[len(history) - MAX_HISTORY]
    if len(names) > MAX_RESEARCHED:
        warn(warnings, RESEARCH_CAPPED)
    return [{'turn': last['turn'], 'seq': last['seq'], 'researching': last['researching'],
             'added': names[:MAX_RESEARCHED], 'removed': [], 'count': len(names),
             'baseline': True}] + kept


def research_from_packets(packets, warnings, redactor):
    """Per-seat research fields: the latest packet plus a delta-only history.

    A packet without a researched list is not a research observation: it is
    counted in `packets`, but it never removes a name, resets a count or
    replaces the latest recorded state.
    """
    latest, history, names = None, [], set()
    for event, state in packets:
        you = state['you']
        current = tech_names(you, warnings, redactor)
        if current is None:
            continue
        added, removed = sorted(current - names), sorted(names - current)
        if removed:
            warn(warnings, RESEARCH_SHRANK)
        if len(added) > MAX_RESEARCHED or len(removed) > MAX_RESEARCHED:
            warn(warnings, RESEARCH_CAPPED)
        if added or removed or not history:
            history.append({'turn': event['turn'], 'seq': event['seq'],
                            'researching': name(you.get('researching'), redactor),
                            'added': added[:MAX_RESEARCHED], 'removed': removed[:MAX_RESEARCHED],
                            'count': len(current), 'baseline': False})
        names = current
        ordered = sorted(current)
        if len(ordered) > MAX_RESEARCHED:
            warn(warnings, RESEARCH_CAPPED)
        latest = {'turn': event['turn'], 'seq': event['seq'],
                  'ts': text(event.get('ts'), redactor),
                  'researching': name(you.get('researching'), redactor),
                  'researched': ordered[:MAX_RESEARCHED], 'researched_count': len(ordered),
                  'gold': count(you.get('gold')),
                  'research_options': research_options(state, warnings, redactor),
                  'options_source': options_source(state, redactor)}
    return {'packets': len(packets), 'latest': latest,
            'history': capped_history(history, warnings),
            'civ_name': name(packets[-1][1]['you'].get('civ_name'), redactor)
            if packets else None}


def researched_at(history, turn):
    """The researched set after folding every history row up to `turn`."""
    names = set()
    for row in history:
        if row['turn'] <= turn:
            names = (names | set(row['added'])) - set(row['removed'])
    return names


def diff(seats):
    """Shared and seat-only research, from each seat's own latest packet."""
    observed = [seat for seat in seats if seat['latest'] is not None]
    as_of = [{'player_id': seat['player_id'], 'turn': seat['latest']['turn'],
              'seq': seat['latest']['seq']} for seat in observed]
    sets = {seat['player_id']: set(seat['latest']['researched']) for seat in observed}
    # One seat alone shares nothing; claiming otherwise would read as agreement.
    shared = sorted(set.intersection(*sets.values())) if len(sets) > 1 else []
    only = {}
    for pid, names in sets.items():
        others = [other for key, other in sets.items() if key != pid]
        only[str(pid)] = sorted(names - set.union(*others) if others else names)
    return {'as_of': as_of, 'shared': shared, 'only': only,
            'counts': {'shared': len(shared),
                       'only': {pid: len(names) for pid, names in only.items()}}}


def seat_index(turns):
    """Seat identity as already recorded by the projection's turn keys."""
    players = {}
    for _, pid, aid in turns:
        players.setdefault(aid, pid)
    return players


def locate(turns, players, event):
    """The seat turn an event belongs to, resolved exactly as the projection does."""
    aid = event.get('agent_id')
    pid = event.get('player_id', event.get('phase_player_id'))
    if pid is None or pid == -1:
        pid = players.get(aid)
    return (event.get('turn'), pid, aid)


def strategy_payload(event, warnings):
    payload = event.get('strategy_payload_json')
    try:
        if isinstance(payload, str | bytes):
            payload = dashboard().parse_json(payload)
    except (ValueError, TypeError, RecursionError, UnicodeError):
        payload = None
    if not isinstance(payload, dict):
        warn(warnings, STRATEGY_MALFORMED)
        return None
    return payload


def directive_fields(previous, current, warnings, redactor):
    """Top-level directive keys that differ; scouting is expanded one level.

    Keys are recorded payload strings, so they are redacted and bounded exactly
    like any other displayed name, and the list itself is bounded.
    """
    fields = []
    for key in sorted(item for item in set(previous) | set(current) if isinstance(item, str)):
        if key == 'tactical_overrides':
            continue
        old, new = previous.get(key), current.get(key)
        if key == 'scouting' and isinstance(old, dict) and isinstance(new, dict):
            fields += [name(f'scouting.{sub}', redactor)
                       for sub in sorted(item for item in set(old) | set(new)
                                         if isinstance(item, str))
                       if canonical(old.get(sub)) != canonical(new.get(sub))]
        elif canonical(old) != canonical(new):
            fields.append(name(key, redactor))
    if len(fields) > MAX_DIRECTIVE_FIELDS:
        warn(warnings, DIRECTIVE_CAPPED)
    return fields[:MAX_DIRECTIVE_FIELDS]


def strategy_delta(payload, previous, warnings, redactor):
    directive = payload.get('directive')
    directive = directive if isinstance(directive, dict) else {}
    overrides = directive.get('tactical_overrides')
    reasons = payload.get('reasons') if isinstance(payload.get('reasons'), list) else []
    changed, fields = None, []
    if previous is not None:
        changed = canonical(previous[1]) != canonical(directive)
        fields = directive_fields(previous[1], directive, warnings, redactor)
    return {'changed': changed,
            'previous_turn': previous[0] if previous is not None else None,
            'source': name(payload.get('source'), redactor),
            'reasons': [text(reason, redactor) for reason in reasons[:MAX_REASONS]
                        if isinstance(reason, str)],
            'last_decision_turn': count(payload.get('last_decision_turn')),
            'changed_fields': fields,
            'tactical_overrides': length(overrides) or 0}


def economy_row(event, payload, redactor):
    args = payload.get('args') if isinstance(payload.get('args'), dict) else {}
    result = payload.get('result') if isinstance(payload.get('result'), dict) else {}
    inner = result.get('result') if isinstance(result.get('result'), dict) else {}
    policy = payload.get('production_policy')
    policy = policy if isinstance(policy, dict) else {}
    candidates = policy.get('candidates') if isinstance(policy.get('candidates'), list) else None
    item = name(args.get('tech_id'), redactor) or name(args.get('item_id'), redactor)
    reason = None
    if candidates is not None:
        for candidate in candidates:
            if isinstance(candidate, dict) and name(candidate.get('item_id'), redactor) == item:
                reason = name(candidate.get('reason'), redactor)
                break
    rejection = result.get('rejection')
    if rejection is not None and not isinstance(rejection, str):
        rejection = canonical(rejection)
    return {'seq': count(event.get('seq')), 'tool': name(payload.get('tool'), redactor),
            'item': item, 'city_id': name(args.get('city_id'), redactor),
            'status': name(result.get('status'), redactor),
            'rejection': text(rejection, redactor),
            'reason': reason, 'outcome': text(inner.get('detail'), redactor),
            'candidates': None if candidates is None else len(candidates),
            'eligible': None if candidates is None else
            sum(1 for c in candidates if isinstance(c, dict) and c.get('eligible') is True)}


def settlement_mission(assessment, redactor):
    mission = assessment.get('settlement_mission')
    if not isinstance(mission, dict):
        return None
    return {'status': name(mission.get('status'), redactor),
            'site': name(mission.get('site'), redactor)}


def growth_block(payload, warnings, redactor):
    assessment = payload.get('assessment')
    if not isinstance(assessment, dict):
        return None
    roles = payload.get('reserved_roles')
    roles = roles if isinstance(roles, dict) else {}
    if len(roles) > MAX_ROLES:
        warn(warnings, ROLES_CAPPED)
    reserved = {}
    for unit, role in list(roles.items())[:MAX_ROLES]:
        label = name(unit, redactor)
        if label is not None:
            reserved[label] = name(role, redactor)
    uncertainties = assessment.get('uncertainties')
    uncertainties = uncertainties if isinstance(uncertainties, list) else []
    return {'mode': name(assessment.get('mode'), redactor),
            'minimum_military_count': count(assessment.get('minimum_military_count')),
            'planning_strength_target': number(assessment.get('planning_strength_target')),
            'known_threat_strength_sum': number(assessment.get('known_threat_strength_sum')),
            'healthy_local_defense_strength_sum':
                number(assessment.get('healthy_local_defense_strength_sum')),
            # An absent threat list is unknown, not an observed zero.
            'threats': {
                'confirmed_barbarian':
                    length(assessment.get('confirmed_local_barbarian_ids')),
                'unknown': length(assessment.get('unknown_threat_strength_ids')),
                'unclassified':
                    length(assessment.get('unclassified_or_nonbarbarian_contact_ids'))},
            'quiet': [count(assessment.get('quiet_observed_turns')),
                      count(assessment.get('quiet_turns_required'))],
            'settlement_mission': settlement_mission(assessment, redactor),
            'uncertainties': [text(item, redactor)
                              for item in uncertainties[:MAX_UNCERTAINTIES]
                              if isinstance(item, str)],
            'reserved_roles': reserved}


def decisions_from(events, turns, warnings, redactor):
    """Attach directive deltas, economy rows and growth assessments to seat turns."""
    players = seat_index(turns)
    baseline, current = {}, {}
    for event in events:
        audit = event.get('audit')
        if event.get('kind') != 'HEARTBEAT' or audit not in (
                'strategy_execution', 'strategy_economy', 'strategy_growth'):
            continue
        turn = turns.get(locate(turns, players, event))
        if turn is None:
            continue
        payload = strategy_payload(event, warnings)
        if payload is None:
            continue
        if audit == 'strategy_execution':
            directive = payload.get('directive')
            directive = directive if isinstance(directive, dict) else {}
            pid, at = turn['player_id'], turn['turn']
            seen = current.get(pid)
            # Repair attempts within one seat turn compare against the previous
            # turn's directive, never against an earlier attempt of their own.
            if seen is not None and seen[0] != at:
                baseline[pid] = seen
            current[pid] = (at, directive)
            turn['strategy_delta'] = strategy_delta(payload, baseline.get(pid), warnings,
                                                    redactor)
        elif audit == 'strategy_economy':
            if len(turn['economy']) >= MAX_ECONOMY:
                warn(warnings, ECONOMY_CAPPED)
                continue
            turn['economy'].append(economy_row(event, payload, redactor))
        else:
            turn['growth'] = growth_block(payload, warnings, redactor)


def turn_facts(events, turns, packets, warnings, redactor):
    """Observed per-seat-turn facts, keyed exactly like the projection's turns."""
    players = seat_index(turns)
    facts, marks = {}, []
    for seat in packets.values():
        for event, state in seat:
            key = locate(turns, players, event)
            if key in turns:
                facts.setdefault(key, {})['packet'] = state
    for event in events:
        kind, audit = event.get('kind'), event.get('audit')
        if kind == 'VIOLATION':
            watchdog = event.get('watchdog') if isinstance(event.get('watchdog'), dict) else {}
            pid = event.get('player_id')
            marks.append({'turn': count(event.get('turn')),
                          'player_id': pid if type(pid) is int else players.get(
                              event.get('agent_id')),
                          'kind': text(watchdog.get('kind'), redactor),
                          'detail': text(watchdog.get('detail'), redactor),
                          'seq': count(event.get('seq'))})
            continue
        if kind == 'HEARTBEAT' and audit == 'completed_seat_turn':
            row = event.get('row')
            if not isinstance(row, dict):
                continue
            key = locate(turns, players, dict(event, turn=row.get('turn'),
                                              player_id=row.get('player'),
                                              agent_id=row.get('agent')))
            if key in turns:
                facts.setdefault(key, {})['audit'] = row
            continue
        if kind not in ('TOOL_CALL', 'TOOL_RESULT'):
            continue
        key = locate(turns, players, event)
        if key not in turns:
            continue
        fact = facts.setdefault(key, {})
        if kind == 'TOOL_CALL':
            fact['calls'] = fact.get('calls', 0) + 1
        elif event.get('tool') in OBSERVING_TOOLS and event.get('status') == 'accepted' \
                and isinstance(event.get('observed'), dict):
            fact[event['tool']] = event['observed']
    if len(marks) > MAX_MARKS:
        warn(warnings, MARKS_CAPPED)
    return facts, marks[-MAX_MARKS:]


def first_value(*candidates):
    """The highest-priority recorded value with its source; null keeps a null source."""
    for value, source in candidates:
        if value is not None:
            return value, source
    return None, None


def timeline_row(turn, fact, added, redactor):
    audit = fact.get('audit') if isinstance(fact.get('audit'), dict) else {}
    overview = fact.get('get_overview') or {}
    seen_units = fact.get('get_units') or {}
    seen_cities = fact.get('get_cities') or {}
    state = fact.get('packet') or {}
    you = state.get('you') if isinstance(state.get('you'), dict) else {}
    researched = you.get('researched')
    values, sources = {}, {}
    values['gold'], sources['gold'] = first_value(
        (count(overview.get('gold')), 'tool'), (count(you.get('gold')), 'packet'))
    values['techs'], sources['techs'] = first_value(
        (count(overview.get('techs')), 'tool'), (length(researched), 'packet'))
    values['researching'], sources['researching'] = first_value(
        (name(overview.get('researching'), redactor), 'tool'),
        (name(you.get('researching'), redactor), 'packet'))
    values['units'], sources['units'] = first_value(
        (count(seen_units.get('own_units')), 'tool'), (length(state.get('own_units')), 'packet'))
    values['cities'], sources['cities'] = first_value(
        (count(seen_cities.get('own_cities')), 'tool'),
        (length(state.get('own_cities')), 'packet'))
    # The whole event stream was read for this seat turn, so an absent counter
    # is an observed zero rather than a gap.
    values['requests'], sources['requests'] = first_value((count(turn.get('requests')), 'events'))
    values['calls'], sources['calls'] = first_value((count(fact.get('calls', 0)), 'events'))
    values['allowed_mutations'], sources['allowed_mutations'] = first_value(
        (count(audit.get('allowed_mutations')), 'audit'))
    values['violations'], sources['violations'] = first_value(
        (count(audit.get('violations')), 'audit'))
    values['elapsed_s'], sources['elapsed_s'] = first_value(
        (number(audit.get('elapsed_s')), 'audit'))
    return {'turn': turn['turn'], 'status': turn['status'], 'tech_added': added,
            'sources': {field: sources[field] for field in ROW_FIELDS},
            **{field: values[field] for field in ROW_FIELDS}}


def timeline_from(events, turns, seats, packets, warnings, redactor):
    """Per-seat-turn rows from audits, tool results and request packets."""
    facts, marks = turn_facts(events, turns, packets, warnings, redactor)
    rows = {seat['player_id']: [] for seat in seats}
    added = {}
    for seat in seats:
        for row in seat['history']:
            # A synthetic baseline row states what a seat already held, not what
            # it acquired on that turn; it is never a turn's recorded addition.
            if row['baseline']:
                continue
            added.setdefault((seat['player_id'], row['turn']), []).extend(row['added'])
    for key, turn in turns.items():
        if turn['player_id'] not in rows:
            continue
        rows[turn['player_id']].append(timeline_row(
            turn, facts.get(key, {}), sorted(added.get((turn['player_id'], turn['turn']), [])),
            redactor))
    seat_rows = []
    for seat in seats:
        ordered = sorted(rows[seat['player_id']], key=lambda row: row['turn'])
        if len(ordered) > MAX_ROWS:
            warn(warnings, ROWS_CAPPED)
        seat_rows.append({'player_id': seat['player_id'], 'agent_id': seat['agent_id'],
                          'rows': ordered[-MAX_ROWS:]})
    return {'turns': timeline_turns(seat_rows), 'seats': seat_rows, 'violations': marks,
            'series': [{'key': key, 'label': label} for key, label in SERIES],
            'note': TIMELINE_NOTE}


def timeline_turns(seat_rows):
    return sorted({row['turn'] for seat in seat_rows for row in seat['rows']})


def project(events, agents, turns, warnings, redactor):
    """The research and timeline comparison blocks for one run."""
    packets, unbound = seat_packets(events, warnings)
    identities = {}
    for entry in agents.values():
        if type(entry.get('player_id')) is int:
            identities.setdefault(entry['player_id'], entry['agent_id'])
    if set(packets) - set(identities):
        warn(warnings, UNKNOWN_SEAT)
        unbound = True
    seats = []
    for pid in sorted(identities):
        seat = {'player_id': pid, 'agent_id': identities[pid]}
        seat.update(research_from_packets(packets.get(pid, []), warnings, redactor))
        seats.append(seat)
    decisions_from(events, turns, warnings, redactor)
    timeline = timeline_from(events, turns, seats, packets, warnings, redactor)
    research = {'catalog': {'route': TECH_TREE_ROUTE, 'note': CATALOG_NOTE},
                'seats': [{'player_id': seat['player_id'], 'agent_id': seat['agent_id'],
                           'civ_name': seat['civ_name'], 'packets': seat['packets'],
                           'latest': seat['latest'], 'history': seat['history']}
                          for seat in seats],
                'diff': diff(seats), 'note': RESEARCH_NOTE, 'incomplete': unbound}
    return {'research': research, 'timeline': timeline}
