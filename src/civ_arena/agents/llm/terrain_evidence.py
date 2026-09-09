"""Directional cold-biome observations, without inferred map dimensions or odds."""
from __future__ import annotations

from civ_arena.game.terrain_metadata import native_terrain


def cold_biome_evidence(tiles: dict, reference: str) -> dict:
    """Engine y == axial r increases north; screen rotation is irrelevant.

    Installed base maputilities.lua:431-455 names y=0 the southern search start
    and y=height-1 the northern start. This establishes only the grid convention.
    """
    origin_r = int(reference.split(',')[1])
    sectors = {name: {'known': 0, 'cold': 0, 'noncold': 0, 'unknown': 0}
               for name in ('north', 'same_row', 'south')}
    for key, tile in tiles.items():
        r = int(key.split(',')[1])
        sector = sectors['north' if r > origin_r else 'south' if r < origin_r else 'same_row']
        sector['known'] += 1
        metadata = tile.get('native_terrain')
        biome = native_terrain(metadata.get('type') if isinstance(metadata, dict)
                               else None)['biome']
        sector['unknown' if biome in (None, 'COAST', 'OCEAN')
               else 'cold' if biome in ('TUNDRA', 'SNOW')
               else 'noncold'] += 1
    hypothesis = None
    for side, opposite in (('north', 'south'), ('south', 'north')):
        if (sectors[side]['cold'] > 0 and sectors[opposite]['cold'] == 0
                and sectors[opposite]['noncold'] > 0):
            hypothesis = f'possible_{side}ern_periphery_unverified'
    return {'reference_coord': reference, 'axis': 'increasing_r_is_north',
            'coverage': 'known_projected_tiles_only_including_remembered',
            'sectors': sectors, 'map_edge_hypothesis': hypothesis,
            'limits': 'Water and missing biomes are unclassified; unexplored coverage is unknown. '
                      'No map bounds, distance '
                      'to edge, active climate model or calibrated probability.'}
