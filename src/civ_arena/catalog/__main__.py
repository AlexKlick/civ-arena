"""Offline extraction/query CLI; JSON artifacts are separate from live engine data."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from civ_arena.canonical import atomic_write_text, canonical
from civ_arena.catalog.base import CatalogError, extract, query
from civ_arena.catalog.projection import MAX_OBSERVATION_BYTES, MAX_PROJECTION_BYTES, project
from civ_arena.catalog.viewer import render


def _unique_object(pairs: list[tuple]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise CatalogError('duplicate observation JSON field')
        result[key] = value
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    build = commands.add_parser('extract')
    build.add_argument('--asset-root', type=Path, required=True)
    build.add_argument('--output', type=Path, required=True)
    lookup = commands.add_parser('query')
    lookup.add_argument('--catalog', type=Path, required=True)
    lookup.add_argument('node_id')
    lookup.add_argument('--depth', type=int, default=3)
    lookup.add_argument('--output', type=Path)
    overlay = commands.add_parser('project')
    overlay.add_argument('--catalog', type=Path, required=True)
    overlay.add_argument('--observations', type=Path, required=True)
    overlay.add_argument('node_id')
    overlay.add_argument('--depth', type=int, default=3)
    overlay.add_argument('--output', type=Path)
    preview = commands.add_parser('render')
    preview.add_argument('--projection', type=Path, action='append', required=True)
    preview.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.command == 'render':
            if len(args.projection) > 8:
                raise CatalogError('viewer example bound exceeded')
            projections = []
            for path in args.projection:
                if path.stat().st_size > MAX_PROJECTION_BYTES:
                    raise CatalogError('viewer projection byte bound exceeded')
                data = path.read_bytes()
                if len(data) > MAX_PROJECTION_BYTES:
                    raise CatalogError('viewer projection byte bound exceeded')
                projections.append(json.loads(data, object_pairs_hook=_unique_object))
            html = render(projections)
            atomic_write_text(args.output, html)
            print(json.dumps({'output': str(args.output), 'scope': 'synthetic_source_graph_preview',
                              'projections': [doc['projection_digest'] for doc in projections]},
                             sort_keys=True))
            return
        if args.command == 'extract':
            doc = extract(args.asset_root)
        else:
            if args.catalog.stat().st_size > 64 * 1024 * 1024:
                raise CatalogError('catalog artifact size bound exceeded')
            catalog = json.loads(args.catalog.read_text())
            if args.command == 'project':
                if args.observations.stat().st_size > MAX_OBSERVATION_BYTES:
                    raise CatalogError('observation byte bound exceeded')
                data = args.observations.read_bytes()
                if len(data) > MAX_OBSERVATION_BYTES:
                    raise CatalogError('observation byte bound exceeded')
                observations = json.loads(data, object_pairs_hook=_unique_object)
                doc = project(catalog, args.node_id, observations, args.depth)
            else:
                doc = query(catalog, args.node_id, args.depth)
        if args.output:
            atomic_write_text(args.output, canonical(doc) + '\n')
            print(json.dumps({'output': str(args.output), 'catalog_digest': doc['catalog_digest'],
                              'scope': doc['scope'], 'nodes': len(doc['nodes']),
                              'edges': len(doc['edges'])}, sort_keys=True))
        else:
            print(json.dumps(doc, indent=2, sort_keys=True))
    except (CatalogError, OSError, ValueError) as exc:
        parser.exit(2, f'catalog error: {exc}\n')


if __name__ == '__main__':
    main()
