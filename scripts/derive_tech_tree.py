"""Derive the match room's technology-tree asset from the base source catalog.

Read-only: this reads one retained catalog file and writes one static asset. It
never contacts the game, a provider or the network. The catalog is a base source
export whose effective ruleset and prerequisite group semantics are unverified;
that provenance travels with the asset instead of being asserted away here.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

DEFAULT_CATALOG = Path(
    '/home/alexk/civ-arena-base-catalog-20260906/runs/'
    'base-catalog-corrections-final-20260906/base-source-catalog.json')
DEFAULT_OUT = Path('src/civ_arena/dashboard_static/tech-tree.json')
ERAS = ('ANCIENT', 'CLASSICAL', 'MEDIEVAL', 'RENAISSANCE',
        'INDUSTRIAL', 'MODERN', 'ATOMIC', 'INFORMATION')
ERA_INDEX = {era: index for index, era in enumerate(ERAS)}
MAX_CATALOG_BYTES = 8 * 1024 * 1024
TECH_ID = re.compile(r'^[A-Z0-9_]{1,40}$')
MAX_ROW = 8


class DeriveError(ValueError):
    """The catalog does not support a tree the viewer may present as fact."""


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise DeriveError(f'duplicate catalog key {key!r}')
        result[key] = value
    return result


def load_catalog(path: Path):
    # Refuse an oversized catalog before reading it, not after.
    if path.stat().st_size > MAX_CATALOG_BYTES:
        raise DeriveError(f'catalog exceeds {MAX_CATALOG_BYTES} bytes: {path}')
    raw = path.read_bytes()
    if len(raw) > MAX_CATALOG_BYTES:
        raise DeriveError(f'catalog exceeds {MAX_CATALOG_BYTES} bytes: {path}')

    def reject_constant(value):
        raise DeriveError('non-finite catalog JSON number')

    catalog = json.loads(raw, object_pairs_hook=unique_object,
                         parse_constant=reject_constant)
    if not isinstance(catalog, dict):
        raise DeriveError('catalog root is not an object')
    if catalog.get('catalog_version') != 2:
        raise DeriveError(f'unsupported catalog_version {catalog.get("catalog_version")!r}')
    return catalog, hashlib.sha256(raw).hexdigest()


def text_field(catalog, key):
    value = catalog.get(key)
    if not isinstance(value, str) or not value or len(value) > 128:
        raise DeriveError(f'catalog {key} is not a bounded string')
    return value


def node_rows(catalog):
    nodes = catalog.get('nodes')
    if not isinstance(nodes, list):
        raise DeriveError('catalog nodes is not a list')
    result = {}
    for node in nodes:
        if not isinstance(node, dict) or node.get('kind') != 'technology':
            continue
        raw_id = node.get('id')
        if not isinstance(raw_id, str):
            raise DeriveError('technology node has no string id')
        tech = raw_id.removeprefix('TECH_')
        if not TECH_ID.match(tech):
            raise DeriveError(f'technology node id is unusable: {raw_id!r}')
        if tech in result:
            raise DeriveError(f'duplicate technology node {tech}')
        attributes = node.get('attributes')
        if not isinstance(attributes, dict):
            raise DeriveError(f'technology node {tech} has no attributes object')
        era = attributes.get('EraType')
        if not isinstance(era, str) or era.removeprefix('ERA_') not in ERA_INDEX:
            raise DeriveError(f'technology node {tech} has unknown era {era!r}')
        row = attributes.get('UITreeRow')
        if type(row) is not int or not -MAX_ROW <= row <= MAX_ROW:
            raise DeriveError(f'technology node {tech} has unusable UITreeRow {row!r}')
        cost = attributes.get('Cost')
        if type(cost) is not int or cost < 0:
            raise DeriveError(f'technology node {tech} has unusable Cost {cost!r}')
        result[tech] = {'id': tech, 'era': era.removeprefix('ERA_'), 'row': row, 'cost': cost}
    if not result:
        raise DeriveError('catalog has no technology nodes')
    return result


def edge_rows(catalog, nodes):
    edges = catalog.get('edges')
    if not isinstance(edges, list):
        raise DeriveError('catalog edges is not a list')
    result, semantics = [], set()
    for edge in edges:
        if not isinstance(edge, dict):
            continue
        provenance = edge.get('provenance')
        if not isinstance(provenance, dict) or provenance.get('table') != 'TechnologyPrereqs':
            continue
        if edge.get('relation') != 'requires_technology':
            raise DeriveError(f'prerequisite edge has relation {edge.get("relation")!r}')
        subject, prereq = edge.get('source'), edge.get('target')
        if not isinstance(subject, str) or not isinstance(prereq, str):
            raise DeriveError('prerequisite edge endpoints are not strings')
        subject, prereq = subject.removeprefix('TECH_'), prereq.removeprefix('TECH_')
        for name in (subject, prereq):
            if name not in nodes:
                raise DeriveError(f'prerequisite edge names unknown technology {name}')
        if ERA_INDEX[nodes[prereq]['era']] > ERA_INDEX[nodes[subject]['era']]:
            raise DeriveError(f'prerequisite {prereq} is later than its subject {subject}')
        semantics.add(edge.get('group_semantics'))
        result.append([subject, prereq])
    if not result:
        raise DeriveError('catalog has no technology prerequisite edges')
    if len(semantics) != 1:
        raise DeriveError(f'prerequisite group semantics are not uniform: {sorted(semantics)}')
    semantic = semantics.pop()
    if not isinstance(semantic, str) or len(semantic) > 128:
        raise DeriveError(f'prerequisite group semantics is not a bounded string: {semantic!r}')
    result.sort()
    if len(set(map(tuple, result))) != len(result):
        raise DeriveError('duplicate prerequisite edges')
    return result, semantic


def derive(path: Path):
    catalog, sha256 = load_catalog(path)
    nodes = node_rows(catalog)
    edges, semantics = edge_rows(catalog, nodes)
    ordered = sorted(nodes.values(), key=lambda n: (ERA_INDEX[n['era']], n['row'], n['id']))
    return {'version': 1, 'scope': text_field(catalog, 'scope'),
            'effective_ruleset': text_field(catalog, 'effective_ruleset'),
            'group_semantics': semantics,
            'catalog_digest': text_field(catalog, 'catalog_digest'),
            'source': {'name': path.name, 'sha256': sha256},
            'eras': list(ERAS), 'nodes': ordered, 'edges': edges}


def encode(tree):
    return json.dumps(tree, sort_keys=True, indent=1) + '\n'


def summarize(tree):
    counts = [sum(1 for node in tree['nodes'] if node['era'] == era) for era in ERAS]
    return (f'{len(tree["nodes"])} nodes, {len(tree["edges"])} edges, '
            f'per-era {counts}, catalog sha256 {tree["source"]["sha256"]}')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--catalog', type=Path, default=DEFAULT_CATALOG)
    parser.add_argument('--out', type=Path, default=DEFAULT_OUT)
    parser.add_argument('--check', action='store_true',
                        help='compare the regenerated bytes with the existing file')
    args = parser.parse_args(argv)
    try:
        rendered = encode(derive(args.catalog))
    except (DeriveError, OSError, UnicodeError, RecursionError) as error:
        print(f'tech tree not derivable: {error}', file=sys.stderr)
        return 2
    if args.check:
        current = args.out.read_text() if args.out.is_file() else ''
        if current == rendered:
            print(f'tech-tree.json is current: {summarize(json.loads(rendered))}')
            return 0
        print(f'tech-tree.json differs from {args.catalog}: '
              f'{len(current)} stored bytes vs {len(rendered)} derived bytes; '
              f'derived {summarize(json.loads(rendered))}', file=sys.stderr)
        return 1
    args.out.write_text(rendered)
    print(f'wrote {args.out}: {summarize(json.loads(rendered))}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
