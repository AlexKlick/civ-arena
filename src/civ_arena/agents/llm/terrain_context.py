"""Lossless compact rendering of projected observations for model requests."""
from __future__ import annotations

import json


def compact_terrain(doc: dict) -> dict:
    """Deduplicate native metadata without dropping coordinates or ownership facts.

    Only the supplied retained rows contribute palette entries. Null cells mean
    the corresponding source field was absent, not an observed empty value.
    Controller planning continues to use its unchanged full projected state.
    """
    palette: list[dict] = []
    indexes: dict[str, int] = {}
    rows = []
    for tile in doc['terrain']:
        native = tile.get('native_terrain')
        native_id = None
        if native is not None:
            key = json.dumps(native, sort_keys=True, separators=(',', ':'))
            if key not in indexes:
                indexes[key] = len(palette)
                palette.append(dict(native))
            native_id = indexes[key]
        rows.append([tile['coord'], tile['terrain'], native_id,
                     tile.get('owner_id'), tile.get('city_id')])
    return {**doc, 'terrain': rows, 'terrain_encoding': 'column_rows_v1',
            'terrain_columns': ['coord', 'terrain', 'native_index', 'owner_id', 'city_id'],
            'terrain_native_palette': palette,
            'terrain_decoding': 'Each terrain row follows terrain_columns. native_index '
                'indexes terrain_native_palette. Null means unavailable; owner_id=-1 '
                'and city_id="" are observed unowned/no-city, not missing.'}


def terrain_rows(doc: dict) -> list[dict]:
    """Expand generated context rows for evidence consumers, never engine input."""
    if 'terrain_encoding' not in doc:
        return doc['terrain']
    if (doc['terrain_encoding'] != 'column_rows_v1' or doc['terrain_columns'] !=
            ['coord', 'terrain', 'native_index', 'owner_id', 'city_id']):
        raise ValueError('unsupported terrain context encoding')
    palette = doc['terrain_native_palette']
    result = []
    for row in doc['terrain']:
        if not isinstance(row, list) or len(row) != 5:
            raise ValueError('invalid terrain context row')
        coord, terrain, index, owner, city = row
        tile = {'coord': coord, 'terrain': terrain}
        if index is not None:
            if type(index) is not int or not 0 <= index < len(palette):
                raise ValueError('invalid native terrain palette index')
            tile['native_terrain'] = dict(palette[index])
        if owner is not None:
            tile['owner_id'] = owner
        if city is not None:
            tile['city_id'] = city
        result.append(tile)
    return result


def compact_entities(doc: dict) -> dict:
    """Share actor field names when a dispersed roster needs the remaining budget."""
    result = dict(doc)
    columns = {}
    for kind in ('own_units', 'own_cities', 'visible_foreign_units', 'visible_foreign_cities'):
        actors = doc[kind]
        # Explicit null values remain verbatim objects. In packed groups a null
        # therefore means only an absent source field, preserving exact roundtrip.
        if not actors or any(value is None for actor in actors for value in actor.values()):
            continue
        fields = sorted({field for actor in actors for field in actor})
        columns[kind] = fields
        result[kind] = [[actor.get(field) for field in fields] for actor in actors]
    if columns:
        result['entity_columns'] = columns
        result['entity_decoding'] = ('Groups named in entity_columns contain rows in that '
                                     'column order. Null cells mean unavailable source fields.')
    return result


def entity_rows(doc: dict, kind: str) -> list[dict]:
    """Expand generated actor tables for evidence consumers."""
    columns = doc.get('entity_columns', {}).get(kind)
    if columns is None:
        return doc[kind]
    result = []
    for row in doc[kind]:
        if not isinstance(row, list) or len(row) != len(columns):
            raise ValueError('invalid entity context row')
        result.append({key: value for key, value in zip(columns, row, strict=True)
                       if value is not None})
    return result
