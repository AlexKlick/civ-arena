"""Optional native terrain evidence, separate from simulator movement classes."""
from __future__ import annotations

import re
from typing import Any

_BIOMES = frozenset({'GRASS', 'PLAINS', 'DESERT', 'TUNDRA', 'SNOW'})
_MOUNTAIN_BIOMES = {f'{biome}_MOUNTAIN': biome for biome in _BIOMES}
_TOKEN = re.compile(r'(?:TERRAIN_)?[A-Z][A-Z0-9_]{0,63}', re.ASCII)
# M4: new tile keys ride a generic bounded-token gate ('' = observed none).
_FIELD_TOKEN = re.compile(r'[A-Z][A-Z0-9_]{0,63}', re.ASCII)

# M4 static tile keys: omniscient-doc scope (visible + remembered). They
# never enter model packets — terrain_fields() stays the closed allowlist —
# but the adapter's remembered cache keeps them (a remembered tile's
# feature/river is no more hidden than its terrain).
STATIC_TILE_FIELDS = ('feature', 'river')
# M4 dynamic tile keys: visible-only scope (stripped from remembered rows).
DYNAMIC_TILE_FIELDS = ('resource', 'improvement', 'district', 'appeal',
                       'engine_visible')


def native_terrain(token: Any) -> dict:
    """Keep bounded source vocabulary; derive only known base-game type semantics."""
    raw = token if isinstance(token, str) and len(token) <= 64 and _TOKEN.fullmatch(token) else None
    name = raw.removeprefix('TERRAIN_') if raw is not None else ''
    biome = _MOUNTAIN_BIOMES.get(name, name.removesuffix('_HILLS'))
    known = biome in _BIOMES or name in ('COAST', 'OCEAN')
    return {'type': raw, 'biome': biome if known else None,
            'hills': name.endswith('_HILLS') if known else None}


def terrain_fields(tile: dict) -> dict:
    """Closed static fields: remembered terrain must never carry hidden ownership."""
    out = {'terrain': tile['terrain']}
    if 'native_terrain' in tile:
        metadata = tile['native_terrain']
        out['native_terrain'] = native_terrain(
            metadata.get('type') if isinstance(metadata, dict) else None)
    return out


def _clean_field_token(value) -> str | None:
    """A bounded [A-Z0-9_] tile key ('' = observed none); None = unusable."""
    if value == '':
        return ''
    if type(value) is str and len(value) <= 64 and _FIELD_TOKEN.fullmatch(value):
        return value
    return None


def static_tile_fields(tile: dict) -> dict:
    """terrain_fields + the STATIC tile keys, type-revalidated: a malformed
    or non-boolean river, or an over-long feature token, is dropped (key
    absent), never passed through raw."""
    out = terrain_fields(tile)
    feature = _clean_field_token(tile.get('feature'))
    if feature is not None:
        out['feature'] = feature
    river = tile.get('river')
    if type(river) is bool:
        out['river'] = river
    return out


def visible_tile_fields(tile: dict) -> dict:
    """The DYNAMIC tile keys, type-revalidated: strict tokens, appeal a
    plain int in [-100, 100], engine_visible a real boolean. Anything else
    drops the key (absent = unread)."""
    out: dict = {}
    for key in ('resource', 'improvement', 'district'):
        token = _clean_field_token(tile.get(key))
        if token is not None:
            out[key] = token
    appeal = tile.get('appeal')
    if type(appeal) is int and -100 <= appeal <= 100:
        out['appeal'] = appeal
    engine_visible = tile.get('engine_visible')
    if type(engine_visible) is bool:
        out['engine_visible'] = engine_visible
    return out
