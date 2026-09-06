"""Read-only, loopback browser view of durable arena events.

No game, tuner, provider, or input controller is imported or contacted. A recent
event means only recent log activity; the dashboard does not infer process life.
"""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import ipaddress
import json
import math
import os
import re
import socket
import stat
import threading
from collections import defaultdict, deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

MAX_LOG_BYTES = 32 * 1024 * 1024
MAX_SUMMARY_BYTES = 1024 * 1024
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_EVENTS = 100_000
MAX_RUNS = 200
MAX_INDEX_BYTES = 64 * 1024 * 1024
MAX_TURNS = 120
MAX_CALLS = 128
MAX_NOTES = 32
NOTE_TOOLS = frozenset({'write_diary', 'set_goal', 'record_prediction', 'record_lesson'})
STATIC_FILES = {'/': 'index.html', '/index.html': 'index.html', '/app.js': 'app.js',
                '/style.css': 'style.css', '/styles.css': 'styles.css',
                '/favicon.svg': 'favicon.svg'}
SENSITIVE = re.compile(r'api.?key|password|secret|credential|authorization|cookie|'
                       r'access.?token|refresh.?token|auth.?token|private.?key', re.I)


class InvalidRun(ValueError):
    """A run identifier or artifact is outside the read-only surface."""


def timestamp(value):
    if not isinstance(value, str):
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.replace('Z', '+00:00'))
        return parsed.timestamp() if parsed.tzinfo is not None else None
    except (ValueError, OverflowError, OSError):
        return None


def finite_number(value):
    return type(value) is int or (type(value) is float and math.isfinite(value))


class Redactor:
    def __init__(self):
        self.secrets = sorted({v for k, v in os.environ.items() if v and
                               (SENSITIVE.search(k) or k.upper().endswith('_TOKEN'))},
                              key=len, reverse=True)

    def text(self, value):
        for secret in self.secrets:
            value = value.replace(secret, '[redacted]')
        return value[:4096] + ('…[truncated]' if len(value) > 4096 else '')

    def clean(self, value, depth=0):
        if depth > 6:
            return '[depth limited]'
        if isinstance(value, str):
            return self.text(value)
        if isinstance(value, dict):
            result = {}
            for key, item in list(value.items())[:40]:
                key = str(key)
                result[self.text(key)] = ('[redacted]' if SENSITIVE.search(key) or
                                         key.lower() in ('key', 'token')
                                         else self.clean(item, depth + 1))
            if len(value) > 40:
                result['_truncated'] = True
            return result
        if isinstance(value, list):
            result = [self.clean(x, depth + 1) for x in value[:40]]
            return result + (['[list truncated]'] if len(value) > 40 else [])
        if value is None or isinstance(value, bool) or finite_number(value):
            return value
        return '[unsupported value]'


def read_artifact(run: Path, name: str, limit: int):
    """Open only a regular direct child, refusing symlinks even during races."""
    directory = os.open(run, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        try:
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        except FileNotFoundError:
            return None, False
        with os.fdopen(fd, 'rb') as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise InvalidRun('artifact is not a regular file')
            raw = stream.read(limit + 1)
        return raw[:limit], len(raw) > limit
    finally:
        os.close(directory)


def parse_json(raw):
    def reject_constant(value):
        raise ValueError('non-finite JSON number')
    return json.loads(raw, parse_constant=reject_constant)


class DashboardStore:
    def __init__(self, runs_root: Path):
        self.root = Path(runs_root).resolve(strict=True)
        if not self.root.is_dir():
            raise ValueError('runs root is not a directory')
        self.index_cache = {}
        self.cache_lock = threading.Lock()

    def resolve_run(self, run_id):
        if not isinstance(run_id, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,159}',
                                                         run_id):
            raise InvalidRun('invalid run id')
        run = self.root / run_id
        if run.is_symlink():
            raise InvalidRun('symlink run directories are unavailable')
        if run.resolve().parent != self.root or not run.is_dir():
            raise InvalidRun('run not found')
        return run

    def load(self, run_id, *, now=None):
        run = self.resolve_run(run_id)
        warnings, broken, events = [], False, []
        redactor = Redactor()
        now = dt.datetime.now(dt.UTC).timestamp() if now is None else now
        try:
            raw, limited = read_artifact(run, 'events.jsonl', MAX_LOG_BYTES)
            if raw is None:
                warnings.append('Event log unavailable.')
                broken = True
            else:
                if limited:
                    warnings.append('Event log exceeds the read limit; this view is incomplete.')
                    broken = True
                lines = raw.splitlines()
                for index, line in enumerate(lines):
                    if not line.strip():
                        continue
                    if len(events) >= MAX_EVENTS:
                        warnings.append('Event count limit reached; this view is incomplete.')
                        broken = True
                        break
                    try:
                        record = parse_json(line)
                    except (ValueError, UnicodeError, RecursionError):
                        if index == len(lines) - 1 and not raw.endswith(b'\n') and not limited:
                            warnings.append('Partial trailing JSON ignored while log is written.')
                        else:
                            warnings.append(f'Malformed event line {index + 1}; view incomplete.')
                            broken = True
                        break
                    if not isinstance(record, dict) or type(record.get('seq')) is not int \
                            or record['seq'] != len(events):
                        warnings.append(f'Event sequence invalid at line {index + 1}.')
                        broken = True
                        break
                    events.append(record)
            raw_summary, limited_summary = read_artifact(run, 'summary.json', MAX_SUMMARY_BYTES)
            summary = None
            if raw_summary is not None:
                try:
                    summary = parse_json(raw_summary)
                    if not isinstance(summary, dict) or limited_summary:
                        raise ValueError('invalid summary')
                except (ValueError, UnicodeError, RecursionError):
                    warnings.append('Summary is incomplete or invalid.')
                    broken = True
                    summary = None
        except (OSError, InvalidRun):
            warnings.append('Artifact unavailable or outside the read-only surface.')
            broken, summary = True, None
        starts = [e for e in events if e.get('kind') == 'MATCH_START']
        ends = [e for e in events if e.get('kind') == 'MATCH_END']
        if len(starts) != 1 or not events or events[0].get('kind') != 'MATCH_START':
            warnings.append('A single initial MATCH_START was not observed.')
            broken = True
        last_ts = events[-1].get('ts') if events else None
        last_time = timestamp(last_ts)
        if last_time is None and events:
            warnings.append('Last event timestamp is unavailable.')
        if ends:
            if len(ends) != 1 or events[-1].get('kind') != 'MATCH_END':
                warnings.append('Terminal event structure is invalid.')
                broken = True
            terminal = ends[-1].get('summary')
            if not isinstance(terminal, dict) or summary != terminal:
                warnings.append('Event terminal summary and summary.json do not match.')
                broken = True
            status = ('incomplete' if broken else 'aborted' if terminal.get('aborted') or
                      terminal.get('clean') is False else 'completed')
            if isinstance(terminal, dict) and (terminal.get('aborted') or
                                               terminal.get('failure_reason')):
                reason = terminal.get('failure_reason') or terminal['aborted']
                warnings.append('Run stopped: ' + redactor.text(str(reason)))
        else:
            if summary is not None:
                warnings.append('Summary exists without a MATCH_END event.')
                broken = True
            recent = last_time is not None and 0 <= now - last_time < 30
            status = 'incomplete' if broken else 'running' if recent else 'stalled'
            warnings.append('No MATCH_END: activity age is not process-liveness proof.')
        try:
            payload = project_events(events, warnings, redactor)
        except (ValueError, TypeError, KeyError, AttributeError, RecursionError):
            warnings.append('Malformed event payload shape; run view incomplete.')
            payload = project_events([], warnings, redactor)
            payload['_incomplete'] = True
        if payload.pop('_incomplete'):
            status = 'incomplete'
        if status == 'completed' and isinstance(summary, dict):
            observed = payload['metrics']
            claimed = summary.get('completed_rounds')
            if (type(claimed) is int and claimed != observed['completed_rounds']) or (
                    2 <= len(payload['agents']) <= 4 and
                    observed['completed_seat_turns'] % len(payload['agents'])):
                status = 'incomplete'
                payload['warnings'].append(
                    'Completed turn evidence conflicts with terminal outcome.')
        for turn in payload['turns']:
            if turn['status'] == 'active' and status != 'running':
                turn['status'] = 'incomplete' if status == 'completed' else status
                if status in ('completed', 'aborted', 'incomplete'):
                    for call in turn['calls']:
                        if call['status'] == 'pending':
                            call['status'] = 'incomplete'
        payload.update(id=redactor.text(run_id), status=status,
                       last_event_at=redactor.text(last_ts) if last_time is not None else None)
        return bound_response(payload)

    def list_runs(self):
        candidates = []
        # Limit directory inspection too; never descend into arbitrary evidence folders.
        with os.scandir(self.root) as entries:
            for index, entry in enumerate(entries):
                if index >= 5000:
                    break
                if not entry.is_dir(follow_symlinks=False):
                    continue
                try:
                    event = Path(entry.path) / 'events.jsonl'
                    if event.is_symlink() or not event.is_file():
                        continue
                    candidates.append((event.stat().st_mtime, entry.name))
                except OSError:
                    continue
        runs, read_bytes = [], 0
        for _, name in sorted(candidates, reverse=True)[:MAX_RUNS]:
            try:
                run = self.resolve_run(name)
                event_stat = (run / 'events.jsonl').stat()
                summary = run / 'summary.json'
                summary_stat = summary.stat() if summary.is_file() else None
                signature = (event_stat.st_mtime_ns, event_stat.st_size,
                             summary_stat.st_mtime_ns if summary_stat else None,
                             summary_stat.st_size if summary_stat else None)
                with self.cache_lock:
                    cached = self.index_cache.get(name)
                if cached and cached[0] == signature:
                    result = dict(cached[1])
                    last = timestamp(result['last_event_at'])
                    if result['status'] == 'running' and (last is None or
                            dt.datetime.now(dt.UTC).timestamp() - last >= 30):
                        result['status'] = 'stalled'
                else:
                    read_bytes += min(event_stat.st_size, MAX_LOG_BYTES) + MAX_SUMMARY_BYTES
                    if read_bytes > MAX_INDEX_BYTES:
                        break
                    full = self.load(name)
                    result = {key: full[key] for key in ('id', 'last_event_at', 'status')}
                    with self.cache_lock:
                        self.index_cache[name] = (signature, result)
            except (InvalidRun, OSError, ValueError, TypeError, KeyError, RecursionError):
                continue
            runs.append({'id': result['id'], 'updated_at': result['last_event_at'],
                         'status': result['status']})
        with self.cache_lock:
            if len(self.index_cache) > MAX_RUNS * 2:
                keep = {row['id'] for row in runs}
                self.index_cache = {key: value for key, value in self.index_cache.items()
                                    if key in keep}
        return {'runs': runs}


def project_events(events, warnings, redactor):
    agents, turns, pending, completed = {}, {}, defaultdict(deque), []
    declared_agents = None
    roster_invalid = False
    requests, violations, incomplete = {}, 0, False
    has_completion_audit = any(e.get('audit') in ('completed_seat_turn', 'run_identity')
                               for e in events)

    def event_time(event):
        value = event.get('ts')
        return redactor.text(value) if timestamp(value) is not None else None

    def configure_roster(config, *, identity=False):
        nonlocal incomplete, declared_agents, roster_invalid
        if isinstance(config, dict) and 'agents' not in config:
            return  # Missing evidence is distinct from an explicit malformed roster.
        entries = config.get('agents') if isinstance(config, dict) else None
        candidate, models = {}, {}
        valid = isinstance(entries, list) and 2 <= len(entries) <= 4
        for item in entries if isinstance(entries, list) else []:
            if identity and isinstance(item, dict):
                aid, pid = item.get('agent_id'), item.get('player_id')
                llm = item.get('llm')
                model = llm.get('model_id') if isinstance(llm, dict) else item.get('model')
            elif not identity and isinstance(item, list) and len(item) >= 2:
                aid, pid, model = item[0], item[1], None
            else:
                valid = False
                continue
            if (not isinstance(aid, str) or not aid or type(pid) is not int
                    or not 0 <= pid <= 63 or aid in candidate):
                valid = False
                continue
            candidate[aid], models[aid] = pid, model
        if len(set(candidate.values())) != len(candidate):
            valid = False
        if not valid:
            warnings.append('Malformed agent configuration in event log.')
            incomplete = roster_invalid = True
            return
        if declared_agents is not None and candidate != declared_agents:
            warnings.append('Configured agent roster changed within the run.')
            incomplete = roster_invalid = True
            return
        declared_agents = candidate
        for aid, pid in candidate.items():
            agent(aid, pid, models[aid])

    def agent(agent_id, player_id, model=None):
        nonlocal incomplete
        if not isinstance(agent_id, str) or type(player_id) is not int:
            return
        if agent_id in agents and agents[agent_id]['player_id'] != player_id:
            warnings.append('Agent identity changed seats within the run.')
            incomplete = True
            return
        current = agents.setdefault(agent_id, {'agent_id': redactor.text(agent_id),
                                              'player_id': player_id, 'model': None})
        if isinstance(model, str):
            current['model'] = redactor.text(model)

    def seat(event, agent_id=None):
        aid = agent_id or event.get('agent_id')
        pid = event.get('player_id', event.get('phase_player_id'))
        if pid is None or pid == -1:
            pid = agents.get(aid, {}).get('player_id')
        turn = event.get('turn')
        if not isinstance(aid, str) or type(pid) is not int or type(turn) is not int:
            return None
        key = (turn, pid, aid)
        if key not in turns:
            turns[key] = {'turn': turn, 'player_id': pid, 'agent_id': redactor.text(aid),
                          'started_at': event_time(event), 'ended_at': None, 'status': 'active',
                          'elapsed_s': None, 'requests': 0, 'calls': [], 'notes': [],
                          'strategy': None, 'scouting_graph': None}
        agent(aid, pid)
        return turns[key]

    for event in events:
        kind, audit = event.get('kind'), event.get('audit')
        if kind == 'MATCH_START':
            configure_roster(event.get('config', {}))
        elif kind == 'HEARTBEAT' and audit == 'run_identity':
            identity = event.get('identity')
            config = identity.get('config', {}) if isinstance(identity, dict) else {}
            configure_roster(config, identity=True)
        elif kind == 'HEARTBEAT' and audit == 'provider_request':
            aid, count = event.get('agent'), event.get('posts_sent')
            if not isinstance(aid, str) or type(count) is not int or count < 0:
                warnings.append('Invalid provider request counter.')
                continue
            old = requests.get(aid, 0)
            if count < old:
                warnings.append('Provider request counter moved backwards.')
            turn = seat(event, aid)
            if turn is not None:
                turn['requests'] += max(0, count - old)
            requests[aid] = max(old, count)
        elif kind == 'HEARTBEAT' and audit in ('strategy_execution', 'strategy_graph'):
            if 'strategy_payload_json' in event:
                try:
                    payload = parse_json(event['strategy_payload_json'])
                    if not isinstance(payload, dict):
                        raise ValueError('strategy payload must be an object')
                except (ValueError, TypeError, RecursionError):
                    warnings.append('Strategy payload JSON is malformed.')
                    incomplete = True
                    continue
                # Display fields may be encoded for deterministic checkpoint
                # compatibility. Identity always comes from the outer event.
                event = dict(event, **{key: payload[key] for key in (
                    'source', 'reasons', 'last_decision_turn', 'seed', 'directive',
                    'persistence', 'cadence', 'opening_frozen_unit_ids', 'graph')
                                      if key in payload})
            turn = seat(event)
            if turn is None:
                warnings.append('Strategy audit missing agent/seat/turn identity.')
                incomplete = True
                continue
            if audit == 'strategy_execution':
                turn['strategy'] = redactor.clean({key: event.get(key) for key in (
                    'source', 'reasons', 'last_decision_turn', 'seed', 'directive',
                    'persistence', 'cadence', 'opening_frozen_unit_ids')})
            elif isinstance(event.get('graph'), dict):
                turn['scouting_graph'] = redactor.clean(event['graph'])
            else:
                warnings.append('Scouting audit has no graph object.')
                incomplete = True
        elif kind in ('LEASE_GRANT', 'TOOL_CALL', 'TOOL_RESULT', 'TURN_END'):
            turn = seat(event)
            if turn is None:
                warnings.append('Event missing agent/seat/turn identity.')
                continue
            if kind == 'LEASE_GRANT':
                turn['started_at'] = event_time(event)
            elif kind == 'TURN_END':
                if not has_completion_audit:
                    turn['status'], turn['ended_at'] = 'completed', event_time(event)
                    completed.append((turn['turn'], turn['player_id'], turn['agent_id']))
            else:
                if not isinstance(event.get('tool'), str):
                    warnings.append('Tool event is missing a tool name.')
                    incomplete = True
                    continue
                key = (event.get('agent_id'), event.get('turn'),
                       event.get('player_id'), event.get('tool'))
                if kind == 'TOOL_CALL':
                    call = {'seq': event['seq'], 'tool': redactor.clean(event.get('tool')),
                            'args': redactor.clean(event.get('args')), 'status': 'pending',
                            'result': None, 'ts': event_time(event), 'ended_at': None}
                    turn['calls'].append(call)
                    pending[key].append(call)
                elif not pending[key]:
                    warnings.append(f'Unmatched TOOL_RESULT at seq {event["seq"]}.')
                    incomplete = True
                else:
                    call = pending[key].popleft()
                    call['status'] = redactor.clean(event.get('status'))
                    call['ended_at'] = event_time(event)
                    result = {k: event[k] for k in ('result_doc', 'observed', 'rejection')
                              if event.get(k) is not None}
                    call['result'] = redactor.clean(result)
                    args = call['args']
                    if call['tool'] in NOTE_TOOLS and call['status'] == 'accepted' \
                            and isinstance(args, dict) and isinstance(args.get('text'), str):
                        turn['notes'].append({'seq': call['seq'], 'tool': call['tool'],
                                              'text': args['text'],
                                              'confidence': args.get('confidence'),
                                              'status': 'accepted'})
        elif kind == 'HEARTBEAT' and audit == 'completed_seat_turn':
            row = event.get('row')
            if not isinstance(row, dict) or row.get('lease_released') is not True:
                warnings.append('Completed-seat audit lacks observed lease release.')
                incomplete = True
                continue
            turn = seat(dict(event, turn=row.get('turn'), player_id=row.get('player'),
                             agent_id=row.get('agent')))
            if turn is not None:
                turn.update(status='completed', ended_at=event_time(event),
                            elapsed_s=row.get('elapsed_s') if
                            finite_number(row.get('elapsed_s')) else None)
                completed.append((turn['turn'], turn['player_id'], turn['agent_id']))
        elif kind == 'VIOLATION':
            violations += 1

    complete_ids = set(completed)
    if len(complete_ids) != len(completed):
        warnings.append('Duplicate completed seat turns observed.')
        incomplete = True
    if completed and not has_completion_audit:
        warnings.append('Legacy TURN_END counts; driver completion/lease audit unavailable.')
    observed_seats = sorted({a['player_id'] for a in agents.values()})
    if len(observed_seats) != len(agents):
        warnings.append('Multiple agent identities occupy the same seat.')
        incomplete = True
    if declared_agents is not None and any(
            declared_agents.get(aid) != value['player_id'] for aid, value in agents.items()):
        warnings.append('Observed seat identity is outside the configured roster.')
        incomplete = True
    if has_completion_audit:
        # Observations may display useful evidence but cannot define the roster
        # required to certify completed engine rounds.
        seats = sorted(declared_agents.values()) if declared_agents and not roster_invalid else []
        if completed and not seats:
            warnings.append('Completed-seat audits require an explicit valid configured roster.')
            incomplete = True
    else:
        # Retain the historical two-seat TURN_END display with its existing
        # missing-driver-audit warning. It is not four-seat completion proof.
        seats = observed_seats if len(observed_seats) == 2 else []
        if completed and not seats:
            warnings.append('Legacy round counting requires two observed seats.')
            incomplete = True
    rounds, expected_turn, offset = 0, None, 0
    if 2 <= len(seats) <= 4:
        for number, pid, _ in completed:
            expected_turn = number if expected_turn is None else expected_turn
            if pid != seats[offset] or number != expected_turn:
                warnings.append('Completed seat order does not form consecutive '
                                'configured-seat rounds.')
                incomplete = True
                break
            offset += 1
            if offset == len(seats):
                rounds, expected_turn, offset = rounds + 1, expected_turn + 1, 0
    result_turns = list(turns.values())
    for turn in result_turns:
        start, end = timestamp(turn['started_at']), timestamp(turn['ended_at'])
        if turn['elapsed_s'] is None and start is not None and end is not None:
            turn['elapsed_s'] = max(0, end - start)
        if len(turn['calls']) > MAX_CALLS or len(turn['notes']) > MAX_NOTES:
            warnings.append('Per-turn display limit reached; older items omitted.')
        turn['calls'] = turn['calls'][-MAX_CALLS:]
        turn['notes'] = turn['notes'][-MAX_NOTES:]
    if len(result_turns) > MAX_TURNS:
        warnings.append('Turn display limit reached; older turns omitted.')
    return {'agents': list(agents.values())[:16], 'turns': result_turns[-MAX_TURNS:],
            'metrics': {'completed_rounds': rounds, 'completed_seat_turns': len(complete_ids),
                        'requests': sum(requests.values()), 'violations': violations},
            'warnings': list(dict.fromkeys(warnings))[:40], '_incomplete': incomplete}


def bound_response(payload):
    while len(json.dumps(payload, allow_nan=False).encode()) > MAX_RESPONSE_BYTES:
        if not payload['turns']:
            raise ValueError('Response exceeds size limit')
        payload['turns'].pop(0)
        warning = 'Response size limit reached; older turns omitted.'
        if warning not in payload['warnings']:
            payload['warnings'].append(warning)
    return payload


def loopback(host):
    if host == 'localhost':
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def create_server(runs_root: Path, host='127.0.0.1', port=8788, *, static_root=None):
    if not loopback(host):
        raise ValueError('Dashboard binds loopback only')
    store = DashboardStore(runs_root)
    assets = Path(static_root or Path(__file__).with_name('dashboard_static')).resolve()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            # Do not copy untrusted paths/query values into a terminal log.
            return

        def respond(self, code, body, content_type='application/json; charset=utf-8',
                    *, head=False):
            self.send_response(code)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Content-Security-Policy', "default-src 'self'; object-src 'none'; "
                             "frame-ancestors 'none'; base-uri 'none'")
            self.end_headers()
            if not head:
                self.wfile.write(body)

        def serve(self, head=False):
            try:
                authority = urlsplit('//' + self.headers.get('Host', '')).hostname
                if not authority or not loopback(authority):
                    self.respond(403, b'{"error":"loopback Host required"}', head=head)
                    return
                parsed = urlsplit(self.path)
                if parsed.path == '/api/runs':
                    payload = store.list_runs()
                elif parsed.path == '/api/run':
                    query = parse_qs(parsed.query, max_num_fields=4)
                    ids = query.get('id', [])
                    if len(ids) != 1:
                        raise InvalidRun('one run id required')
                    payload = store.load(ids[0])
                elif parsed.path in STATIC_FILES:
                    file = assets / STATIC_FILES[parsed.path]
                    if file.is_symlink() or file.resolve().parent != assets:
                        raise InvalidRun('static asset unavailable')
                    body, limited = read_artifact(assets, file.name, MAX_RESPONSE_BYTES)
                    if body is None or limited:
                        raise InvalidRun('static asset unavailable')
                    content_type = {'.html': 'text/html', '.css': 'text/css',
                                    '.js': 'text/javascript', '.svg': 'image/svg+xml'}[
                                        file.suffix] + '; charset=utf-8'
                    self.respond(200, body, content_type, head=head)
                    return
                else:
                    raise InvalidRun('route unavailable')
                self.respond(200, json.dumps(payload, allow_nan=False).encode(), head=head)
            except (InvalidRun, ValueError, OSError, TypeError, KeyError, RecursionError):
                self.respond(404, b'{"error":"resource unavailable"}', head=head)

        def do_GET(self):
            self.serve()

        def do_HEAD(self):
            self.serve(head=True)

        def reject_write(self):
            self.respond(405, b'{"error":"read-only dashboard"}')

        do_POST = do_PUT = do_PATCH = do_DELETE = do_OPTIONS = reject_write

    class Server(ThreadingHTTPServer):
        daemon_threads = True
        address_family = socket.AF_INET6 if ':' in host else socket.AF_INET

    return Server((host, port), Handler)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--runs-root', type=Path, default=Path('runs'))
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=8788)
    args = parser.parse_args()
    with create_server(args.runs_root, args.host, args.port) as server:
        print(f'Read-only arena dashboard: http://{args.host}:{server.server_port}', flush=True)
        with contextlib.suppress(KeyboardInterrupt):
            server.serve_forever()


if __name__ == '__main__':
    main()
