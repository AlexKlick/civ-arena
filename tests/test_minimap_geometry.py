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


def test_concatenated_script_passes_node_check(tmp_path):
    if NODE is None:
        pytest.fail('node is required')
    target = tmp_path / 'atlas.js'
    target.write_text(script_source())
    proc = subprocess.run([NODE, '--check', str(target)], capture_output=True, text=True,
                          timeout=60)
    assert proc.returncode == 0, proc.stderr
    assert script_source().startswith("'use strict';")
