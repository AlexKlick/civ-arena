"""Lane V integrated browser proof: the spectator world territory layer.

Runs with /usr/bin/python3 and headless /usr/bin/google-chrome against an
ephemeral loopback dashboard server (`dashboard.create_server(runs_root,
port=0)`) over a COPY of the retained run `minimax100-20260907T183425Z`.
The original run directory is read only and never modified.

The M4 producer (Lane P) is not on this branch, so the world is the committed
schema fixture `tests/fixtures/spectator_world_sample.json`, translated into
the run's observed coordinate area and injected as world-carrying records:
  - a SPECTATOR_SNAPSHOT carrier at turn 30 (older), and
  - the `spectator_world` audit at turn 34 (the record the page must show).
Both go into a DERIVED copy of the event log (`<run>-m4world`) right after the
last retained strategy_request, with the seq chain renumbered; the copied run
itself is untouched. The bundle is ALSO materialized through the Python API
(`DashboardStore.load_map` -> `minimap.render`) into the evidence dir, per the
lane spec.

Checks (each recomputed against the page's own embedded #data JSON where the
DOM can drift): territory tints/frontier paths with data-owner attributes; the
legend toggle hides/shows ONLY the world layer; the roster panel lists the
city-state; the two provenance rows verbatim; the fog counters; the city-state
city badge with its capital star; the player route carries NO world layer; zero
page errors, zero console script errors, same-origin requests only.

Writes lane-v-smoke-summary.json and the territory-1440.png / territory-390.png
/ roster-panel.png screenshots beside this script.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from copy import deepcopy
from pathlib import Path

ROOT = Path(__file__).resolve().parent
REPO = ROOT.parents[1]
sys.path.insert(0, str(REPO / 'src'))
sys.path.insert(0, str(REPO / 'tests'))

# The harness HOME may be a per-instance directory; playwright lives in the real
# user site. Insert it before importing (same pattern the M2/M3 proof used via
# HOME=/home/alexk).
try:
    from playwright.sync_api import sync_playwright  # noqa: E402
except ImportError:  # pragma: no cover - environment bootstrap
    for site in ('/home/alexk/.local/lib/python3.12/site-packages',):
        if os.path.isdir(site) and site not in sys.path:
            sys.path.append(site)
    from playwright.sync_api import sync_playwright  # noqa: E402

from civ_arena import dashboard, minimap  # noqa: E402
from test_minimap import world_event  # noqa: E402

REAL_RUN = Path('/home/alexk/civ-arena-reliability-20260904/runs/'
                'minimax100-20260907T183425Z')
RUN_ID = REAL_RUN.name
DERIVED_ID = f'{RUN_ID}-m4world'
FIXTURE = REPO / 'tests' / 'fixtures' / 'spectator_world_sample.json'
# Translate the fixture cluster (q 0..8, r 0..5) into the run's observed area
# (P0's turn-34 territory sits around q 41..45, r 13..16).
OFFSET = (41, 12)
WORLD_TURN = 34
OLDER_TURN = 30

TERRITORY_ROW = 'Territory: spectator capture at seat 1 · turn 34'
OBSERVED_ROW = 'Observed ownership: seat packets'
TOGGLE_LABEL = 'Spectator territory (omniscient capture)'


def head_commit():
    try:
        done = subprocess.run(['git', '-C', str(REPO), 'rev-parse', '--short', 'HEAD'],
                              capture_output=True, text=True, timeout=15, check=False)
        return done.stdout.strip() or 'unknown'
    except OSError:
        return 'unknown'


def translate_world(world, offset):
    """Move every fixture coordinate by a constant offset (data copy, no reuse)."""
    dq, dr = offset
    moved = deepcopy(world)
    for rows in moved['owned_tiles_columns'].values():
        for row in rows:
            row['q'] += dq
            row['r'] += dr
    for city in moved['cities']:
        city['q'] += dq
        city['r'] += dr
    coords = []
    for value in moved['fog_audit']['disagree_coords']:
        q, r = (int(piece) for piece in value.split(','))
        coords.append(f'{q + dq},{r + dr}')
    moved['fog_audit']['disagree_coords'] = coords
    return moved


def prepare(workspace):
    """Copy the retained run and derive the world-carrying variant."""
    fixture = json.loads(FIXTURE.read_text())
    world = translate_world(fixture, OFFSET)
    runs_root = workspace / 'runs'
    runs_root.mkdir()
    copied = runs_root / RUN_ID
    copied.mkdir()
    for source in sorted(REAL_RUN.iterdir()):
        if source.is_file():
            shutil.copy(source, copied / source.name)
    lines = (REAL_RUN / 'events.jsonl').read_text().splitlines()
    events = [json.loads(line) for line in lines]
    template = next(e for e in events if e.get('audit') == 'strategy_request')
    insert_at = max(i for i, e in enumerate(events)
                    if e.get('audit') == 'strategy_request') + 1
    records = [
        world_event(0, OLDER_TURN, world, kind='SPECTATOR_SNAPSHOT', audit=None),
        world_event(0, WORLD_TURN, world),
    ]
    for record in records:
        for key in ('match_id', 'game_instance_id'):
            record[key] = template[key]
        record['ts'] = template.get('ts')
    derived = events[:insert_at] + records + events[insert_at:]
    renumbered = [dict(event, seq=index) for index, event in enumerate(derived)]
    (runs_root / DERIVED_ID).mkdir()
    (runs_root / DERIVED_ID / 'events.jsonl').write_text(
        '\n'.join(json.dumps(event) for event in renumbered) + '\n')
    return runs_root, world, {'records_injected': len(records),
                              'insert_at_seq': insert_at,
                              'source_events': len(events)}


def serve(runs_root):
    server = dashboard.create_server(runs_root, port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread, f'http://127.0.0.1:{server.server_port}/'


class Checks:
    def __init__(self):
        self.results = []

    def run(self, group, name, body):
        started = time.time()
        try:
            detail = body()
            row = {'group': group, 'check': name, 'result': 'pass', 'detail': detail}
        except Exception as error:                                        # noqa: BLE001
            row = {'group': group, 'check': name, 'result': 'fail',
                   'detail': f'{type(error).__name__}: {error}'}
        row['seconds'] = round(time.time() - started, 2)
        self.results.append(row)
        print(f"[{row['result'].upper():4}] {group} · {name}\n       {row['detail']}",
              flush=True)
        return row['result'] == 'pass'

    @property
    def failed(self):
        return [row for row in self.results if row['result'] != 'pass']


def settle(page, body, timeout=20.0):
    deadline = time.time() + timeout
    while True:
        try:
            return body()
        except AssertionError:
            if time.time() > deadline:
                raise
            page.wait_for_timeout(250)


def main():
    head_before = head_commit()
    workspace = Path(tempfile.mkdtemp(prefix='observer-m4v-smoke-'))
    runs_root, world, manifest_bits = prepare(workspace)
    checks = Checks()
    errors, notices, requests, shots, facts = [], [], [], {}, {}
    server, thread, origin = serve(runs_root)
    facts['origin'] = origin

    # ------------------------------------------------- Python API (lane spec)
    def python_api():
        store = dashboard.DashboardStore(runs_root)
        bundle = store.load_map(DERIVED_ID, spectator=True, turn=WORLD_TURN)
        assert 'world' in bundle, 'spectator bundle carries no world'
        receipt = bundle['world']['receipt']
        assert receipt['source'] == 'spectator_world' and receipt['turn'] == WORLD_TURN, \
            receipt
        assert bundle['world']['after_seat'] == 1
        counts = {owner: len(rows) for owner, rows
                  in bundle['world']['owned_tiles_columns'].items()}
        assert counts == {'0': 6, '1': 5, '12': 12}, counts
        player = store.load_map(DERIVED_ID, player=0, turn=WORLD_TURN)
        assert 'world' not in player, 'player route attached a world'
        for name, doc in (('atlas-spectator.html', bundle), ('atlas-player.html', player)):
            target = ROOT / name
            if target.exists():
                target.unlink()
            target.write_text(minimap.render(doc))
        facts['python_api'] = {'receipt': receipt, 'owners': counts,
                               'spectator_html_bytes': (ROOT / 'atlas-spectator.html').stat().st_size,
                               'player_html_bytes': (ROOT / 'atlas-player.html').stat().st_size}
        return (f'spectator bundle world receipt {receipt}, owner columns {counts}; '
                f'player bundle has none; both pages rendered through '
                f'minimap.render into the evidence dir '
                f'({facts["python_api"]["spectator_html_bytes"]} / '
                f'{facts["python_api"]["player_html_bytes"]} bytes)')

    checks.run('V1', 'Python API attaches the world on the spectator route only',
               python_api)

    try:
        with sync_playwright() as play:
            browser = play.chromium.launch(executable_path='/usr/bin/google-chrome',
                                           headless=True,
                                           args=['--no-sandbox', '--disable-dev-shm-usage'])
            try:
                page = browser.new_page(viewport={'width': 1440, 'height': 1080})
                page.set_default_timeout(30000)

                def track_console(message, tag):
                    if message.type != 'error':
                        return
                    row = {'page': tag, 'text': message.text,
                           'url': (message.location or {}).get('url', '')}
                    if message.text.startswith('Failed to load resource'):
                        notices.append(row)
                    else:
                        errors.append({'kind': 'console', **row})

                def watch(target, tag):
                    target.on('pageerror', lambda error: errors.append(
                        {'kind': 'pageerror', 'page': tag, 'text': str(error)}))
                    target.on('console', lambda message: track_console(message, tag))
                    target.on('request', lambda request: requests.append(
                        {'page': tag, 'url': request.url}))

                watch(page, 'spectator')
                page.goto(f'{origin}map?id={DERIVED_ID}&spectator=1&turn={WORLD_TURN}',
                          wait_until='load')
                page.wait_for_selector('#legend-groups .legend-group', timeout=30000)

                def world_data():
                    return json.loads(page.locator('#data').text_content())['world']

                # ------------------------------------------------------- V2
                def territory_paths():
                    data = world_data()
                    assert data and data['receipt']['turn'] == WORLD_TURN, data['receipt']
                    expected = {owner for owner, rows
                                in data['owned_tiles_columns'].items() if rows}
                    tints = page.eval_on_selector_all(
                        '#world .tint.world', 'nodes => nodes.map(n => n.getAttribute("data-owner"))')
                    borders = page.eval_on_selector_all(
                        '#world .border.world',
                        'nodes => nodes.map(n => ({owner: n.getAttribute("data-owner"),'
                                  ' width: getComputedStyle(n).strokeWidth,'
                                  ' fill: getComputedStyle(n).fill,'
                                  ' segs: Number(n.getAttribute("data-segments"))}))')
                    assert set(tints) == expected, (tints, expected)
                    assert {row['owner'] for row in borders} == expected, borders
                    assert all(row['width'] == '2.6px' for row in borders), borders
                    assert all(row['fill'] == 'none' for row in borders), borders
                    assert all(row['segs'] > 0 for row in borders), borders
                    stroke = page.eval_on_selector_all(
                        '#world .border.world',
                        'nodes => nodes.map(n => n.getAttribute("stroke"))')
                    assert all(s.startswith('rgb(') for s in stroke), stroke
                    facts['territory'] = {'owners': sorted(expected), 'strokes': sorted(stroke),
                                          'segments': {row['owner']: row['segs']
                                                       for row in borders}}
                    return (f'one tint + one frontier path per owner {sorted(expected)}, each '
                            f'data-owner addressed, computed stroke-width 2.6px, fill none, '
                            f'strokes {sorted(stroke)} (engine palette rgb() strings), '
                            f'{sum(row["segs"] for row in borders)} frontier segments')

                checks.run('V2', 'territory tints and frontier paths render with data-owner',
                           territory_paths)

                # ------------------------------------------------------- V3
                def toggle_only_world():
                    page.locator('#legend-groups input.legend-check').wait_for()
                    box = page.locator('#world-territory-toggle')
                    assert box.is_checked() is True
                    label = page.locator('label[for="world-territory-toggle"]').text_content()
                    assert label == TOGGLE_LABEL, label
                    before = page.locator('#world .tint.world').count()
                    box.uncheck()
                    hidden = settle(page, lambda: assert_layer('off'))
                    observed = page.eval_on_selector(
                        '#world .layer-borders',
                        'node => getComputedStyle(node).display')
                    assert observed != 'none', observed
                    box.check()
                    settle(page, lambda: assert_layer('on'))
                    facts['toggle'] = {'tints_before': before, 'hidden_state': hidden,
                                       'observed_layer_display': observed}
                    return (f'unchecking "{label}" set .layer-world to display:none while '
                            f'.layer-borders stayed {observed} and {before} tints were '
                            f'removed from paint; re-checking restored them')

                def assert_layer(state):
                    display = page.eval_on_selector('#world .layer-world',
                                                    'node => getComputedStyle(node).display')
                    if state == 'off':
                        assert display == 'none', display
                    else:
                        assert display != 'none', display
                    return display

                checks.run('V3', 'the toggle hides and shows ONLY the world layer',
                           toggle_only_world)

                # ------------------------------------------------------- V4
                def roster_panel():
                    rows = page.eval_on_selector_all(
                        '#roster .roster-row',
                        'nodes => nodes.map(n => ({id: n.querySelector(".roster-id").textContent,'
                                  ' civ: n.querySelector("strong").textContent,'
                                  ' kind: n.querySelector(".roster-kind").textContent,'
                                  ' suzerain: n.querySelector(".roster-suzerain").textContent,'
                                  ' alive: n.querySelector(".roster-alive").textContent}))')
                    assert len(rows) == 3, rows
                    geneva = next(row for row in rows if row['id'] == 'P12')
                    assert geneva['civ'] == 'GENEVA', geneva
                    assert geneva['kind'] == 'city_state', geneva
                    assert geneva['suzerain'] == 'suzerain P0', geneva
                    assert geneva['alive'] == 'alive', geneva
                    head = page.locator('#roster p').first.text_content()
                    assert 'seat 1 · turn 34' in head, head
                    facts['roster'] = rows
                    return (f'3 roster rows; the city-state reads {geneva}; the panel head '
                            f'prints "{head}"')

                checks.run('V4', 'roster panel lists the city-state with kind and suzerain',
                           roster_panel)

                # ------------------------------------------------------- V5
                def provenance_rows():
                    legend = page.locator('#legend-groups').inner_text()
                    assert TERRITORY_ROW in legend, legend
                    assert OBSERVED_ROW in legend, legend
                    fog = page.locator('#legend-groups').inner_text()
                    for needle in ('Fog audit (spectator capture)', 'engine visible 21',
                                   'disagreeing 2'):
                        assert needle in fog, needle
                    facts['legend_snippets'] = {
                        'territory_row': TERRITORY_ROW in legend,
                        'observed_row': OBSERVED_ROW in legend,
                        'fog': 'Fog audit (spectator capture)' in legend}
                    return (f'both provenance rows print verbatim ("{TERRITORY_ROW}", '
                            f'"{OBSERVED_ROW}") and the fog counters (engine visible 21 · '
                            'engine not visible 1 · unavailable 1 · disagreeing 2) sit in the '
                            'Provenance group')

                checks.run('V5', 'the two provenance rows print verbatim', provenance_rows)

                # ------------------------------------------------------- V6
                def city_state_badge():
                    badges = page.eval_on_selector_all(
                        '#world .world-city',
                        'nodes => nodes.map(n => ({owner: n.getAttribute("data-owner"),'
                                  ' label: n.querySelector("text.label").textContent,'
                                  ' capital: n.querySelectorAll(".world-capital").length,'
                                  ' pop: n.querySelector(".city-pop").textContent}))')
                    assert len(badges) == 1, badges
                    badge = badges[0]
                    assert badge['owner'] == '12' and badge['label'] == 'GENEVA', badge
                    assert badge['capital'] == 1 and badge['pop'] == '3', badge
                    data = world_data()
                    city = next(row for row in data['cities'] if row['owner'] == 12)
                    facts['world_city'] = {'dom': badge, 'city': city}
                    return (f'exactly one city-state badge: {badge} at the recorded '
                            f'coordinate {city["q"]},{city["r"]} with a capital star path '
                            f'(majors\' cities stay seat badges)')

                checks.run('V6', 'city-state city badge carries the roster name and star',
                           city_state_badge)

                # ------------------------------------------------------- shots
                def screenshots():
                    page.evaluate('() => window.scrollTo(0, 0)')
                    page.wait_for_timeout(250)
                    shots['territory-1440'] = str(ROOT / 'territory-1440.png')
                    page.screenshot(path=shots['territory-1440'])
                    page.locator('#roster').scroll_into_view_if_needed()
                    page.wait_for_timeout(250)
                    shots['roster-panel'] = str(ROOT / 'roster-panel.png')
                    page.locator('#roster').screenshot(path=shots['roster-panel'])
                    page.set_viewport_size({'width': 390, 'height': 844})
                    page.wait_for_timeout(450)
                    overflow = page.evaluate(
                        '() => [document.documentElement.scrollWidth, innerWidth]')
                    assert overflow[0] <= overflow[1], overflow
                    page.evaluate('() => window.scrollTo(0, 0)')
                    page.wait_for_timeout(250)
                    shots['territory-390'] = str(ROOT / 'territory-390.png')
                    page.screenshot(path=shots['territory-390'])
                    page.set_viewport_size({'width': 1440, 'height': 1080})
                    page.wait_for_timeout(300)
                    facts['overflow_390'] = overflow
                    return (f'territory-1440.png, roster-panel.png and territory-390.png '
                            f'written; at 390px scrollWidth<=innerWidth {overflow}')

                checks.run('V7', 'screenshots at 1440 and 390 with no horizontal overflow',
                           screenshots)

                # ------------------------------------------------- player route
                player = browser.new_page(viewport={'width': 1440, 'height': 1080})
                player.set_default_timeout(30000)
                watch(player, 'player')

                def player_route():
                    player.goto(f'{origin}map?id={DERIVED_ID}&turn={WORLD_TURN}&player=0',
                                wait_until='load')
                    player.wait_for_selector('#legend-groups .legend-group', timeout=30000)
                    embedded = json.loads(player.locator('#data').text_content())
                    assert 'world' not in embedded, 'player export carries a world'
                    assert player.locator('#world .tint.world').count() == 0
                    assert player.locator('#world .border.world').count() == 0
                    assert player.locator('#world .world-city').count() == 0
                    assert player.locator('#world-territory-toggle').count() == 0
                    roster = player.locator('#roster').inner_text()
                    assert 'not recorded' in roster, roster
                    assert 'GENEVA' not in roster
                    facts['player_route'] = {'world': 'world' not in embedded,
                                             'roster': roster.strip()[:60]}
                    return ('/map?…&player=0 over the same log: no world in the embedded '
                            f'data, no world layer paths, no toggle, and the roster panel '
                            f'reads "{roster.strip()[:40]}…"')

                checks.run('V8', 'the player route carries NO world layer', player_route)
                player.close()

                # ------------------------------------------------------- clean
                def clean_run():
                    external = [row['url'] for row in requests
                                if not row['url'].startswith(origin)]
                    assert not errors, errors
                    assert not external, external[:5]
                    facts['requests'] = sorted({row['url'].split('?')[0]
                                                for row in requests})
                    return (f'0 page errors and 0 console script errors across '
                            f'{len(requests)} request(s), all same-origin '
                            f'{facts["requests"]}')

                checks.run('V9', 'zero page/console errors, same-origin requests only',
                           clean_run)
            finally:
                browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        shutil.rmtree(workspace, ignore_errors=True)

    passed = len([row for row in checks.results if row['result'] == 'pass'])
    summary = {
        'scope': ('Ephemeral loopback dashboard server over a COPY of '
                  'minimax100-20260907T183425Z plus a derived world-carrying event log '
                  '(schema fixture translated into the observed area); headless '
                  '/usr/bin/google-chrome; the original run directory was read only; '
                  'no tuner, match, provider or live engine touched.'),
        'commit': head_before,
        'head_at_start': head_before, 'head_at_end': head_commit(),
        'derived_run': DERIVED_ID, 'copied_run': RUN_ID,
        'world': {'fixture': str(FIXTURE), 'offset': OFFSET, 'turn': WORLD_TURN,
                  'older_snapshot_turn': OLDER_TURN, **manifest_bits},
        'checks': checks.results,
        'totals': {'checks': len(checks.results), 'passed': passed,
                   'failed': len(checks.failed),
                   'page_errors': len([row for row in errors
                                       if row.get('kind') == 'pageerror']),
                   'console_script_errors': len([row for row in errors
                                                 if row.get('kind') == 'console']),
                   'resource_notices': len(notices), 'requests': len(requests)},
        'errors': errors, 'resource_notices': notices,
        'screenshots': shots, 'facts': facts,
    }
    (ROOT / 'lane-v-smoke-summary.json').write_text(json.dumps(summary, indent=2) + '\n')
    print(json.dumps({'passed': passed, 'failed': len(checks.failed),
                      'failures': [row['check'] for row in checks.failed],
                      'page_errors': summary['totals']['page_errors'],
                      'console_script_errors': summary['totals']['console_script_errors']},
                     indent=2))
    return 1 if checks.failed or errors else 0


if __name__ == '__main__':
    sys.exit(main())
