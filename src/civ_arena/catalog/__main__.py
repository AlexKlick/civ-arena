"""Offline extraction/query CLI; JSON artifacts are separate from live engine data."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from civ_arena.canonical import atomic_write_text, canonical
from civ_arena.catalog.base import CatalogError, extract, query


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
    args = parser.parse_args()
    try:
        if args.command == 'extract':
            doc = extract(args.asset_root)
        else:
            if args.catalog.stat().st_size > 64 * 1024 * 1024:
                raise CatalogError('catalog artifact size bound exceeded')
            doc = query(json.loads(args.catalog.read_text()), args.node_id, args.depth)
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
