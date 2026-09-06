"""Optional native terrain evidence, separate from simulator movement classes."""
from __future__ import annotations

import re
from typing import Any

_BIOMES = frozenset({'GRASS', 'PLAINS', 'DESERT', 'TUNDRA', 'SNOW'})
_TOKEN = re.compile(r'(?:TERRAIN_)?[A-Z][A-Z0-9_]{0,63}', re.ASCII)


def native_terrain(token: Any) -> dict:
    """Keep bounded source vocabulary; derive only known base-game type semantics."""
    raw = token if isinstance(token, str) and len(token) <= 64 and _TOKEN.fullmatch(token) else None
    name = raw.removeprefix('TERRAIN_') if raw is not None else ''
    biome = name.removesuffix('_HILLS')
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
