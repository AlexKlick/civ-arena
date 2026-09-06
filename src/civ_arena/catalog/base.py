"""Read five base XML sources and declared schema into a provenance-bearing graph."""
from __future__ import annotations

import copy
import hashlib
import re
import sqlite3
import xml.etree.ElementTree as ET
from collections import Counter
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from civ_arena.canonical import args_digest

PREFIX = 'base/assets/gameplay/data/'
XML_FILES = tuple(PREFIX + name + '.xml' for name in
                  ('technologies', 'civics', 'units', 'buildings', 'districts'))
SCHEMA_FILE = PREFIX + 'schema/01_gameplayschema.sql'
ENTITIES = {'Technologies': ('technology', 'TechnologyType'), 'Civics': ('civic', 'CivicType'),
            'Units': ('unit', 'UnitType'), 'Buildings': ('building', 'BuildingType'),
            'Districts': ('district', 'DistrictType')}
RELATIONS = {
    'TechnologyPrereqs': ('Technology', 'PrereqTech', 'requires_technology'),
    'CivicPrereqs': ('Civic', 'PrereqCivic', 'requires_civic'),
    'Unit_BuildingPrereqs': ('Unit', 'PrereqBuilding', 'requires_building'),
    'BuildingPrereqs': ('Building', 'PrereqBuilding', 'requires_building'),
    'MutuallyExclusiveBuildings': ('Building', 'MutuallyExclusiveBuilding', 'mutually_exclusive'),
    'UnitReplaces': ('CivUniqueUnitType', 'ReplacesUnitType', 'replaces'),
    'BuildingReplaces': ('CivUniqueBuildingType', 'ReplacesBuildingType', 'replaces'),
    'DistrictReplaces': ('CivUniqueDistrictType', 'ReplacesDistrictType', 'replaces'),
    'UnitUpgrades': ('Unit', 'UpgradeUnit', 'upgrades_to'),
}
SUPPORTING = frozenset({'Requirements', 'RequirementArguments', 'RequirementSets',
                        'RequirementSetRequirements', 'Modifiers', 'ModifierArguments',
                        'TechnologyModifiers', 'CivicModifiers', 'UnitModifiers',
                        'BuildingModifiers', 'DistrictModifiers'})
SELECTED = ENTITIES.keys() | RELATIONS.keys() | SUPPORTING
MAX_BYTES = 4 * 1024 * 1024
MAX_ROWS = 20000
_DDL = re.compile(r'(CREATE TABLE\s+"?([A-Za-z][A-Za-z0-9_]*)"?\s*\(.*?\);)', re.S)


class CatalogError(ValueError):
    pass


def _read(root: Path, relative: str) -> bytes:
    """Resolve only fixed input paths; reject case ambiguity and escaped symlinks."""
    path = root.resolve(strict=True)
    for part in relative.split('/'):
        matches = [entry for entry in path.iterdir() if entry.name.casefold() == part.casefold()]
        if len(matches) != 1:
            raise CatalogError(f'missing or ambiguous source: {relative}')
        path = matches[0].resolve(strict=True)
        if not path.is_relative_to(root.resolve()):
            raise CatalogError(f'source escapes asset root: {relative}')
    if not path.is_file() or path.stat().st_size > MAX_BYTES:
        raise CatalogError(f'invalid source size: {relative}')
    data = path.read_bytes()
    if len(data) > MAX_BYTES:
        raise CatalogError(f'invalid source size: {relative}')
    return data


def _value(text: str, declared_type: str) -> Any:
    if declared_type == 'TEXT':
        return text
    if declared_type == 'BOOLEAN' and text.casefold() in ('true', 'false', '0', '1'):
        return text.casefold() in ('true', '1')
    if declared_type == 'INTEGER' and re.fullmatch(r'-?[0-9]{1,18}', text):
        return int(text)
    if declared_type == 'REAL':
        try:
            value = Decimal(text)
            if value.is_finite() and len(text) <= 64:
                return {'decimal': str(value)}  # Exact decimal, never binary float.
        except InvalidOperation:
            pass
    raise CatalogError(f'unsupported {declared_type} value')


def _default(text: str | None, declared_type: str) -> Any:
    if text is None or text.upper() == 'NULL':
        return None
    if text[:1] in ('"', "'") and text[-1:] == text[:1]:
        text = text[1:-1].replace(text[:1] * 2, text[:1])
    return _value(text, declared_type)


def _schema(data: bytes, tables: set[str], identity: dict) -> dict:
    """Inspect selected CREATE TABLE statements in memory, never execute a SQL script."""
    declarations: dict[str, str] = {}
    for statement, table in _DDL.findall(data.decode('utf-8-sig')):
        if table in tables:
            if table in declarations:
                raise CatalogError(f'duplicate schema declaration: {table}')
            declarations[table] = statement
    if tables - declarations.keys():
        raise CatalogError('missing selected schema declarations')
    output = {}
    with sqlite3.connect(':memory:') as db:
        for table, statement in sorted(declarations.items()):
            try:
                db.execute(statement)
                columns = {}
                info = db.execute(f'PRAGMA table_info("{table}")')
                for _, name, kind, required, default, pk in info:
                    columns[name] = {'type': kind, 'required': bool(required), 'primary_key': pk,
                                     'default_sql': default,
                                     'default_value': _default(default, kind)}
                if not columns:
                    raise CatalogError('empty schema declaration')
                output[table] = {'source': identity, 'statement_sha256': _sha(statement.encode()),
                                 'columns': columns, 'foreign_keys': [
                                     {'table': target, 'from': source, 'to': column}
                                     for _, _, target, source, column, *_ in
                                     db.execute(f'PRAGMA foreign_key_list("{table}")')]}
            except sqlite3.Error as exc:
                raise CatalogError(f'unsupported schema declaration: {table}') from exc
    return output


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _rows(data: bytes, identity: dict) -> tuple[list[dict], list[dict]]:
    if b'<!DOCTYPE' in data.upper() or b'<!ENTITY' in data.upper():
        raise CatalogError('XML declarations/entities unsupported')
    try:
        root = ET.fromstring(data)
    except ET.ParseError as exc:
        raise CatalogError('invalid XML') from exc
    if root.tag != 'GameInfo' or root.attrib:
        raise CatalogError('unsupported XML root')
    rows, excluded, occurrences = [], [], Counter()
    count = 0
    for table in root:
        occurrences[table.tag] += 1
        if table.attrib:
            raise CatalogError('table attributes unsupported')
        for number, row in enumerate(table, 1):
            count += 1
            if count > MAX_ROWS:
                raise CatalogError('source row bound exceeded')
            if row.tag != 'Row':
                raise CatalogError(f'unsupported XML operation: {row.tag}')
            values = dict(row.attrib)
            for field in row:
                if field.tag in values or field.attrib or len(field):
                    raise CatalogError('ambiguous XML field')
                values[field.tag] = (field.text or '').strip()
            if any(len(value) > 4096 for value in values.values()):
                raise CatalogError('field size bound exceeded')
            if table.tag in SELECTED:
                rows.append({'table': table.tag, 'raw': values, 'provenance': {
                    **identity, 'table': table.tag, 'table_occurrence': occurrences[table.tag],
                    'row': number, 'operation': 'Row'}})
        if table.tag not in SELECTED:
            excluded.append({**identity, 'table': table.tag,
                             'table_occurrence': occurrences[table.tag], 'rows': len(table),
                             'status': 'unmodeled_table'})
    return rows, excluded


def extract(asset_root: Path) -> dict:
    """No game state, player identity, active manifest, or inference input is accepted."""
    sources, rows, excluded = [], [], []
    schema_data = _read(asset_root, SCHEMA_FILE)
    schema_identity = {'path': SCHEMA_FILE, 'sha256': _sha(schema_data)}
    sources.append(schema_identity)
    for relative in XML_FILES:
        data = _read(asset_root, relative)
        identity = {'path': relative, 'sha256': _sha(data)}
        sources.append(identity)
        selected, unmodeled = _rows(data, identity)
        rows.extend(selected)
        excluded.extend(unmodeled)
        if len(rows) + sum(table['rows'] for table in excluded) > MAX_ROWS:
            raise CatalogError('bundle row bound exceeded')
    schema = _schema(schema_data, set(ENTITIES) | {row['table'] for row in rows}, schema_identity)
    definitions, nodes, supporting, edges, observed = {}, {}, [], [], []
    for row in rows:
        table, raw = row['table'], row['raw']
        columns = schema[table]['columns']
        if raw.keys() - columns.keys():
            raise CatalogError(f'unknown declared column: {table}')
        values = {key: _value(value, columns[key]['type']) for key, value in raw.items()}
        effective = {key: values.get(key, column['default_value'])
                     for key, column in columns.items()}
        if any(column['required'] and effective[key] is None for key, column in columns.items()):
            raise CatalogError(f'missing required field: {table}')
        keys = sorted((column['primary_key'], key) for key, column in columns.items()
                      if column['primary_key'])
        if not keys or any(effective[key] is None for _, key in keys):
            raise CatalogError(f'missing declared row key: {table}')
        provenance = {**row['provenance'], 'row_key': {key: effective[key] for _, key in keys}}
        identity = (table, args_digest(provenance['row_key']))
        if identity in definitions and definitions[identity] != effective:
            raise CatalogError(f'contradictory duplicate definition: {table}')
        definitions[identity] = effective
        observed.append({'table': table, 'values': effective, 'provenance': provenance})
        if table in ENTITIES:
            kind, key = ENTITIES[table]
            node_id = effective[key]
            if not isinstance(node_id, str) or not re.fullmatch(r'[A-Z][A-Z0-9_]{1,127}', node_id):
                raise CatalogError('invalid entity identity')
            if node_id in nodes and nodes[node_id]['kind'] != kind:
                raise CatalogError('contradictory cross-kind identity')
            if node_id not in nodes:
                nodes[node_id] = {'id': node_id, 'kind': kind, 'attributes': effective,
                                  'schema_table': table, 'declarations': []}
            nodes[node_id]['declarations'].append({'provenance': provenance,
                                                  'explicit_columns': sorted(raw)})
            for field, relation in (('PrereqTech', 'requires_technology'),
                                    ('PrereqCivic', 'requires_civic'),
                                    ('PrereqDistrict', 'requires_district')):
                if effective.get(field):
                    edges.append(_edge(node_id, effective[field], relation, provenance,
                                       {'field': field}, 'unverified'))
        elif table in RELATIONS:
            source, target, relation = RELATIONS[table]
            edges.append(_edge(effective[source], effective[target], relation, provenance,
                               {key: value for key, value in effective.items()
                                if key not in (source, target)}, 'unverified'))
        else:
            supporting.append({'table': table, 'values': effective, 'provenance': provenance,
                               'explicit_columns': sorted(raw)})
    if {node['kind'] for node in nodes.values()} != {kind for kind, _ in ENTITIES.values()}:
        raise CatalogError('missing entity table definitions')
    for edge in edges:
        if edge['source'] not in nodes or edge['target'] not in nodes:
            raise CatalogError('unresolved entity reference inside declared slice')
    unresolved = []
    observed_values = {(row['table'], column, args_digest(value))
                       for row in observed for column, value in row['values'].items()}
    for row in observed:
        for foreign in schema[row['table']]['foreign_keys']:
            value = row['values'][foreign['from']]
            if value is not None and (foreign['table'], foreign['to'], args_digest(value)) \
                    not in observed_values:
                unresolved.append({'provenance': row['provenance'], 'column': foreign['from'],
                                   'target_table': foreign['table'],
                                   'target_column': foreign['to'], 'target_value': value,
                                   'status': 'not_defined_in_selected_source_rows'})
    result = {'catalog_version': 1, 'scope': 'base_source_catalog',
              'effective_ruleset': 'unverified', 'current_feasibility': 'unknown',
              'schema_scope': 'selected_CREATE_TABLE_declarations_only',
              'source_files': sorted(sources, key=lambda source: source['path']),
              'schema': schema, 'nodes': [nodes[key] for key in sorted(nodes)],
              'edges': sorted(edges, key=lambda edge: edge['id']),
              'supporting_rows': supporting, 'unmodeled_tables': excluded,
              'unresolved_source_references': unresolved,
              'limits': ['Base declarations; enabled content/application order unknown.',
                         'No current player/city state, rates, forecasts or legal-action proof.',
                         'Prerequisite group semantics unverified; do not assume AND or OR.',
                         'Supporting references may be outside these five files.']}
    result['catalog_digest'] = args_digest(result)
    return result


def _edge(source: str, target: str, relation: str, provenance: dict,
          constraints: dict, semantics: str) -> dict:
    edge = {'source': source, 'target': target, 'relation': relation,
            'constraints': constraints, 'group_semantics': semantics, 'provenance': provenance}
    return {'id': args_digest(edge), **edge}


def query(catalog: dict, node_id: str, depth: int = 3) -> dict:
    """Return a bounded dependency neighborhood plus every selected edge's provenance."""
    if type(depth) is not int or not 0 <= depth <= 8:
        raise CatalogError('query depth must be 0..8')
    if not isinstance(catalog, dict) or type(catalog.get('catalog_version')) is not int:
        raise CatalogError('invalid catalog envelope')
    payload = {key: value for key, value in catalog.items() if key != 'catalog_digest'}
    if catalog.get('catalog_digest') != args_digest(payload) or catalog.get('catalog_version') != 1:
        raise CatalogError('catalog digest or version mismatch')
    nodes = {node['id']: node for node in catalog['nodes']}
    if node_id not in nodes:
        raise CatalogError('unknown catalog node')
    selected = {node_id}
    for _ in range(depth):
        frontier = frozenset(selected)
        selected.update(edge['target'] for edge in catalog['edges']
                        if edge['source'] in frontier)
        if len(selected) > 512:
            raise CatalogError('query node bound exceeded')
    edges = [edge for edge in catalog['edges']
             if edge['source'] in selected and edge['target'] in selected]
    unexpanded = [edge for edge in catalog['edges']
                  if edge['source'] in selected and edge['target'] not in selected]
    # Traverse only declared keys/foreign keys inside the selected supporting tables.
    # External common types are unresolved facts, not joins that pull in unrelated rows.
    known = {(nodes[key]['schema_table'], ENTITIES[nodes[key]['schema_table']][1], key)
             for key in selected}
    supporting, picked = [], set()
    changed = True
    while changed:
        changed = False
        for index, row in enumerate(catalog['supporting_rows']):
            if index in picked:
                continue
            definition = catalog['schema'][row['table']]
            handles = {(row['table'], column, row['values'][column])
                       for column, declaration in definition['columns'].items()
                       if declaration['primary_key']}
            handles.update((link['table'], link['to'], row['values'][link['from']])
                           for link in definition['foreign_keys']
                           if link['table'] in SELECTED)
            if handles & known:
                picked.add(index)
                supporting.append(row)
                known.update(handles)
                changed = True
                if len(supporting) > 2000:
                    raise CatalogError('query supporting-row bound exceeded')
    provenance_ids = {args_digest(row['provenance']) for row in supporting}
    provenance_ids.update(args_digest(declaration['provenance'])
                          for key in selected for declaration in nodes[key]['declarations'])
    unresolved = [reference for reference in catalog['unresolved_source_references']
                  if args_digest(reference['provenance']) in provenance_ids]
    tables = {nodes[key]['schema_table'] for key in selected}
    tables.update(edge['provenance']['table'] for edge in edges + unexpanded)
    tables.update(row['table'] for row in supporting)
    return copy.deepcopy({'catalog_digest': catalog['catalog_digest'], 'scope': catalog['scope'],
            'root': node_id, 'depth': depth, 'nodes': [nodes[key] for key in sorted(selected)],
            'edges': edges, 'unexpanded_edges': unexpanded,
            'query_complete': not unexpanded, 'supporting_rows': supporting,
            'schema': {table: catalog['schema'][table] for table in sorted(tables)},
            'unresolved_source_references': unresolved,
            'unresolved_conditions': catalog['limits'], 'current_feasibility': 'unknown'})
