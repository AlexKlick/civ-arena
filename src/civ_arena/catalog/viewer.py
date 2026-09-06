"""Render supplied projection artifacts into a self-contained offline preview."""
from __future__ import annotations

import base64
import hashlib
import re
from pathlib import Path

from civ_arena.canonical import CanonicalError, args_digest, canonical
from civ_arena.catalog.base import CatalogError
from civ_arena.catalog.projection import MAX_PROJECTION_BYTES

MAX_EXAMPLES = 8
MAX_BUNDLE_BYTES = 8 * 1024 * 1024
MAX_HTML_BYTES = 12 * 1024 * 1024
_HASH = re.compile(r'[0-9a-f]{64}')


def _validate(projection: dict) -> None:
    if (not isinstance(projection, dict) or type(projection.get('projection_version')) is not int
            or projection.get('projection_version') != 1
            or projection.get('scope') != 'offline_observed_dependency_projection'):
        raise CatalogError('unsupported viewer projection')
    digest = projection.get('projection_digest')
    try:
        expected = args_digest({key: value for key, value in projection.items()
                                if key != 'projection_digest'})
    except CanonicalError as exc:
        raise CatalogError('unsupported viewer JSON value') from exc
    if not isinstance(digest, str) or not _HASH.fullmatch(digest) or digest != expected:
        raise CatalogError('viewer projection digest mismatch')
    if len(canonical(projection).encode()) > MAX_PROJECTION_BYTES:
        raise CatalogError('viewer projection byte bound exceeded')
    if (projection.get('effective_ruleset') != 'unverified'
            or projection.get('current_feasibility') != 'unknown'):
        raise CatalogError('viewer requires explicit unknown effective-rule feasibility')
    for field in ('catalog_digest', 'observation_digest'):
        if not isinstance(projection.get(field), str) or not _HASH.fullmatch(projection[field]):
            raise CatalogError('invalid viewer source identity')
    nodes = projection.get('nodes')
    if not isinstance(nodes, list) or not 1 <= len(nodes) <= 512:
        raise CatalogError('viewer node bound exceeded')
    ids = []
    for node in nodes:
        if (not isinstance(node, dict) or not isinstance(node.get('id'), str)
                or not 1 <= len(node['id']) <= 256):
            raise CatalogError('invalid viewer node')
        if not isinstance(node.get('kind'), str) or not isinstance(node.get('observations'), dict):
            raise CatalogError('invalid viewer node annotations')
        for fact in node['observations'].values():
            if (not isinstance(fact, dict)
                    or fact.get('evidence_class') not in ('unknown', 'supplied_observation')
                    or (fact['evidence_class'] == 'supplied_observation'
                        and (type(fact.get('value')) is not bool
                             or not isinstance(fact.get('evidence_id'), str)))):
                raise CatalogError('invalid viewer observation annotation')
        ids.append(node['id'])
    if len(set(ids)) != len(ids) or projection.get('root') not in ids:
        raise CatalogError('duplicate viewer node or absent root')
    edge_ids, targets = set(), set(ids)
    for field in ('edges', 'other_relations', 'unexpanded_edges'):
        rows = projection.get(field)
        if not isinstance(rows, list):
            raise CatalogError('invalid viewer edge list')
        for edge in rows:
            if (not isinstance(edge, dict) or not isinstance(edge.get('id'), str)
                    or not _HASH.fullmatch(edge['id']) or edge['id'] in edge_ids
                    or not isinstance(edge.get('relation'), str)
                    or edge.get('source') not in ids
                    or not isinstance(edge.get('target'), str)
                    or not 1 <= len(edge['target']) <= 256
                    or (field != 'unexpanded_edges' and edge['target'] not in ids)):
                raise CatalogError('invalid viewer edge identity or endpoint')
            edge_ids.add(edge['id'])
            targets.add(edge['target'])
    if len(edge_ids) > 2048 or len(targets) > 1024:
        raise CatalogError('viewer graph bound exceeded')
    groups = projection.get('prerequisite_options')
    if not isinstance(groups, list) or any(
            not isinstance(group, dict) or group.get('connective') != 'unverified'
            or group.get('source') not in ids or not isinstance(group.get('relation'), str)
            or not isinstance(group.get('why_unverified'), str)
            or not isinstance(group.get('edge_ids'), list)
            or any(not isinstance(key, str) or key not in edge_ids for key in group['edge_ids'])
            for group in groups):
        raise CatalogError('viewer requires unverified prerequisite connective')
    if (not isinstance(projection.get('observation_snapshot'), dict)
            or type(projection.get('depth')) is not int or not 0 <= projection['depth'] <= 8):
        raise CatalogError('invalid viewer snapshot or depth')


def render(projections: list[dict]) -> str:
    """No file/network discovery: only supplied artifacts and packaged static assets."""
    if not isinstance(projections, list) or not 1 <= len(projections) <= MAX_EXAMPLES:
        raise CatalogError('viewer example bound exceeded')
    for projection in projections:
        _validate(projection)
    data = canonical(projections)
    if len(data.encode()) > MAX_BUNDLE_BYTES:
        raise CatalogError('viewer bundle byte bound exceeded')
    # Embedded JSON cannot close its script element, including arbitrary source labels.
    data = data.replace('&', r'\u0026').replace('<', r'\u003c').replace('>', r'\u003e')
    assets = Path(__file__).parent
    script = (assets / 'viewer.js').read_text()
    style = (assets / 'viewer.css').read_text()
    template = (assets / 'viewer.html').read_text()
    digest = base64.b64encode(hashlib.sha256(script.encode()).digest()).decode()
    html = template.replace('<!-- STYLE -->', style).replace('<!-- SCRIPT_HASH -->', digest)
    html = html.replace('<!-- SCRIPT -->', script).replace('<!-- DATA -->', data)
    if len(html.encode()) > MAX_HTML_BYTES:
        raise CatalogError('viewer HTML byte bound exceeded')
    return html
