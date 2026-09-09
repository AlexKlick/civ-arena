"""Audit hotseat event structure. This cannot prove live engine state or victory."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from civ_arena.session.legality import KNOWN_ACTION_TOOLS


def validate(run_dir: Path, rounds: int = 30, *, require_live: bool = True) -> dict:
    records = [json.loads(line) for line in (run_dir / 'events.jsonl').read_text().splitlines()]
    summary_path = Path(run_dir) / 'summary.json'
    summary = (json.loads(summary_path.read_text())
               if summary_path.exists() else None)
    if summary is not None and summary.get('phase') == 'spectate':
        return validate_spectate(run_dir, rounds, records=records,
                                 summary=summary, require_live=require_live)
    if summary is None:
        # CAP-02: a summary-less log can only be a running prefix. A
        # spectate-shaped one audits as such; anything else keeps the
        # legacy INCOMPLETE behavior (missing summary is not a run).
        if any(r.get('kind') in ('SPECTATOR_SNAPSHOT', 'HUMAN_TURN_START',
                                 'HUMAN_TURN_END') for r in records):
            return validate_spectate(run_dir, rounds, records=records,
                                     summary=None, require_live=require_live)
        raise FileNotFoundError(str(summary_path))
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
    seats = sorted(a['player_id'] for a in agents)
    require(len(seats) == 2 and len(set(seats)) == 2, 'two seats')
    rows = summary.get('per_turn', [])
    require(len(rows) == 2 * rounds, 'completed seat count')
    expected = [(rows[0]['turn'] + n // 2, seats[n % 2]) for n in range(2 * rounds)] \
        if rows and len(seats) == 2 else []
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


def validate_spectate(run_dir: Path, rounds: int = 30, *, records=None,
                     summary=None, require_live: bool = True) -> dict:
    """Spectate-mode audit (CAP-02): the shared structural core plus a
    declared FULL profile of outcome/coverage checks. Outcome classes let
    an interrupted or operator-stopped prefix validate honestly — an
    unpaired final human-turn START is an observed open interval, not a
    defect — while a completed run still has to be clean. Structure,
    integrity, completion, and dataset eligibility are SEPARATE
    decisions: decision_eligible requires completed + consistent
    snapshots + closed final interval + no capture gaps. Nothing here
    re-executes or compares state."""
    from civ_arena.spectate_audit import (
        OPEN_PREFIX_OUTCOMES,
        check_manifest,
        classify_outcome,
        final_interval_state,
        structural_problems,
    )
    if records is None:
        records = [json.loads(line) for line in
                   (run_dir / 'events.jsonl').read_text().splitlines()]
    summary_path = Path(run_dir) / 'summary.json'
    has_summary = summary is not None or summary_path.exists()
    if summary is None:
        summary = (json.loads(summary_path.read_text())
                   if summary_path.exists() else {})
    errors: list[str] = []
    eligibility: list[str] = []

    def require(condition, message):
        if not condition:
            errors.append(message)

    outcome = classify_outcome(summary)
    allow_open = outcome in OPEN_PREFIX_OUTCOMES
    errors.extend(structural_problems(
        records, summary=summary if has_summary else None,
        allow_open_prefix=allow_open))
    final_interval = final_interval_state(records)

    ends = [r for r in records if r['kind'] == 'MATCH_END']
    if has_summary:
        require(bool(ends) and ends[-1].get('summary') == summary,
                'event/summary equality')
        require(summary.get('violations_total') == 0,
                'zero watchdog violations')
        if outcome == 'completed':
            require(summary.get('clean') is True
                    and summary.get('aborted') is None, 'clean outcome')
            require(summary.get('cleanup', {}).get('status') ==
                    'disconnect_only_no_game_actions_no_leases',
                    'disconnect-only cleanup')
            require(summary.get('completed_rounds') == rounds
                    and summary.get('requested_rounds') == rounds,
                    'round counts')
        else:
            # an honest non-completed record still has to declare its
            # failure (running_prefix excepted: no summary at all)
            if outcome != 'running_prefix':
                require(bool(summary.get('failure_reason')
                             or summary.get('aborted')),
                        'failure reason recorded for non-completed outcome')
            require(summary.get('completed_rounds') == rounds,
                    'completed round count matches captured rounds')
        census = summary.get('command_census', {})
        require(census.get('game_writes') == 0, 'zero game writes')
        require(census.get('status_polls', 0) >= rounds, 'polled every round')
        require(isinstance(summary.get('operator'), str)
                and summary.get('operator'), 'operator identity')
        require(bool(summary.get('observed_players')), 'observed players')
        identity = summary.get('identity') or {}
        require(bool(identity.get('commit')) and bool(identity.get('mod_sha256')),
                'implementation identity')
        if require_live:
            require(identity.get('dirty') is False, 'clean commit identity')
            run_identity = [r for r in records if r.get('audit') == 'run_identity']
            require(len(run_identity) == 1
                    and run_identity[0]['identity'] == identity
                    and run_identity[0].get('fake') is False,
                    'identity audit / real engine run')
        # retained-manifest integrity: a payload changed after closeout
        # fails here even with untouched sequence numbers
        errors.extend(check_manifest(Path(run_dir)))
        # eligibility (does NOT fail the audit — it classifies dataset use)
        if outcome != 'completed':
            eligibility.append(f'outcome={outcome} — not decision-eligible '
                               '(decision samples require completed runs)')
        if final_interval == 'open':
            eligibility.append('final interval open (unpaired trailing '
                               'HUMAN_TURN_START observed)')
        inconsistent = [s for s in records
                        if s['kind'] == 'SPECTATOR_SNAPSHOT'
                        and isinstance(s.get('digest'), dict)
                        and s['digest'].get('consistent') is False]
        for s in inconsistent:
            eligibility.append(
                f'snapshot at seq {s.get("seq")} consistent=false '
                '(census observed board movement mid-read)')
        if isinstance(summary.get('trace_gaps'), int) and summary['trace_gaps']:
            eligibility.append(
                f"trace_gaps={summary['trace_gaps']} — rounds lost to ring "
                'wrap are absent from the record')
        # CAP-R1 #11: CAP-01's gap channels must reach the eligibility
        # layer too — a run can have zero ring wraps and matching turn
        # labels yet still carry engine-jump gaps / gap-inferred rounds
        jump_gaps = [r for r in records if r.get('audit') == 'capture_gap']
        if jump_gaps:
            eligibility.append(
                f'capture_gap audits={len(jump_gaps)} — the engine advanced '
                'between observations (turns inside the gap are unobserved)')
        gap_inferred = [r for r in records
                        if r.get('kind') == 'HUMAN_TURN_START'
                        and r.get('boundary') == 'gap_inferred']
        if gap_inferred:
            eligibility.append(
                f'gap_inferred rounds={len(gap_inferred)} — coarse interval '
                'attribution (round opened without a directly observed hook)')
        # a START->END turn span is the honest signature of a lost turn
        # boundary: one interval covers two engine turns (observed live:
        # ring-wrap gaps). Reported, never counted as a structural defect.
        snaps = [r for r in records if r['kind'] == 'SPECTATOR_SNAPSHOT']
        ends_ = [r for r in records if r['kind'] == 'HUMAN_TURN_END']
        for i in range(min(len(snaps), len(ends_))):
            if snaps[i].get('turn') != ends_[i].get('turn'):
                eligibility.append(
                    f"round {i + 1} spans turns {snaps[i].get('turn')}"
                    f"->{ends_[i].get('turn')} — capture-gap signature "
                    '(a lost turn boundary made one interval cover two turns)')
    else:
        eligibility.append('running prefix: no summary — sealed-prefix '
                           'structural shape only')

    decision_eligible = (not errors and outcome == 'completed'
                         and final_interval == 'closed' and not eligibility)
    return dict(status='FAIL' if errors else 'PASS', errors=errors,
                profile='full', outcome=outcome,
                final_interval=final_interval,
                decision_eligible=decision_eligible,
                eligibility_notes=eligibility,
                completed_rounds=summary.get('completed_rounds'),
                structural_replay='structural audit only; no driven tool '
                                  'calls to re-execute, no state comparison',
                reexecution_performed=False,
                comparison_result='not_performed',
                operator=summary.get('operator'))


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
