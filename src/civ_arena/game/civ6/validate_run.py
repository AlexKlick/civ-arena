"""Audit hotseat event structure. This cannot prove live engine state or victory."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from civ_arena.game.civ6.hotseat_roster import seat_order
from civ_arena.session.legality import KNOWN_ACTION_TOOLS


def validate(run_dir: Path, rounds: int = 30, *, require_live: bool = True) -> dict:
    records = [json.loads(line) for line in (run_dir / 'events.jsonl').read_text().splitlines()]
    summary = json.loads((run_dir / 'summary.json').read_text())
    errors = []
    def require(condition, message):
        if not condition:
            errors.append(message)
    require([r.get('seq') for r in records] == list(range(len(records))), 'event sequence')
    starts = [r for r in records if r['kind'] == 'MATCH_START']
    ends = [r for r in records if r['kind'] == 'MATCH_END']
    require(len(starts) == 1 and records[0]['kind'] == 'MATCH_START', 'one initial MATCH_START')
    require(len(ends) == 1 and records[-1]['kind'] == 'MATCH_END', 'one final MATCH_END')
    require(bool(ends) and ends[-1]['summary'] == summary, 'event/summary equality')
    require(summary.get('clean') is True and summary.get('aborted') is None, 'clean outcome')
    require(summary.get('cleanup', {}).get('status') == 'completed', 'cleanup completed')
    require(summary.get('violations_total') == 0, 'zero watchdog violations')
    require(not any(r['kind'] == 'VIOLATION' for r in records), 'no violation events')
    require(summary.get('completed_rounds') == rounds, 'complete round count')
    require(summary.get('requested_rounds') == rounds, 'requested round count')
    identity = summary.get('identity') or {}
    require(bool(identity.get('commit')) and identity.get('dirty') is False,
            'clean commit identity')
    require(bool(identity.get('mod_sha256')), 'mod identity')
    agents = identity.get('config', {}).get('agents', [])
    try:
        seats = seat_order(a['player_id'] for a in agents)
    except ValueError:
        seats = []
        require(False, 'two through four distinct seats')
    seat_count = len(seats)
    rows = summary.get('per_turn', [])
    require(len(rows) == seat_count * rounds, 'completed seat count')
    expected = [(rows[0]['turn'] + n // seat_count, seats[n % seat_count])
                for n in range(seat_count * rounds)] \
        if rows and seats else []
    actual = [(r['turn'], r['player']) for r in rows]
    require(actual == expected and bool(expected), 'ordered seat/engine-turn pairs')
    for kind in ('LEASE_GRANT', 'LEASE_RELEASE', 'TURN_END'):
        pairs = [(r['turn'], r['player_id']) for r in records if r['kind'] == kind]
        require(pairs == expected, f'{kind} order/count')
    completed = [r['row'] for r in records if r.get('audit') == 'completed_seat_turn']
    require(completed == rows, 'completed-turn audit/summary equality')
    granted = None
    pending = None
    for r in records:
        if r['kind'] == 'LEASE_GRANT':
            require(granted is None, 'overlapping leases')
            granted = (r['turn'], r['player_id'], r['lease_id'])
        elif r['kind'] == 'LEASE_RELEASE':
            require(granted == (r['turn'], r['player_id'], r['lease_id']), 'release lease identity')
            granted = None
        elif r['kind'] == 'TOOL_CALL':
            require(pending is None, 'tool result missing')
            pending = (r['turn'], r['player_id'], r['tool'])
        elif r['kind'] == 'TOOL_RESULT':
            require(pending == (r['turn'], r['player_id'], r['tool']), 'tool pair mismatch')
            pending = None
    require(granted is None and pending is None, 'no open lease or tool call')
    closure = [(r['turn'], r['player_id']) for r in records
               if r['kind'] == 'TOOL_RESULT' and r.get('tool') == 'end_turn'
               and r.get('status') == 'accepted']
    require(closure == expected, 'accepted end_turn count/order')
    for pid in seats:
        require(any(r['kind'] == 'TOOL_RESULT' and r.get('player_id') == pid
                    and r.get('tool') in KNOWN_ACTION_TOOLS and r.get('status') == 'accepted'
                    and not r.get('duplicate') for r in records), f'accepted game action p{pid}')
    allowance = summary.get('movement_allowance')
    require(isinstance(allowance, bool), 'explicit movement allowance')
    allowances = [r for r in records if r.get('audit') == 'movement_allowance']
    if allowance:
        require([(r['turn'], r['player_id']) for r in allowances] == expected,
                'allowance audit every completed turn')
        for r in allowances:
            for m in r['mutations']:
                require(m['entity_type'] == 'unit'
                        and m['attr'] in ('moves', 'movement', 'pos', 'q', 'r')
                        and r.get('owners', {}).get(m['entity_id']) == r['player_id'],
                        'movement allowance policy')
    limits = summary.get('limits', {})
    require(all(r.get('lease_released') is True and
                r.get('elapsed_s', float('inf')) <= limits.get('agent_turn', 0) for r in rows),
            'released turns within deadlines')
    require(any(r.get('audit') == 'play_complete' and r['elapsed_s'] <= limits.get('match', 0)
                for r in records), 'play deadline')
    require(all(r.get('attempts', 0) <= limits.get('sweeps', 0)
                and r.get('elapsed_s', 0) <= limits.get('recovery', 0)
                for r in records if r.get('audit') == 'engaged'), 'recovery episode limits')
    run_identity = [r for r in records if r.get('audit') == 'run_identity']
    require(len(run_identity) == 1 and run_identity[0]['identity'] == identity, 'identity audit')
    if require_live:
        require(bool(run_identity) and run_identity[0]['fake'] is False, 'real engine run')
        for agent in agents:
            if agent['policy'] == 'llm':
                posts = summary.get('request_usage', {}).get(agent['agent_id'])
                require(isinstance(posts, int)
                        and 0 < posts <= agent['llm']['max_requests_per_match'],
                        f"provider requests {agent['agent_id']}")
    return dict(status='FAIL' if errors else 'PASS', errors=errors,
                completed_seat_turns=len(rows), completed_rounds=summary.get('completed_rounds'),
                structural_replay='event structure only; engine-state replay not proven',
                movement_allowance=allowance)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('run_dir', type=Path)
    ap.add_argument('--rounds', type=int, default=30)
    ap.add_argument('--allow-fake', action='store_true')
    opts = ap.parse_args()
    try:
        result = validate(opts.run_dir, opts.rounds, require_live=not opts.allow_fake)
    except (OSError, ValueError, KeyError, TypeError, IndexError) as exc:
        result = dict(status='INCOMPLETE', errors=[str(exc)])
    print(json.dumps(result, sort_keys=True))
    raise SystemExit(0 if result['status'] == 'PASS' else 1)


if __name__ == '__main__':
    main()
