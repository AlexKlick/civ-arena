"""Pure match-room comparison contracts, evaluated under Node exactly as the browser runs them."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from civ_arena import dashboard as d

ASSETS = Path(d.__file__).with_name('dashboard_static')
NODE = shutil.which('node')
CORE = (ASSETS / 'compare-core.js').read_text()


def run(harness):
    if NODE is None:
        pytest.fail('node is required to evaluate compare-core.js contracts (never skip them)')
    proc = subprocess.run([NODE, '-e', CORE + '\n' + harness], capture_output=True, text=True,
                          timeout=60)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


SEAT = '''
const seat = {player_id: 0, history: [
  {turn: 1, seq: 1, added: ['POTTERY'], removed: [], researching: 'MINING', baseline: false},
  {turn: 3, seq: 2, added: ['MINING'], removed: [], researching: 'WRITING', baseline: false},
  {turn: 5, seq: 3, added: [], removed: ['POTTERY'], researching: null, baseline: false}],
  latest: {turn: 5, seq: 3, researched: ['MINING'], researching: 'WRITING'}};
const capped = {player_id: 1, history: [
  {turn: 20, seq: 9, added: ['POTTERY'], removed: [], researching: null, baseline: true},
  {turn: 22, seq: 10, added: ['MINING'], removed: [], researching: null, baseline: false}],
  latest: {turn: 22, seq: 10, researched: ['MINING', 'POTTERY'], researching: null}};
const show = fold => ({researched: [...fold.researched].sort(), at: [...fold.at],
  turn: fold.turn, seq: fold.seq, researching: fold.researching,
  retained: fold.retained, retainedFrom: fold.retainedFrom});
'''


def test_fold_honours_the_cutoff_the_latest_packet_and_an_unretained_history():
    out = run(SEAT + '''
console.log(JSON.stringify({
  early: show(Core.foldSeat(seat, 1)),
  mid: show(Core.foldSeat(seat, 4)),
  atLatest: show(Core.foldSeat(seat, 5)),
  beyond: show(Core.foldSeat(seat, 9)),
  none: show(Core.foldSeat(seat, null)),
  unretained: show(Core.foldSeat(capped, 15)),
  retained: show(Core.foldSeat(capped, 21)),
}));''')
    # The cutoff decides the set: ignoring it would show the newest packet always.
    assert out['early']['researched'] == ['POTTERY'] and out['early']['turn'] == 1
    assert out['early']['researching'] == 'MINING' and out['early']['seq'] == 1
    assert out['mid']['researched'] == ['MINING', 'POTTERY']
    assert out['mid']['at'] == [['POTTERY', 1], ['MINING', 3]]
    assert out['mid']['turn'] == 3
    # The latest packet wins at or after its own turn, because history is capped.
    assert out['atLatest']['researched'] == ['MINING'] and out['atLatest']['turn'] == 5
    assert out['beyond']['researched'] == ['MINING']
    assert out['none']['researched'] == ['MINING'] and out['none']['researching'] == 'WRITING'
    # Before a synthetic baseline row nothing was retained, and an empty set lies.
    assert out['unretained']['retained'] is False
    assert out['unretained']['retainedFrom'] == 20
    assert out['retained']['retained'] is True and out['retained']['retainedFrom'] is None
    assert out['retained']['researched'] == ['POTTERY']


def test_diff_separates_shared_and_seat_only_and_refuses_to_share_with_an_unretained_seat():
    out = run(SEAT + '''
const other = {player_id: 1, history: [
  {turn: 2, seq: 4, added: ['MINING', 'SAILING'], removed: [], researching: null,
   baseline: false}], latest: null};
const empty = {player_id: 1, history: [], latest: null};
const pack = result => ({shared: result.shared, only: result.only,
                         unretained: result.unretained});
console.log(JSON.stringify({
  pair: pack(Core.diffOf([Core.foldSeat(seat, 4), Core.foldSeat(other, 4)])),
  lonely: pack(Core.diffOf([Core.foldSeat(seat, 4), Core.foldSeat(empty, 4)])),
  blind: pack(Core.diffOf([Core.foldSeat(seat, 4), Core.foldSeat(capped, 15)])),
}));''')
    assert out['pair'] == {'shared': ['MINING'], 'only': [['POTTERY'], ['SAILING']],
                           'unretained': False}
    assert out['lonely'] == {'shared': [], 'only': [['MINING', 'POTTERY'], []],
                             'unretained': False}
    # Nothing is "shared" with a seat whose history at this turn was not retained.
    assert out['blind'] == {'shared': [], 'only': [['MINING', 'POTTERY'], []],
                            'unretained': True}


def test_baseline_names_carry_no_acquisition_turn():
    out = run('''
const seat = {player_id: 0, history: [
  {turn: 11, seq: 5, added: ['POTTERY'], removed: [], researching: null, baseline: true},
  {turn: 12, seq: 6, added: ['MINING'], removed: [], researching: null, baseline: false}],
  latest: null};
const fold = Core.foldSeat(seat, 13);
console.log(JSON.stringify({at: [...fold.at], retained: fold.retained,
  pottery: Core.statusFor(fold, 'POTTERY'), mining: Core.statusFor(fold, 'MINING')}));''')
    # The baseline turn is where the fold stopped, not where POTTERY was gained.
    assert out['at'] == [['POTTERY', None], ['MINING', 12]]
    assert out['retained'] is True
    assert out['pottery'] == 'researched, packet turn not retained'
    assert out['mining'] == 'researched at turn 12'


def test_seat_series_reads_the_shared_turn_axis_and_keeps_gaps_null():
    out = run('''
const rows = [{turn: 1, gold: 5}, {turn: 3, gold: null}, {turn: 4, gold: '5'},
              {turn: 5, gold: 'later'}, {turn: 6}];
const asMap = new Map(rows.map(row => [row.turn, row]));
console.log(JSON.stringify({
  fromArray: Core.seatSeries(rows, [1, 2, 3, 4, 5, 6], 'gold'),
  fromMap: Core.seatSeries(asMap, [1, 2, 3, 4, 5, 6], 'gold'),
  other: Core.seatSeries(rows, [1, 2], 'techs'),
  none: Core.seatSeries(null, [1, 2], 'gold'),
}));''')
    # A missing row, a null field and an unparsable field are all gaps, not zeros.
    assert out['fromArray'] == [5, None, None, 5, None, None]
    assert out['fromMap'] == out['fromArray']
    assert out['other'] == [None, None]
    assert out['none'] == [None, None]


def test_half_state_never_marks_an_unretained_seat_as_done():
    out = run(SEAT + '''
console.log(JSON.stringify({
  done: Core.halfState(Core.foldSeat(seat, 4), 'POTTERY'),
  researching: Core.halfState(Core.foldSeat(seat, 4), 'WRITING'),
  notYet: Core.halfState(Core.foldSeat(seat, 4), 'SAILING'),
  unretained: Core.halfState(Core.foldSeat(capped, 15), 'POTTERY'),
  absent: Core.halfState(null, 'POTTERY'),
}));''')
    assert out['done'] == {'done': True, 'researching': False, 'unretained': False}
    assert out['researching'] == {'done': False, 'researching': True, 'unretained': False}
    assert out['notYet'] == {'done': False, 'researching': False, 'unretained': False}
    # An unretained seat is neither done nor researching, whatever the set holds.
    assert out['unretained'] == {'done': False, 'researching': False, 'unretained': True}
    assert out['absent'] == {'done': False, 'researching': False, 'unretained': False}


def test_options_view_only_yields_chips_for_an_observed_list():
    out = run('''console.log(JSON.stringify({
      observed: Core.optionsView({options_source: 'observed', research_options: [
        {tech_id: 'POTTERY', cost: 25}, {tech_id: 'MINING', cost: null}]}),
      observedEmpty: Core.optionsView({options_source: 'observed', research_options: []}),
      unobserved: Core.optionsView({options_source: 'not_requested_active_choice',
        research_options: [{tech_id: 'POTTERY', cost: 25}, {tech_id: 'MINING', cost: 30}]}),
      missing: Core.optionsView(null),
    }));''')
    assert out['observed'] == {'chips': [{'tech_id': 'POTTERY', 'cost': 25},
                                         {'tech_id': 'MINING', 'cost': None}], 'note': None}
    assert out['observedEmpty'] == {'chips': [],
                                    'note': 'No options recorded in this packet (observed)'}
    # A recorded list the packet never observed is not a menu of choices.
    assert out['unobserved'] == {'chips': [], 'note': 'Options not requested in this packet '
                                                      '(not_requested_active_choice)'}
    assert out['missing'] == {'chips': [],
                              'note': 'Options not requested in this packet (not recorded)'}


def test_status_for_names_researching_gaps_and_unretained_history():
    out = run(SEAT + '''
console.log(JSON.stringify({
  researching: Core.statusFor(Core.foldSeat(seat, 4), 'WRITING'),
  done: Core.statusFor(Core.foldSeat(seat, 4), 'POTTERY'),
  notYet: Core.statusFor(Core.foldSeat(seat, 4), 'SAILING'),
  unknownTurn: Core.statusFor(Core.foldSeat(seat, 9), 'MINING'),
  unretained: Core.statusFor(Core.foldSeat(capped, 15), 'POTTERY'),
  absent: Core.statusFor(null, 'POTTERY'),
}));''')
    assert out == {'researching': 'researching', 'done': 'researched at turn 1',
                   'notYet': 'not yet', 'unknownTurn': 'researched at turn 3',
                   'unretained': 'history not retained at this turn',
                   'absent': 'no retained packet'}


def test_options_are_only_shown_when_the_packet_recorded_observing_them():
    out = run('''console.log(JSON.stringify({
      observed: Core.optionsNote({options_source: 'observed',
                                  research_options: [{tech_id: 'POTTERY', cost: 25}]}),
      observedEmpty: Core.optionsNote({options_source: 'observed', research_options: []}),
      other: Core.optionsNote({options_source: 'not_requested_active_choice',
                               research_options: [{tech_id: 'POTTERY', cost: 25}]}),
      unnamed: Core.optionsNote({research_options: []}),
      missing: Core.optionsNote(null),
    }));''')
    # Only an observed, non-empty list may be drawn as chips.
    assert out['observed'] is None
    assert out['observedEmpty'] == 'No options recorded in this packet (observed)'
    assert out['other'] == ('Options not requested in this packet '
                            '(not_requested_active_choice)')
    assert out['unnamed'] == 'Options not requested in this packet (not recorded)'
    assert out['missing'] == 'Options not requested in this packet (not recorded)'


def test_nice_max_ladder_never_reports_a_ceiling_below_the_data():
    out = run('''console.log(JSON.stringify([0, -3, 0.4, 1, 1.1, 2.4, 7, 9, 42, 118, 2600]
      .map(Core.niceMax)));''')
    assert out == [1, 1, 0.4, 1, 1.2, 2.5, 8, 10, 50, 120, 3000]


def test_series_segments_break_at_gaps_and_at_missing_turns():
    out = run('''
const runs = (values, turns) => Core.seriesSegments(values, turns)
  .map(run => run.map(point => [point.turn, point.value]));
console.log(JSON.stringify({
  solid: runs([1, 2, 3], [1, 2, 3]),
  gap: runs([1, null, 3], [1, 2, 3]),
  skipped: runs([1, 3], [1, 3]),
  mixed: runs([1, 2, null, 4, 5], [1, 2, 3, 5, 6]),
  none: runs([null, null], [1, 2]),
  text: runs(['4', 5], [1, 2]),
}));''')
    assert out['solid'] == [[[1, 1], [2, 2], [3, 3]]]
    # A missing value is a gap, never a zero and never a bridge.
    assert out['gap'] == [[[1, 1]], [[3, 3]]]
    # Turns 1 and 3 are not neighbours: the line must break between them.
    assert out['skipped'] == [[[1, 1]], [[3, 3]]]
    assert out['mixed'] == [[[1, 1], [2, 2]], [[5, 4], [6, 5]]]
    assert out['none'] == []
    assert out['text'] == [[[1, 4], [2, 5]]]


def test_axis_ticks_label_turn_values_not_row_positions():
    out = run('''console.log(JSON.stringify({
      sparse: Core.axisTicks([1, 2, 40]),
      short: Core.axisTicks([4, 5, 6]),
      single: Core.axisTicks([7]),
      empty: Core.axisTicks([]),
    }));''')
    assert out['sparse'] == [1, 6, 11, 16, 21, 26, 31, 36, 40]
    assert out['short'] == [4, 5, 6]
    assert out['single'] == [7]
    assert out['empty'] == []


def test_directive_text_names_the_recorded_source_for_every_shape():
    out = run('''
const delta = extra => Object.assign({changed: true, previous_turn: 1, source: 'model',
  reasons: [], last_decision_turn: 4, changed_fields: [], tactical_overrides: 0}, extra);
console.log(JSON.stringify({
  missing: Core.directiveText(null),
  first: Core.directiveText(delta({changed: null, source: 'autopilot'})),
  firstUnnamed: Core.directiveText(delta({changed: null, source: null})),
  model: Core.directiveText(delta({reasons: ['new_visible_contact'],
                                   changed_fields: ['scouting.policy']})),
  modelBare: Core.directiveText(delta({})),
  autopilotSame: Core.directiveText(delta({changed: false, source: 'autopilot'})),
  autopilotChanged: Core.directiveText(delta({source: 'autopilot',
                                              changed_fields: ['research_preferences']})),
  unnamed: Core.directiveText(delta({source: null})),
  other: Core.directiveText(delta({source: 'initial_capital_dispatch_guard',
                                   changed_fields: ['production_preferences']})),
}));''')
    assert out['missing'] == 'No directive record'
    assert out['first'] == 'First recorded directive · Autopilot'
    assert out['firstUnnamed'] == 'First recorded directive · source not recorded'
    assert out['model'] == 'Model update · new_visible_contact · changed: scouting.policy'
    assert out['modelBare'] == 'Model update · no reasons recorded · changed: none'
    assert out['autopilotSame'] == 'Unchanged · autopilot · last decision T4'
    assert out['autopilotChanged'] == 'Changed under autopilot · research_preferences'
    assert out['unnamed'] == 'Directive recorded · source not recorded'
    assert out['other'] == 'Initial Capital Dispatch Guard · changed: production_preferences'


def test_economy_lines_mark_acceptance_rejection_and_an_empty_candidate_set():
    out = run('''console.log(JSON.stringify({
      none: Core.economyLines([]),
      rows: Core.economyLines([
        {tool: 'set_research', item: 'POTTERY', status: 'accepted', reason: null,
         rejection: null, outcome: 'POTTERY', candidates: null, eligible: null},
        {tool: 'set_city_production', item: 'WARRIOR', status: 'rejected',
         rejection: '{"code":"busy"}', outcome: null, reason: 'defense_gap',
         candidates: 2, eligible: 0},
        {tool: 'set_city_production', item: null, status: null, rejection: null,
         outcome: null, reason: null, candidates: 1, eligible: 1}]),
    }));''')
    assert out['none'] == [{'text': 'not recorded', 'bad': False}]
    accepted, rejected, unknown = out['rows']
    assert accepted == {'text': 'set research POTTERY ✓', 'bad': False}
    assert rejected == {'text': 'set city production WARRIOR ✕ · {"code":"busy"} · '
                                'production: no eligible item', 'bad': True}
    # An unrecorded status is neither accepted nor rejected.
    assert unknown == {'text': 'set city production item not recorded ◌', 'bad': False}


def test_growth_text_prints_absent_threat_counts_as_gaps():
    out = run('''console.log(JSON.stringify({
      none: Core.growthText(null),
      unknown: Core.growthText({mode: 'expand', minimum_military_count: null,
        threats: {confirmed_barbarian: null, unknown: null, unclassified: null}}),
      counted: Core.growthText({mode: 'defend', minimum_military_count: 2,
        threats: {confirmed_barbarian: 1, unknown: 0, unclassified: 2},
        settlement_mission: {status: 'awaiting_settler', site: '7,18'}}),
    }));''')
    assert out['none'] == 'not recorded'
    assert out['unknown'] == 'expand · min military — · threats —/—/—'
    assert out['counted'] == ('defend · min military 2 · threats 1/0/2 · '
                              'settling 7,18 (awaiting_settler)')


def test_tree_depths_count_same_era_prerequisites_and_survive_a_cycle():
    out = run('''
const nodes = [{id: 'A', era: 'ANCIENT'}, {id: 'B', era: 'ANCIENT'}, {id: 'C', era: 'ANCIENT'},
  {id: 'D', era: 'CLASSICAL'}, {id: 'X', era: 'ANCIENT'}, {id: 'Y', era: 'ANCIENT'}];
const edges = [['B', 'A'], ['C', 'B'], ['D', 'C'], ['X', 'Y'], ['Y', 'X'], ['A', 'A'],
  ['C', 'NOPE']];
console.log(JSON.stringify(Object.fromEntries(Core.treeDepths(nodes, edges))));''')
    # Depth counts same-era prerequisites only; the cross-era edge into D stays 0.
    assert out == {'A': 0, 'B': 1, 'C': 2, 'D': 0, 'X': 0, 'Y': 0}


def test_every_match_room_script_parses_under_node(tmp_path):
    if NODE is None:
        pytest.fail('node is required')
    scripts = sorted(ASSETS.glob('*.js'))
    assert [script.name for script in scripts] == ['app.js', 'compare-core.js', 'compare.js',
                                                   'journal-core.js', 'journal.js']
    for script in scripts:
        proc = subprocess.run([NODE, '--check', str(script)], capture_output=True, text=True,
                              timeout=60)
        assert proc.returncode == 0, (script.name, proc.stderr)
        assert script.read_text().startswith("'use strict';")
