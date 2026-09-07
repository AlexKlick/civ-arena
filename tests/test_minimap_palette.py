"""The atlas palette is one source: fills, glyphs, owners, tokens and the generated key."""
import base64
import hashlib
import json
import re

import pytest

from civ_arena.game.terrain_metadata import _BIOMES
from civ_arena.minimap import ASSETS, build, load_palette, render, script_source, tokens_css
from test_minimap import bind, packet

PALETTE = load_palette()
DASHBOARD_CSS = ASSETS.with_name('dashboard_static') / 'style.css'
KNOWN_UNITS = {'SETTLER', 'BUILDER', 'WARRIOR', 'SCOUT', 'SLINGER', 'ARCHER', 'SPEARMAN',
               'SUMERIAN_WAR_CART', 'GALLEY', 'QUADRIREME'}


def luminance(hex_color):
    def channel(value):
        c = int(value, 16) / 255
        return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = (channel(hex_color[i:i + 2]) for i in (1, 3, 5))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast(a, b):
    hi, lo = sorted((luminance(a), luminance(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def test_palette_terrain_keys_cover_native_vocabulary():
    assert set(PALETTE['terrain']) >= _BIOMES | {'COAST', 'OCEAN', 'MOUNTAIN', 'UNKNOWN'}
    aliases = PALETTE['terrain_aliases']
    assert aliases['GRASSLAND'] == 'GRASS' and aliases['HILL'] == 'UNKNOWN'
    assert set(aliases.values()) <= set(PALETTE['terrain'])
    assert PALETTE['terrain']['UNKNOWN']['hatch']


def test_palette_unit_glyphs_cover_known_types_and_fallback():
    units = PALETTE['units']
    assert set(units) >= KNOWN_UNITS
    glyphs = [u['glyph'] for u in units.values()]
    assert len(set(glyphs)) == len(glyphs) and all(1 <= len(g) <= 2 for g in glyphs)
    assert all(u['role'] in PALETTE['roles'] for u in units.values())
    assert PALETTE['unit_fallback'] == {'glyph': '?', 'role': 'unknown', 'label': 'unknown type'}
    assert PALETTE['roles']['unknown']['dashed'] is True


def test_hills_mountain_and_cold_biomes_are_distinct():
    terrain = PALETTE['terrain']
    fills = [row['fill'] for row in terrain.values()]
    assert len(set(fills)) == len(fills)
    assert terrain['TUNDRA']['fill'] != terrain['SNOW']['fill']
    assert set(PALETTE['elevation']) == {'hills', 'mountain'}
    assert PALETTE['borders']['frontier']['dash'] is None
    assert PALETTE['borders']['unknown_beyond']['dash']


def test_contrast_floors_for_glyph_ink_halo_and_badges():
    tokens = PALETTE['tokens']
    ink = tokens['glyph_ink']
    for key in ('GRASS', 'PLAINS', 'DESERT', 'TUNDRA', 'SNOW', 'MOUNTAIN'):
        assert contrast(ink, PALETTE['terrain'][key]['fill']) >= 3.0, key
    badges = [tokens['seat0'], tokens['seat1'], tokens['foreign'], tokens['coral'],
              *PALETTE['owners']['majors'], PALETTE['owners']['major_overflow'],
              PALETTE['owners']['nonmajor']['stroke'], PALETTE['owners']['unclassified']['stroke']]
    for colour in badges:
        assert contrast(ink, colour) >= 4.5, colour
    for key in ('COAST', 'OCEAN'):
        assert contrast(tokens['halo'], PALETTE['terrain'][key]['fill']) >= 3.0, key


def test_match_room_tokens_match_atlas_palette():
    root = re.search(r':root\{([^}]*)\}', DASHBOARD_CSS.read_text())[1]
    tokens = dict(re.findall(r'--([a-z-]+):(#[0-9a-fA-F]{6,8})', root))
    atlas = PALETTE['tokens']
    assert tokens['gold'] == atlas['seat0'] and tokens['teal'] == atlas['seat1']
    for css_name, atlas_name in (('chart-gold', 'chart_seat0'), ('chart-teal', 'chart_seat1'),
                                 ('chart-action', 'chart_action'), ('chart-note', 'chart_note'),
                                 ('chart-observation', 'chart_observation')):
        assert tokens[css_name] == atlas[atlas_name], css_name


def test_minimap_style_has_no_literal_colors_and_only_known_tokens():
    css = (ASSETS / 'style.css').read_text()
    assert re.findall(r'#[0-9a-fA-F]{3,8}\b', css) == []
    used = set(re.findall(r'var\(--([a-z0-9_]+)\)', css))
    assert used <= set(PALETTE['tokens']), used - set(PALETTE['tokens'])


@pytest.mark.parametrize('tokens', [{'bg': 'red;}body{'}, {'Bad Key': '#000000'},
                                    {'bg': '#00000'}, {}])
def test_tokens_css_rejects_unsafe_values(tokens):
    with pytest.raises(ValueError):
        tokens_css({'tokens': tokens})


def test_tokens_css_emits_custom_properties_in_order():
    assert tokens_css({'tokens': {'bg': '#0a1419', 'mini_bg': '#0a141dea'}}) == \
        ':root{--bg:#0a1419;--mini_bg:#0a141dea;}'


def test_render_embeds_palette_block_tokens_and_single_script_hash():
    p = packet()
    hostile = '</script><script>window.pwned=1</script>@@PALETTE@@ & <img src=x>'
    p['projected_state']['public'] = {'players': [{'player_id': 0, 'civ_name': hostile,
                                                   'alive': True}], 'turn': 1}
    result = build([bind(p)], [], player=0)
    html = render(result)
    palette = re.search(r'<script id="palette" type="application/json">(.*?)</script>', html,
                        re.DOTALL)[1]
    assert json.loads(palette) == PALETTE
    data = re.search(r'<script id="data" type="application/json">(.*?)</script>', html,
                     re.DOTALL)[1]
    assert json.loads(data) == result
    assert hostile not in html and html.count('window.pwned') == 1
    assert ':root{--bg:#0a1419;' in html
    script = script_source()
    expected = base64.b64encode(hashlib.sha256(script.encode()).digest()).decode()
    assert f"script-src 'sha256-{expected}'" in html and html.count('<script>') == 1
    assert 'textContent' in html and 'innerHTML' not in html and 'fetch(' not in html
    for name in ('geometry.js', 'app.js', 'style.css', 'index.html'):
        source = (ASSETS / name).read_text()
        assert 'innerHTML' not in source and 'fetch(' not in source, name
    assert 'innerHTML' not in json.dumps(PALETTE) and 'fetch(' not in json.dumps(PALETTE)
