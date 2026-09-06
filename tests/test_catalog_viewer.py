"""Standalone HTML binds supplied artifacts without executable label interpolation."""
from __future__ import annotations

import base64
import copy
import hashlib
import json
import re
import subprocess
import sys

import pytest

from civ_arena.canonical import args_digest, canonical
from civ_arena.catalog.base import CatalogError, extract
from civ_arena.catalog.projection import project
from civ_arena.catalog.viewer import render
from test_base_source_catalog import assets as assets
from test_catalog_projection import fact, snapshot


def artifact(assets, root='BUILDING_LIBRARY', depth=3):
    catalog = extract(assets)
    return project(catalog, root, snapshot(catalog, [fact('TECH_POTTERY')]), depth)


def rebind(view):
    view['projection_digest'] = args_digest({key: value for key, value in view.items()
                                            if key != 'projection_digest'})


def embedded(html):
    pattern = r'<script id="projection-data" type="application/json">(.*?)</script>'
    return json.loads(re.search(pattern, html, re.S)[1])


def test_exact_artifact_custody_deterministic_html_and_independent_csp_hash(assets):
    views = [artifact(assets), artifact(assets, 'BUILDING_ARMORY')]
    before = copy.deepcopy(views)
    html = render(views)
    assert embedded(html) == views == before
    assert html == render(views)
    script = re.search(r'<script>(.*?)</script>', html, re.S)[1]
    digest = base64.b64encode(hashlib.sha256(script.encode()).digest()).decode()
    assert f"script-src 'sha256-{digest}'" in html
    assert "connect-src 'none'" in html and "default-src 'none'" in html
    assert not re.search(r'<(?:script|link|img)[^>]+(?:src|href)=', html)
    assert 'fetch(' not in script and 'innerHTML' not in script and 'textContent' in script
    assert 'OFFLINE · SYNTHETIC PREVIEW' in html
    assert 'not current live model thinking' in html


def test_arbitrary_source_label_cannot_close_data_script_or_replace_template_markers(assets):
    view = artifact(assets)
    payload = '</script><img src="https://invalid.example/secret" onerror="window.pwned=1">'
    payload += '<!-- SCRIPT --><!-- DATA --> & \u2028'
    view['nodes'][0]['attributes']['Name'] = payload
    rebind(view)
    html = render([view])
    assert payload not in html
    assert '\\u003c/script\\u003e' in html
    assert embedded(html) == [view]
    assert html.count('<script') == 2


def test_frontier_and_explicit_unknown_groups_remain_in_embedded_projection(assets):
    view = artifact(assets, depth=0)
    recovered = embedded(render([view]))[0]
    assert recovered['unexpanded_edges'] == view['unexpanded_edges']
    assert not recovered['query_complete'] and len(recovered['nodes']) == 1
    assert 'Depth frontier' in render([view])
    armory = embedded(render([artifact(assets, 'BUILDING_ARMORY')]))[0]
    assert all(group['connective'] == 'unverified' for group in armory['prerequisite_options'])
    assert any(edge['relation'] == 'mutually_exclusive' for edge in armory['other_relations'])


def test_tampered_digest_or_noncanonical_artifact_rejects(assets):
    view = artifact(assets)
    view['nodes'][0]['attributes']['Cost'] = 900
    with pytest.raises(CatalogError, match='digest mismatch'):
        render([view])
    view['nodes'][0]['attributes']['Cost'] = 1.5
    with pytest.raises(CatalogError, match='unsupported viewer JSON'):
        render([view])


@pytest.mark.parametrize('field,value', [
    ('projection_version', True), ('effective_ruleset', 'verified'),
    ('current_feasibility', 'ready'), ('root', 'MISSING'), ('nodes', []),
    ('observation_snapshot', None), ('depth', True), ('edges', None)])
def test_invalid_render_contract_rejects_even_with_recomputed_digest(assets, field, value):
    view = artifact(assets)
    view[field] = value
    rebind(view)
    with pytest.raises(CatalogError):
        render([view])


def test_duplicate_nodes_edges_and_invented_connective_reject(assets):
    original = artifact(assets)
    for mutate in (lambda doc: doc['nodes'].append(doc['nodes'][0]),
                   lambda doc: doc['edges'].append(doc['edges'][0]),
                   lambda doc: doc['prerequisite_options'][0].update(connective='all')):
        view = copy.deepcopy(original)
        mutate(view)
        rebind(view)
        with pytest.raises(CatalogError):
            render([view])


def test_example_and_bundle_limits_fail_without_mutation(assets, monkeypatch):
    import civ_arena.catalog.viewer as viewer
    view = artifact(assets)
    before = copy.deepcopy(view)
    for inputs in ([], [view]*9):
        with pytest.raises(CatalogError, match='example bound'):
            render(inputs)
    monkeypatch.setattr(viewer, 'MAX_BUNDLE_BYTES', 100)
    with pytest.raises(CatalogError, match='bundle byte bound'):
        render([view])
    assert view == before


def test_cli_explicit_multiple_artifacts_and_refusal_preserves_existing_output(assets, tmp_path):
    paths = [tmp_path/'library.json', tmp_path/'armory.json']
    views = [artifact(assets), artifact(assets, 'BUILDING_ARMORY')]
    for path, view in zip(paths, views, strict=True):
        path.write_text(canonical(view))
    output = tmp_path/'preview.html'
    command = [sys.executable, '-m', 'civ_arena.catalog', 'render', '--projection', str(paths[0]),
               '--projection', str(paths[1]), '--output', str(output)]
    run = subprocess.run(command, capture_output=True, text=True, timeout=10, check=True)
    assert json.loads(run.stdout)['projections'] == [view['projection_digest'] for view in views]
    assert embedded(output.read_text()) == views
    before = output.read_bytes()
    paths[0].write_text(paths[0].read_text().replace('"depth":3', '"depth":3,"depth":2'))
    result = subprocess.run(command, capture_output=True, text=True, timeout=10)
    assert result.returncode == 2 and 'duplicate' in result.stderr
    assert output.read_bytes() == before
