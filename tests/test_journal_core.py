"""Pure match-room journal contracts, evaluated under Node exactly as the browser runs them."""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from civ_arena import dashboard as d

ASSETS = Path(d.__file__).with_name('dashboard_static')
NODE = shutil.which('node')
CORE = (ASSETS / 'journal-core.js').read_text()


def run(harness):
    """Evaluate the shipped module, then the harness against what it exported.

    journal-core.js keeps its whole body in one function scope because the match
    room loads it beside compare-core.js and two classic scripts cannot both
    declare a top-level `Core`; under Node the export arrives on module.exports.
    """
    if NODE is None:
        pytest.fail('node is required to evaluate journal-core.js contracts (never skip them)')
    source = f'{CORE}\nconst Core = module.exports;\n{harness}'
    proc = subprocess.run([NODE, '-e', source], capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


# The renderer's own kind and acceptance rules, copied from app.js so the harness
# folds and filters exactly what the page folds and filters.
KINDS = '''
const observation = tool => /^(get_|observe|read_|list_|query_|inspect_)/.test(tool || '');
const noteTool = tool =>
  /(diary|journal|goal|prediction|lesson|note|recall|strategy)/.test(tool || '');
const kindOf = tool => observation(tool) ? 'Observation' : noteTool(tool) ? 'Note' : 'Action';
const isAccepted = status => ['accepted', 'ok', 'success', 'completed'].includes(status);
const call = (tool, status, seq) => ({tool, status, seq});
'''

RUNS = KINDS + '''
const calls = [call('get_units', 'accepted', 1), call('get_units', 'accepted', 2),
  call('get_units', 'accepted', 3), call('move_unit', 'accepted', 4),
  call('get_units', 'accepted', 5), call('get_units', 'rejected', 6),
  call('get_units', 'rejected', 7)];
const show = items => items.map(item => item.type === 'run'
  ? {type: 'run', key: item.key, tool: item.tool, status: item.status, index: item.index,
     seqs: item.calls.map(one => one.seq)}
  : {type: 'call', seq: item.call.seq, index: item.index, run: item.run || null});
'''


def test_filter_keeps_one_kind_and_an_unknown_filter_keeps_everything():
    out = run(KINDS + '''
const list = [call('get_units', 'accepted', 1), call('move_unit', 'rejected', 2),
  call('write_diary', 'accepted', 3), call('get_visible_map', 'accepted', 4)];
const seqs = filter => Core.filterCalls(list, filter, kindOf).map(one => one.seq);
console.log(JSON.stringify({
  all: seqs('all'), actions: seqs('actions'), observations: seqs('observations'),
  notes: seqs('notes'), unknown: seqs('sideways'), inherited: seqs('constructor'),
  builtin: seqs('toString'), proto: seqs('__proto__'),
  missing: Core.filterCalls(undefined, 'all', kindOf),
}));''')
    assert out['all'] == [1, 2, 3, 4]
    assert out['actions'] == [2]
    assert out['observations'] == [1, 4]
    assert out['notes'] == [3]
    # An unknown filter shows everything; it never empties the lane silently.
    assert out['unknown'] == [1, 2, 3, 4]
    # An inherited property name is not a filter: a plain-object lookup table
    # would answer 'constructor' with Object and remove every call.
    assert out['inherited'] == [1, 2, 3, 4]
    assert out['builtin'] == [1, 2, 3, 4]
    assert out['proto'] == [1, 2, 3, 4]
    assert out['missing'] == []


def test_consecutive_same_tool_and_status_calls_fold_and_an_opened_run_yields_its_calls():
    out = run(RUNS + '''
console.log(JSON.stringify({
  folded: show(Core.foldRuns(calls, 5, 0, new Set())),
  opened: show(Core.foldRuns(calls, 5, 0, new Set(['5:0:1']))),
  key: Core.runKey(5, 0, 1),
  seatOne: show(Core.foldRuns(calls, 5, 1, new Set())),
  seatOneOpened: show(Core.foldRuns(calls, 5, 1, new Set(['5:1:1']))),
  single: show(Core.foldRuns([call('end_turn', 'rejected', 9)], 5, 0, new Set())),
  none: show(Core.foldRuns(undefined, 5, 0, new Set())),
}));''')
    assert out['key'] == '5:0:1'
    # The key names the seat: the same sequence numbers on the other seat are a
    # different run, and one opened run must never open its twin.
    assert [item['key'] for item in out['seatOne'] if item['type'] == 'run'] == ['5:1:1',
                                                                                '5:1:6']
    assert [item['type'] for item in out['seatOne']] == [item['type'] for item in out['folded']]
    assert [item['type'] for item in out['seatOneOpened']][:3] == ['call', 'call', 'call']
    assert out['folded'] == [
        {'type': 'run', 'key': '5:0:1', 'tool': 'get_units', 'status': 'accepted', 'index': 0,
         'seqs': [1, 2, 3]},
        {'type': 'call', 'seq': 4, 'index': 3, 'run': None},
        # Same tool, different status: a rejection never folds into an acceptance.
        {'type': 'call', 'seq': 5, 'index': 4, 'run': None},
        {'type': 'run', 'key': '5:0:6', 'tool': 'get_units', 'status': 'rejected', 'index': 5,
         'seqs': [6, 7]}]
    assert out['opened'][:3] == [
        {'type': 'call', 'seq': 1, 'index': 0, 'run': {'key': '5:0:1', 'size': 3, 'position': 1}},
        {'type': 'call', 'seq': 2, 'index': 1, 'run': {'key': '5:0:1', 'size': 3, 'position': 2}},
        {'type': 'call', 'seq': 3, 'index': 2, 'run': {'key': '5:0:1', 'size': 3, 'position': 3}}]
    # Opening one run leaves the other folded and every index unchanged.
    assert [item['index'] for item in out['opened']] == [0, 1, 2, 3, 4, 5]
    assert out['opened'][-1]['type'] == 'run' and out['opened'][-1]['seqs'] == [6, 7]
    assert out['single'] == [{'type': 'call', 'seq': 9, 'index': 0, 'run': None}]
    assert out['none'] == []


def test_a_run_that_grew_between_polls_folds_at_its_new_size():
    out = run(RUNS + '''
const pair = [call('get_units', 'accepted', 1), call('get_units', 'accepted', 2)];
const grown = pair.concat([call('get_units', 'accepted', 3)]);
console.log(JSON.stringify({
  pair: show(Core.foldRuns(pair, 5, 0, new Set())),
  grown: show(Core.foldRuns(grown, 5, 0, new Set())),
  pairOpened: show(Core.foldRuns(pair, 5, 0, new Set(['5:0:1']))),
  grownOpened: show(Core.foldRuns(grown, 5, 0, new Set(['5:0:1']))),
}));''')
    # The key is the first call, so an appended consecutive call joins the run
    # the reader already opened instead of starting a second one.
    assert out['pair'] == [{'type': 'run', 'key': '5:0:1', 'tool': 'get_units',
                            'status': 'accepted', 'index': 0, 'seqs': [1, 2]}]
    assert out['grown'] == [{'type': 'run', 'key': '5:0:1', 'tool': 'get_units',
                             'status': 'accepted', 'index': 0, 'seqs': [1, 2, 3]}]
    assert [item['run']['size'] for item in out['pairOpened']] == [2, 2]
    assert [item['run']['position'] for item in out['pairOpened']] == [1, 2]
    # The same opened key now yields three calls: a hint read off this fold says
    # "of 3", which is why journal.js recomputes it instead of remembering it.
    assert [item['run']['size'] for item in out['grownOpened']] == [3, 3, 3]
    assert [item['run']['position'] for item in out['grownOpened']] == [1, 2, 3]
    assert [item['seq'] for item in out['grownOpened']] == [1, 2, 3]


def test_status_class_names_acceptance_every_recorded_failure_and_silence():
    out = run(KINDS + '''
const shape = status => Core.statusClass(status, isAccepted);
console.log(JSON.stringify({
  accepted: ['accepted', 'ok', 'success', 'completed'].map(shape),
  failures: ['rejected', 'failed', 'error', 'aborted'].map(shape),
  quiet: [null, undefined, '', 'running', 'queued'].map(shape),
  aborted: Core.pillSummary([call('move_unit', 'aborted', 1)], 'all', kindOf, isAccepted),
  glyphs: Core.STATUS_GLYPH,
}));''')
    assert out['accepted'] == ['accepted'] * 4
    # An aborted call is a recorded failure, not a call still waiting.
    assert out['failures'] == ['rejected'] * 4
    assert out['quiet'] == ['pending'] * 5
    assert out['aborted']['rejected'] == 1 and out['aborted']['pending'] == 0
    assert out['aborted']['glyphs'] == '▶1 ✕1'
    assert out['aborted']['title'] == '1 recorded call · accepted 0 · rejected 1 · pending 0'
    assert out['glyphs'] == {'accepted': '✓', 'rejected': '✕', 'pending': '◌'}


def test_pill_summary_counts_kinds_rejections_and_names_the_filter():
    out = run(KINDS + '''
const list = [call('get_units', 'accepted', 1), call('get_cities', 'accepted', 2),
  call('move_unit', 'rejected', 3)];
const pill = (calls, filter) => Core.pillSummary(calls, filter, kindOf, isAccepted);
console.log(JSON.stringify({
  all: pill(list, 'all'),
  actions: pill(list, 'actions'),
  observationsOne: pill([call('get_units', 'accepted', 1)], 'observations'),
  pending: pill([call('move_unit', null, 1)], 'all'),
  empty: pill([], 'all'),
}));''')
    assert out['all']['count'] == 3
    assert out['all']['kinds'] == {'Observation': 2, 'Action': 1, 'Note': 0}
    assert out['all']['rejected'] == 1 and out['all']['pending'] == 0
    assert out['all']['text'] == '3 calls'
    assert out['all']['glyphs'] == '◉2 ▶1 ✕1'
    assert out['all']['title'] == '3 recorded calls · accepted 2 · rejected 1 · pending 0'
    assert out['actions'] == {'count': 1, 'kinds': {'Observation': 0, 'Action': 1, 'Note': 0},
                              'rejected': 1, 'pending': 0, 'text': '1 action', 'glyphs': '▶1 ✕1',
                              'title': '1 recorded call · accepted 0 · rejected 1 · pending 0'}
    assert out['observationsOne']['text'] == '1 observation'
    assert out['observationsOne']['glyphs'] == '◉1'
    # An unrecorded status is pending, neither accepted nor rejected.
    assert out['pending']['pending'] == 1 and out['pending']['rejected'] == 0
    assert out['pending']['glyphs'] == '▶1'
    assert out['empty']['text'] == '0 calls' and out['empty']['glyphs'] == ''
    assert out['empty']['title'] == '0 recorded calls · accepted 0 · rejected 0 · pending 0'


def test_window_clips_at_both_ends_and_snaps_a_centre_that_is_not_a_recorded_turn():
    out = run('''
const turns = Array.from({length: 38}, (unused, index) => index + 1);
const pack = result => ({turns: result.turns, earlier: result.earlier, later: result.later,
                         center: result.center});
console.log(JSON.stringify({
  middle: pack(Core.windowOf(turns, 20)),
  head: pack(Core.windowOf(turns, 2)),
  beyond: pack(Core.windowOf(turns, 100)),
  gap: pack(Core.windowOf([1, 4, 30], 5, 1)),
  empty: pack(Core.windowOf([], 3)),
  radius: Core.RADIUS,
}));''')
    assert out['radius'] == 6
    assert out['middle'] == {'turns': list(range(14, 27)), 'earlier': 13, 'later': 12,
                             'center': 20}
    assert out['head'] == {'turns': list(range(1, 9)), 'earlier': 0, 'later': 30, 'center': 2}
    # A centre past the last recorded turn snaps back to it instead of emptying.
    assert out['beyond'] == {'turns': list(range(32, 39)), 'earlier': 31, 'later': 0,
                             'center': 38}
    assert out['gap'] == {'turns': [1, 4, 30], 'earlier': 0, 'later': 0, 'center': 4}
    assert out['empty'] == {'turns': [], 'earlier': 0, 'later': 0, 'center': None}


MODEL = '''
const item = seq => ({type: 'call', call: {tool: 'get_units', status: 'accepted', seq}, index: 0});
const lane = (seat, playerId, entries) => ({seat, player_id: playerId,
                                            byTurn: new Map(entries)});
const summary = count => ({count, text: `${count} calls`, glyphs: '', title: '', rejected: 0,
                           pending: 0, kinds: {Observation: count, Action: 0, Note: 0}});
const model = () => ({turns: [5, 6], expanded: new Set([5]), selected: 5, lanes: [
  lane(0, 0, [[5, {status: 'completed', summary: null, items: [item(1), item(2), item(3)]}],
              [6, {status: 'completed', items: [], summary: summary(4)}]]),
  lane(1, 1, [[5, null], [6, {status: 'completed', items: [], summary: summary(2)}]])]});
'''


def test_layout_places_abutting_columns_pills_and_nodes_from_the_pinned_constants():
    out = run(MODEL + '''
const box = Core.layout(model());
console.log(JSON.stringify({
  width: box.width, height: box.height,
  columns: box.columns.map(column => ({turn: column.turn, x: column.x, width: column.width,
    expanded: column.expanded, selected: column.selected,
    lanes: column.lanes.map(lane => ({seat: lane.seat, y: lane.y, kind: lane.kind,
      boxes: lane.items.map(one => [one.x, one.y, one.w, one.h])}))})),
  empty: Core.layout({turns: [], expanded: new Set(), selected: null, lanes: []}),
}));''')
    assert (out['width'], out['height']) == (622, 234)
    first, second = out['columns']
    assert (first['turn'], first['x'], first['width']) == (5, 64, 436)
    assert first['expanded'] is True and first['selected'] is True
    assert [lane['kind'] for lane in first['lanes']] == ['nodes', 'absent']
    # Nodes start one PAD into the column and step by PITCH; the lane centres them.
    assert first['lanes'][0]['boxes'] == [[74, 51, 132, 54], [216, 51, 132, 54],
                                          [358, 51, 132, 54]]
    assert (first['lanes'][0]['y'], first['lanes'][1]['y']) == (30, 126)
    assert first['lanes'][1]['boxes'] == []
    assert (second['turn'], second['x'], second['width']) == (6, 500, 112)
    assert second['expanded'] is False and second['selected'] is False
    assert [lane['kind'] for lane in second['lanes']] == ['pill', 'pill']
    assert second['lanes'][0]['boxes'] == [[510, 58, 92, 40]]
    assert second['lanes'][1]['boxes'] == [[510, 154, 92, 40]]
    assert out['empty'] == {'width': 74, 'height': 234, 'columns': []}


def test_an_expanded_column_whose_filter_removed_every_call_still_has_a_column():
    out = run(MODEL + '''
const filtered = {turns: [5], expanded: new Set([5]), selected: 5, lanes: [
  lane(0, 0, [[5, {status: 'completed', summary: null, items: []}]]),
  lane(1, 1, [[5, {status: 'completed', summary: null, items: []}]])]};
const box = Core.layout(filtered);
console.log(JSON.stringify({width: box.width, column: box.columns[0].width,
  x: box.columns[0].x, kinds: box.columns[0].lanes.map(one => one.kind),
  items: box.columns[0].lanes.map(one => one.items.length)}));''')
    # A filter that matched nothing is still a turn on the axis: the column keeps
    # one node's width so the reader can see which turn is empty and why.
    assert out['column'] == 152
    assert (out['x'], out['width']) == (64, 226)
    assert out['kinds'] == ['nodes', 'nodes'] and out['items'] == [0, 0]


def test_layout_and_folding_are_deterministic():
    out = run(MODEL + RUNS + '''
const once = () => JSON.stringify([Core.layout(model()),
  show(Core.foldRuns(calls, 5, 0, new Set(['5:0:6']))),
  Core.pillSummary(calls, 'all', kindOf, isAccepted),
  Core.windowOf([1, 2, 3, 4, 5, 6, 7, 8, 9], 5, 2)]);
console.log(JSON.stringify({same: once() === once(), digest: once().length}));''')
    assert out['same'] is True
    assert out['digest'] > 0


def test_both_journal_scripts_parse_under_node():
    if NODE is None:
        pytest.fail('node is required')
    for name in ('journal-core.js', 'journal.js'):
        script = ASSETS / name
        proc = subprocess.run([NODE, '--check', str(script)], capture_output=True, text=True,
                              timeout=60)
        assert proc.returncode == 0, (name, proc.stderr)
        assert script.read_text().startswith("'use strict';")


def test_the_match_room_scripts_share_one_global_scope_without_a_name_clash(tmp_path):
    """Classic scripts share one top-level lexical scope in the browser.

    Two of them declaring the same `const` is a page error that silently kills
    the second module, so the served set is parsed here exactly as the page
    concatenates it, in the order index.html loads.
    """
    if NODE is None:
        pytest.fail('node is required')
    order = re.findall(r'<script src="/([^"]+\.js)" defer></script>',
                       (ASSETS / 'index.html').read_text())
    assert order == ['app.js', 'journal-core.js', 'journal.js', 'compare-core.js',
                     'compare.js'], order
    combined = tmp_path / 'combined.js'
    combined.write_text('\n'.join((ASSETS / name).read_text() for name in order))
    proc = subprocess.run([NODE, '--check', str(combined)], capture_output=True, text=True,
                          timeout=60)
    assert proc.returncode == 0, proc.stderr
