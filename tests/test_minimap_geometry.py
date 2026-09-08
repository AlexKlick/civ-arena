"""Pure atlas geometry contracts, evaluated under Node exactly as the browser runs them."""
import json
import shutil
import subprocess

import pytest

from civ_arena.minimap import ASSETS, load_palette, script_source

NODE = shutil.which('node')
GEOMETRY = (ASSETS / 'geometry.js').read_text()


def run(harness):
    if NODE is None:
        pytest.fail('node is required to evaluate geometry.js contracts (never skip them)')
    source = GEOMETRY + '\nconst palette = ' + json.dumps(load_palette()) + ';\n' + harness
    proc = subprocess.run([NODE, '-e', source], capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def test_neighbor_table_matches_edge_midpoints():
    errors = run('''
const out = [];
for (let i = 0; i < 6; i++) {
  const [x, y] = point('3,-2'), v = hexVertices(x, y), a = v[i], b = v[(i + 1) % 6];
  const [nx, ny] = point(neighbourCoord('3,-2', i));
  out.push(Math.hypot((a[0] + b[0]) / 2 - (x + nx) / 2, (a[1] + b[1]) / 2 - (y + ny) / 2));
}
console.log(JSON.stringify(out));''')
    assert len(errors) == 6 and all(e < 1e-6 for e in errors)


def test_border_segments_three_tile_fixture_without_duplicates():
    result = run('''
const row = (coord, obs, seq) => ({observation: {coord, ...obs},
                                   receipt: {seq, turn: 1, player_id: 0}});
const tiles = new Map([
  ['0,0', [row('0,0', {owner_id: 0}, 10)]], ['1,0', [row('1,0', {owner_id: 1}, 10)]],
  ['0,1', [row('0,1', {}, 10)]], ['-1,0', [row('-1,0', {owner_id: -1}, 10)]]]);
const resolve = alts => resolveOwnership(alts, () => 10);
const classify = id => ownerClass(id, [0, 1], [0, 1]);
const borders = borderSegments(tiles, resolve, classify, 1.6);
const groups = Object.fromEntries([...borders].map(([k, g]) => [k, g.segments.length]));
const tints = Object.fromEntries([...tintGroups(tiles, resolve, classify)]
  .map(([k, g]) => [k, g.hexes]));
const path = segmentPath(borders.get('seat:1|frontier|0').segments);
console.log(JSON.stringify({groups, tints, path}));''')
    assert result['groups'] == {'seat:0|frontier|0': 2, 'seat:0|unknown_beyond|0': 4,
                                'seat:1|frontier|0': 1, 'seat:1|unknown_beyond|0': 5}
    assert result['tints'] == {'seat:0|0': ['0,0'], 'seat:1|0': ['1,0']}
    assert result['path'].startswith('M') and result['path'].count('M') == 1


def test_ownership_three_way_and_owner_classes():
    out = run('''
const alts = (obs, seq) => [{observation: obs, receipt: {seq, turn: 1, player_id: 0}}];
const latest = () => 20;
const seats = [0, 1], roster = [0, 1, 3];
console.log(JSON.stringify({
  owned: resolveOwnership(alts({owner_id: 3}, 20), latest),
  unowned: resolveOwnership(alts({owner_id: -1}, 20), latest).status,
  nul: resolveOwnership(alts({owner_id: null}, 20), latest).status,
  unobserved: resolveOwnership(alts({}, 20), latest).status,
  stale: resolveOwnership(alts({owner_id: 2}, 10), latest).stale,
  newest: resolveOwnership([...alts({owner_id: 5}, 10), ...alts({}, 30),
                            ...alts({owner_id: 6}, 15)], latest).owner_id,
  classes: [ownerClass(0, seats, roster), ownerClass(3, seats, roster),
            ownerClass(6, seats, roster), ownerClass(6, seats, null)],
  colors: [ownerColor(0, 'seat', seats, roster, palette),
           ownerColor(1, 'seat', seats, roster, palette),
           ownerColor(3, 'major', seats, roster, palette),
           ownerColor(6, 'nonmajor', seats, roster, palette),
           ownerColor(6, 'unclassified', seats, null, palette),
           ownerColor(9, 'major', seats, [0, 1, 3, 4, 5, 9], palette)],
}));''')
    palette = load_palette()
    assert out['owned'] == {'status': 'owned', 'owner_id': 3, 'stale': False,
                            'receipt': {'seq': 20, 'turn': 1, 'player_id': 0}}
    assert (out['unowned'], out['nul'], out['unobserved']) == ('unowned', 'null_field',
                                                               'unobserved')
    assert out['stale'] is True and out['newest'] == 6
    assert out['classes'] == ['seat', 'major', 'nonmajor', 'unclassified']
    tokens, owners = palette['tokens'], palette['owners']
    assert out['colors'] == [tokens['seat0'], tokens['seat1'], owners['majors'][0],
                             owners['nonmajor']['stroke'], owners['unclassified']['stroke'],
                             owners['major_overflow']]


def test_unit_spec_unknown_fallback_never_echoes_token():
    out = run('''console.log(JSON.stringify([unitSpec('ZZZ', palette),
      unitSpec('</script>', palette), unitSpec(null, palette), unitSpec('SETTLER', palette),
      unitSpec('__proto__', palette)]));''')
    for spec in out[:3] + out[4:]:
        assert spec == {'glyph': '?', 'role': 'unknown', 'label': 'unknown type', 'known': False}
    assert out[3] == {'glyph': 'St', 'role': 'civilian', 'label': 'Settler', 'known': True}


def test_terrain_classification_and_names():
    out = run('''
const native = (type, biome, hills) => ({type, biome, hills});
const cases = [
  {terrain: 'HILL', native_terrain: native('TERRAIN_GRASS_HILLS', 'GRASS', true)},
  {terrain: 'MOUNTAIN', native_terrain: native('TERRAIN_PLAINS_MOUNTAIN', 'PLAINS', false)},
  {terrain: 'PLAINS', native_terrain: native('TERRAIN_TUNDRA', 'TUNDRA', false)},
  {terrain: 'PLAINS', native_terrain: native(null, null, null)},
  {terrain: 'PLAINS', native_terrain: native('TERRAIN_FOO', null, null)},
  {terrain: 'HILL'}, {terrain: 'GRASSLAND'}, {terrain: 'PLAINS', native_terrain: null},
  {terrain: 'COAST', native_terrain: native('TERRAIN_COAST', 'COAST', false)},
  {terrain: 'HILL', native_terrain: {biome: 'TUNDRA', hills: true}},
];
const rows = cases.map(t => [terrainName(t, palette), classifyTile(t, palette)]);
console.log(JSON.stringify(rows));
''')
    native, normalized = 'native', 'normalized'
    assert out == [
        ['Grassland hills', {'fillKey': 'GRASS', 'elevation': 'hills', 'source': native}],
        ['Plains mountain', {'fillKey': 'MOUNTAIN', 'elevation': 'mountain', 'source': native}],
        ['Tundra', {'fillKey': 'TUNDRA', 'elevation': None, 'source': native}],
        ['native type not supplied',
         {'fillKey': 'PLAINS', 'elevation': None, 'source': normalized}],
        ['unsupported native token TERRAIN_FOO',
         {'fillKey': 'PLAINS', 'elevation': None, 'source': normalized}],
        ['normalized class HILL (native type not supplied)',
         {'fillKey': 'UNKNOWN', 'elevation': 'hills', 'source': normalized}],
        ['normalized class GRASSLAND (native type not supplied)',
         {'fillKey': 'GRASS', 'elevation': None, 'source': normalized}],
        ['normalized class PLAINS (native type not supplied)',
         {'fillKey': 'PLAINS', 'elevation': None, 'source': normalized}],
        ['Coast', {'fillKey': 'COAST', 'elevation': None, 'source': native}],
        ['Tundra hills (native biome; type not supplied)',
         {'fillKey': 'TUNDRA', 'elevation': 'hills', 'source': native}],
    ]


DECISION_FIXTURE = '''
const decision = {origin: '0,0', reason: 'frontier_probe',
  selected: {action: 'move_unit', args: {unit_id: 'u0:1', dest: '1,-1'}, reason: 'chosen'},
  candidates: [
    {dest: '1,0', probability: 0.6, score: 10,
     components: {frontier_distance: 1, unexplored_gain: 5}},
    {dest: '0,1', probability: null},
    {dest: '-1,0', excluded: 'observed_occupied_tile', probability: 0.0},
    {dest: '1,-1', probability: 0.4},
    {dest: 'bad', probability: 0.9}]};
'''


def test_decision_shapes_state_recorded_weights_without_inferring_the_missing_ones():
    out = run(DECISION_FIXTURE + '''
const s = decisionShapes(decision);
console.log(JSON.stringify({
  origin: s.origin, action: s.action, reason: s.reason, chosen: s.chosen, labels: s.labels,
  dropped: s.dropped, coords: s.candidates.map(c => c.coord),
  widths: s.candidates.map(c => c.strokeWidth), weights: s.candidates.map(c => c.weight),
  unscored: s.candidates.map(c => c.unscored), excluded: s.candidates.map(c => c.excluded),
  isChosen: s.candidates.map(c => c.chosen), scores: s.candidates.map(c => c.score),
  components: s.candidates.map(c => c.components),
  firstVertex: s.candidates[0].points.split(' ')[0],
  insetRadius: s.candidates.every(c => c.points === hexPoints(c.coord, HEX_R - DECISION_INSET)),
  centres: s.candidates.map(c => [c.cx, c.cy]), destPoint: point('1,-1'),
  originPoint: point('0,0')}));''')
    assert out['origin'] == {'coord': '0,0', 'x': out['originPoint'][0], 'y': out['originPoint'][1]}
    assert (out['action'], out['reason']) == ('move_unit', 'frontier_probe')
    assert out['coords'] == ['1,0', '0,1', '-1,0', '1,-1'] and out['dropped'] == 1
    assert out['widths'] == [2.8, 1, 1, 2.2]
    assert out['weights'] == [0.6, None, 0.0, 0.4]
    assert out['unscored'] == [False, True, False, False]
    assert out['excluded'] == [None, None, 'observed_occupied_tile', None]
    assert out['isChosen'] == [False, False, False, True]
    assert out['scores'] == [10, None, None, None]
    assert out['components'] == [{'frontier_distance': 1, 'unexplored_gain': 5}, None, None, None]
    assert out['chosen'] == {'coord': '1,-1', 'x1': out['originPoint'][0],
                             'y1': out['originPoint'][1], 'x2': out['destPoint'][0],
                             'y2': out['destPoint'][1]}
    assert out['labels'] == [
        {'coord': '1,-1', 'x': out['destPoint'][0], 'y': out['destPoint'][1] - 17,
         'text': 'chosen · weight 0.40', 'side': 'above'},
        {'coord': '1,0', 'x': out['centres'][0][0], 'y': out['centres'][0][1] - 17,
         'text': 'weight 0.60', 'side': 'above'}]
    # Radius 14 - 2.5 keeps the outline inside the tile border it annotates.
    assert out['firstVertex'] == '34.21,5.75' and out['insetRadius'] is True


def test_recorded_weights_are_clamped_and_non_numbers_stay_unscored():
    out = run('''
const shape = probability => decisionShapes({origin: '0,0', selected: null,
  candidates: [{dest: '1,0', probability}]}).candidates[0];
console.log(JSON.stringify({
  rows: [1.5, -1, '0.5', 0, 1, undefined, NaN].map(p => {
    const c = shape(p);
    return [c.weight, c.strokeWidth, c.unscored];
  }),
  direct: [candidateStroke(null), candidateStroke(0), candidateStroke(1),
           candidateStroke(undefined)],
  weights: [weightOf({probability: 1.5}), weightOf({probability: -1}),
            weightOf({probability: '0.5'}), weightOf(null), weightOf({})]}));''')
    assert out['rows'] == [[1, 4, False], [0, 1, False], [None, 1, True], [0, 1, False],
                           [1, 4, False], [None, 1, True], [None, 1, True]]
    assert out['direct'] == [1, 1, 4, 1]
    assert out['weights'] == [1, 0, None, None, None]


def test_selection_without_a_destination_states_the_action_instead_of_an_edge():
    out = run(DECISION_FIXTURE + '''
const none = decisionShapes({...decision, selected: null});
const fortify = decisionShapes({...decision, selected: {action: 'fortify', args: {}}});
const elsewhere = decisionShapes({...decision,
  selected: {action: 'move_unit', args: {dest: '2,-2'}}});
console.log(JSON.stringify({
  none: {chosen: none.chosen, action: none.action, labels: none.labels,
         candidates: none.candidates.length},
  fortify: {chosen: fortify.chosen, action: fortify.action, labels: fortify.labels},
  elsewhere: {chosen: elsewhere.chosen, labels: elsewhere.labels,
              isChosen: elsewhere.candidates.map(c => c.chosen)},
  origin: point('0,0'), away: point('2,-2')}));''')
    assert out['none'] == {'chosen': None, 'action': None, 'labels': [], 'candidates': 4}
    assert out['fortify']['chosen'] is None and out['fortify']['action'] == 'fortify'
    assert out['fortify']['labels'] == [{'coord': '0,0', 'x': out['origin'][0],
                                         'y': out['origin'][1] - 17, 'side': 'above',
                                         'text': 'fortify (recorded)'}]
    # A chosen destination outside the recorded candidate set is still drawn and
    # labelled, and it carries no borrowed weight.
    assert out['elsewhere']['chosen'] == {'coord': '2,-2', 'x1': out['origin'][0],
                                          'y1': out['origin'][1], 'x2': out['away'][0],
                                          'y2': out['away'][1]}
    assert out['elsewhere']['isChosen'] == [False, False, False, False]
    assert out['elsewhere']['labels'][0] == {'coord': '2,-2', 'x': out['away'][0],
                                             'y': out['away'][1] - 17, 'side': 'above',
                                             'text': 'chosen · unscored'}
    assert out['elsewhere']['labels'][1]['text'] == 'weight 0.60'


def test_unusable_origin_or_candidate_list_drops_shapes_and_counts_them():
    out = run('''
const bad = decisionShapes({origin: 'bogus', candidates: [{dest: '1,0'}, {dest: '0,1'}],
                            selected: {action: 'move_unit', args: {dest: '1,0'}}});
console.log(JSON.stringify({
  bad, missing: decisionShapes({}), nonArray: decisionShapes({origin: '0,0',
    candidates: 'nope'}).candidates,
  junk: decisionShapes(null), rows: decisionShapes({origin: '0,0',
    candidates: [null, 7, {dest: '1,0'}]}).candidates.map(c => c.coord),
  droppedRows: decisionShapes({origin: '0,0', candidates: [null, 7, {dest: '1,0'}]}).dropped}));
''')
    assert out['bad'] == {'origin': None, 'action': 'move_unit', 'reason': None, 'chosen': None,
                          'candidates': [], 'labels': [], 'dropped': 2}
    assert out['missing'] == {'origin': None, 'action': None, 'reason': None, 'chosen': None,
                              'candidates': [], 'labels': [], 'dropped': 0}
    assert out['junk']['origin'] is None and out['junk']['dropped'] == 0
    assert out['nonArray'] == []
    assert out['rows'] == ['1,0'] and out['droppedRows'] == 2


def test_printed_weights_never_round_toward_certainty():
    out = run('''
const cases = [0, 1, 0.5, 0.0005527786369235994, 0.9994472213630762, 0.004999, 0.995001,
               0.005, 0.995, 0.004, 0.996, null, undefined, NaN, '0.5'];
const label = probability => decisionShapes({origin: '0,0',
  selected: {action: 'move_unit', args: {dest: '1,0'}},
  candidates: [{dest: '1,0', probability}]}).labels[0].text;
console.log(JSON.stringify({texts: cases.map(weightText),
                            labels: [0.9994472213630762, 0.0005527786369235994, 0.5, 0, 1]
                              .map(label)}));''')
    assert out['texts'] == ['0.00', '1.00', '0.50', '<0.01', '>0.99', '<0.01', '>0.99',
                            '0.01', '0.99', '<0.01', '>0.99',
                            'unscored', 'unscored', 'unscored', 'unscored']
    # The real T33 pair: 0.9994 is not certainty and 0.00055 is not zero.
    assert out['labels'] == ['chosen · weight >0.99', 'chosen · weight <0.01',
                             'chosen · weight 0.50', 'chosen · weight 0.00',
                             'chosen · weight 1.00']


def test_a_runner_up_label_moves_below_its_hex_instead_of_overprinting():
    # Real coordinates from minimax100-20260907T183425Z T33, unit u0:196608: the two
    # 0.4999999 candidates sit on one label row, 24.25px apart, and one label is
    # wider than a hex.
    out = run('''
const row = (dest, probability) => ({dest, probability});
const collide = decisionShapes({origin: '44,25',
  selected: {action: 'move_unit', args: {dest: '43,26'}},
  candidates: [row('43,26', 0.49999994373241896), row('44,26', 0.49999994373241896)]});
const apart = decisionShapes({origin: '44,25',
  selected: {action: 'move_unit', args: {dest: '43,26'}},
  candidates: [row('43,26', 0.5), row('43,25', 0.4)]});
console.log(JSON.stringify({
  collide: collide.labels, apart: apart.labels, below: point('44,26'),
  widths: collide.candidates.map(c => c.strokeWidth)}));''')
    texts = [label['text'] for label in out['collide']]
    # "↑ " states the direction so the label is never read as the hex it sits on.
    assert texts == ['chosen · weight 0.50', '↑ weight 0.50']
    assert [label['side'] for label in out['collide']] == ['above', 'below']
    # Below its own hex, 40px clear of the chosen label's row — never dropped.
    assert out['collide'][1]['y'] == out['below'][1] + 23
    assert out['collide'][1]['x'] == out['below'][0]
    assert abs(out['collide'][1]['y'] - out['collide'][0]['y']) > 10
    assert out['widths'] == [2.4999998311972567, 2.4999998311972567]
    # A candidate on another label row keeps the default placement.
    assert [label['text'] for label in out['apart']] == ['chosen · weight 0.50', 'weight 0.40']
    assert [label['side'] for label in out['apart']] == ['above', 'above']
    assert out['apart'][0]['y'] != out['apart'][1]['y']


def test_malformed_and_edge_recorded_rows_stay_honest():
    out = run('''
const base = (candidates, selected) => decisionShapes({origin: '0,0', selected, candidates});
const tied = base([{dest: '1,-1', probability: 0.9}, {dest: '2,0', probability: 0.4},
                   {dest: '1,1', probability: 0.4}],
                  {action: 'move_unit', args: {dest: '1,-1'}});
const excludedWeight = base([{dest: '1,0', probability: 0.6,
                              excluded: 'observed_threat_proximity'}], null);
console.log(JSON.stringify({
  infinities: [weightOf({probability: Infinity}), weightOf({probability: -Infinity})],
  infiniteWidths: base([{dest: '1,0', probability: Infinity}], null)
    .candidates.map(c => [c.weight, c.unscored, c.strokeWidth]),
  excludedWeight: excludedWeight.candidates.map(c => [c.excluded, c.weight, c.strokeWidth]),
  tie: tied.labels.map(l => [l.coord, l.text]),
  noArgs: base([{dest: '1,0', probability: 0.5}], {action: 'fortify'}),
  exclusions: base([{dest: '1,0', excluded: 7}, {dest: '0,1', excluded: true},
                    {dest: '-1,0', excluded: ''}], null).candidates.map(c => c.excluded),
  nonString: base([{dest: 7}, {dest: null}, {dest: ['1,0']}, {dest: '1,0'}], null)}));''')
    # Infinity is not a recorded weight: unscored, thinnest outline.
    assert out['infinities'] == [None, None]
    assert out['infiniteWidths'] == [[None, True, 1]]
    # Exclusion does not erase the recorded weight the outline states.
    assert out['excludedWeight'] == [['observed_threat_proximity', 0.6, 2.8]]
    # Ties break on the lexicographically smaller coordinate.
    assert out['tie'] == [['1,-1', 'chosen · weight 0.90'], ['1,1', 'weight 0.40']]
    # A selection object without args must not throw.
    assert out['noArgs']['chosen'] is None
    assert [label['text'] for label in out['noArgs']['labels']] == ['fortify (recorded)']
    assert len(out['noArgs']['candidates']) == 1
    # Only a non-empty string is an exclusion reason.
    assert out['exclusions'] == [None, None, None]
    assert [c['coord'] for c in out['nonString']['candidates']] == ['1,0']
    assert out['nonString']['dropped'] == 3


def test_decision_shapes_are_deterministic_for_the_same_recorded_row():
    out = run(DECISION_FIXTURE + '''
const text = JSON.stringify(decision);
const a = JSON.stringify(decisionShapes(JSON.parse(text)));
const b = JSON.stringify(decisionShapes(JSON.parse(text)));
console.log(JSON.stringify({equal: a === b, mutated: JSON.stringify(decision) === text}));''')
    assert out == {'equal': True, 'mutated': True}


def test_concatenated_script_passes_node_check(tmp_path):
    if NODE is None:
        pytest.fail('node is required')
    target = tmp_path / 'atlas.js'
    target.write_text(script_source())
    proc = subprocess.run([NODE, '--check', str(target)], capture_output=True, text=True,
                          timeout=60)
    assert proc.returncode == 0, proc.stderr
    assert script_source().startswith("'use strict';")
