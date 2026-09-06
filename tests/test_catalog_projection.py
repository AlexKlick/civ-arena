"""Truthful offline overlays never turn source reachability into live feasibility."""
from __future__ import annotations

import copy
import json
import subprocess
import sys

import pytest

from civ_arena.canonical import args_digest, canonical
from civ_arena.catalog.base import PREFIX, CatalogError, extract
from civ_arena.catalog.projection import project
from test_base_source_catalog import assets as assets


def snapshot(catalog, facts=()):
    return {'observation_version': 1, 'catalog_digest': catalog['catalog_digest'],
            'snapshot_id': 'fixture-turn-12', 'player_id': 0, 'turn': 12, 'facts': list(facts)}


def fact(node, value=True, predicate='researched'):
    return {'node_id': node, 'predicate': predicate, 'value': value,
            'evidence_id': 'fixture-observation-1'}


def test_completion_is_observed_fact_and_missing_is_unknown_without_propagation(assets):
    catalog = extract(assets)
    observed = snapshot(catalog, [fact('TECH_POTTERY')])
    view = project(catalog, 'BUILDING_LIBRARY', observed)
    writing = next(row for row in view['nodes'] if row['id'] == 'TECH_WRITING')
    assert writing['observations']['researched']['status'] == 'not_observed'
    assert writing['observations']['unlocked']['status'] == 'not_observed'
    edge = next(edge for edge in view['edges'] if edge['source'] == 'TECH_WRITING')
    assert edge['target_observation']['value'] is True
    assert edge['target_observation']['evidence_class'] == 'supplied_observation'
    assert edge['evidence_class'] == 'source_fact' and edge['provenance']
    assert all(group['evaluation'] == 'unknown' for group in view['prerequisite_options'])
    assert all(group['connective'] == 'unverified' and group['why_unverified']
               for group in view['prerequisite_options'])
    assert view['effective_ruleset'] == 'unverified' and view['current_feasibility'] == 'unknown'


def test_explicit_false_and_unlock_are_distinct_from_missing_completion_or_city_state(assets):
    catalog = extract(assets)
    facts = [fact('TECH_WRITING', False), fact('TECH_WRITING', True, 'unlocked'),
             fact('DISTRICT_CAMPUS', True, 'unlocked')]
    view = project(catalog, 'BUILDING_LIBRARY', snapshot(catalog, facts))
    research = next(edge for edge in view['edges'] if edge['source'] == 'BUILDING_LIBRARY'
                    and edge['relation'] == 'requires_technology')
    district = next(edge for edge in view['edges'] if edge['relation'] == 'requires_district')
    assert research['target_observation']['value'] is False
    assert district['target_observation']['evidence_class'] == 'unknown'
    assert district['target_observation']['status'] == 'not_evaluated'
    writing = next(row for row in view['nodes'] if row['id'] == 'TECH_WRITING')
    assert writing['observations']['unlocked']['value'] is True
    assert writing['observations']['researched']['value'] is False


def test_armory_alternatives_and_replacements_keep_source_semantics(assets):
    catalog = extract(assets)
    view = project(catalog, 'BUILDING_ARMORY', snapshot(catalog, [
        fact('BUILDING_BARRACKS', True, 'unlocked')]))
    group = next(group for group in view['prerequisite_options']
                 if group['source'] == 'BUILDING_ARMORY')
    assert len(group['edge_ids']) == 2 and group['connective'] == 'unverified'
    assert any(edge['relation'] == 'mutually_exclusive' for edge in view['other_relations'])
    assert all(edge['target_observation']['status'] == 'not_evaluated' for edge in view['edges'])
    replacement = project(catalog, 'BUILDING_UNIQUE_LIBRARY', snapshot(catalog))
    assert any(edge['relation'] == 'replaces' for edge in replacement['other_relations'])


@pytest.mark.parametrize('operator', ['ALL', 'ANY'])
def test_explicit_requirement_set_operator_is_preserved_without_evaluating_or_transferring(assets,
                                                                                         operator):
    path = assets / PREFIX / 'buildings.xml'
    path.write_text(path.read_text().replace('REQUIREMENTSET_TEST_ALL',
                                            'REQUIREMENTSET_TEST_' + operator))
    catalog = extract(assets)
    view = project(catalog, 'BUILDING_LIBRARY', snapshot(catalog))
    group = view['source_requirement_sets'][0]
    assert group['operator'] == operator.lower() and group['evaluation'] == 'unknown'
    assert group['declaration']['provenance']
    assert len(group['memberships']) == 2
    assert any(row['values']['RequirementId'] == 'REQ_EXTERNAL' for row in group['memberships'])
    assert all(option['connective'] == 'unverified' for option in view['prerequisite_options'])


def test_depth_boundary_keeps_provenance_and_cannot_propagate_external_observations(assets):
    catalog = extract(assets)
    view = project(catalog, 'BUILDING_LIBRARY', snapshot(catalog, [fact('TECH_POTTERY')]), depth=0)
    assert len(view['nodes']) == 1 and not view['query_complete']
    assert not view['edges'] and not view['prerequisite_options']
    assert len(view['unexpanded_edges']) == 2
    assert all(edge['provenance'] for edge in view['unexpanded_edges'])
    assert 'TECH_POTTERY' not in canonical(view['nodes'])


def test_determinism_digest_invalidation_input_and_output_custody(assets):
    catalog = extract(assets)
    observed = snapshot(catalog, [fact('TECH_WRITING'), fact('TECH_POTTERY')])
    before = copy.deepcopy((catalog, observed))
    view = project(catalog, 'BUILDING_LIBRARY', observed)
    assert view == project(catalog, 'BUILDING_LIBRARY', observed)
    observed['facts'].reverse()
    assert view == project(catalog, 'BUILDING_LIBRARY', observed)
    observed['turn'] += 1
    later = project(catalog, 'BUILDING_LIBRARY', observed)
    assert later['projection_digest'] != view['projection_digest']
    assert later['observation_digest'] != view['observation_digest']
    observed = before[1]
    assert catalog == before[0]
    view['nodes'][0]['attributes']['Cost'] = 999
    view['observation_snapshot']['turn'] = 999
    view['source_requirement_sets'][0]['declaration']['values']['RequirementSetType'] = 'BAD'
    assert catalog == before[0] and observed == before[1]
    assert later['projection_digest'] == args_digest({key: value for key, value in later.items()
                                                     if key != 'projection_digest'})


@pytest.mark.parametrize('changes', [
    {'observation_version': True}, {'catalog_digest': 'wrong'}, {'player_id': True},
    {'turn': 0}, {'snapshot_id': '../private/path'}, {'extra_private_state': 'secret'}])
def test_invalid_or_extra_snapshot_fields_reject(assets, changes):
    catalog = extract(assets)
    observed = snapshot(catalog)
    observed.update(changes)
    with pytest.raises(CatalogError):
        project(catalog, 'BUILDING_LIBRARY', observed)


@pytest.mark.parametrize('row', [
    fact('TECH_MISSING'), fact('BUILDING_LIBRARY'), fact('TECH_POTTERY', 1),
    fact('TECH_POTTERY', True, 'built'), {**fact('TECH_POTTERY'), 'evidence_id': ''},
    {**fact('TECH_POTTERY'), 'private_message': 'secret'}])
def test_invalid_or_wrong_scope_facts_reject(assets, row):
    catalog = extract(assets)
    with pytest.raises(CatalogError):
        project(catalog, 'BUILDING_LIBRARY', snapshot(catalog, [row]))


@pytest.mark.parametrize('second', [True, False])
def test_duplicate_or_conflicting_observations_reject(assets, second):
    catalog = extract(assets)
    with pytest.raises(CatalogError, match='duplicate observation'):
        project(catalog, 'BUILDING_LIBRARY', snapshot(catalog, [
            fact('TECH_POTTERY'), fact('TECH_POTTERY', second)]))


def test_civic_completion_and_unverified_snapshot_authority(assets):
    catalog = extract(assets)
    view = project(catalog, 'CIVIC_CRAFTSMANSHIP', snapshot(catalog, [fact('CIVIC_CODE_OF_LAWS')]))
    assert view['edges'][0]['target_observation']['value'] is True
    assert 'not verified' in view['observation_authority']
    assert view['observation_snapshot']['player_id'] == 0
    assert not any('probability' in key or 'ready' in key for key in view)


def test_projection_bounds_reject_without_mutating_inputs(assets, monkeypatch):
    import civ_arena.catalog.projection as module
    catalog = extract(assets)
    observed = snapshot(catalog, [fact('TECH_POTTERY')])
    before = copy.deepcopy((catalog, observed))
    monkeypatch.setattr(module, 'MAX_FACTS', 0)
    with pytest.raises(CatalogError, match='fact bound'):
        project(catalog, 'BUILDING_LIBRARY', observed)
    monkeypatch.setattr(module, 'MAX_FACTS', 512)
    monkeypatch.setattr(module, 'MAX_PROJECTION_BYTES', 100)
    with pytest.raises(CatalogError, match='projection byte bound'):
        project(catalog, 'BUILDING_LIBRARY', observed)
    assert (catalog, observed) == before


def test_no_extra_files_or_environment_enter_projection(assets, monkeypatch):
    catalog = extract(assets)
    observed = snapshot(catalog)
    baseline = project(catalog, 'BUILDING_LIBRARY', observed)
    (assets / 'enemy-private.json').write_text('{"secret":"hidden"}')
    monkeypatch.setenv('CIV_HIDDEN_STATE', 'not-supplied-to-overlay')
    assert project(catalog, 'BUILDING_LIBRARY', observed) == baseline
    assert 'not-supplied-to-overlay' not in canonical(baseline)
    assert extract(assets) == catalog


def test_cli_projection_and_duplicate_json_field_refusal(assets, tmp_path):
    catalog = extract(assets)
    catalog_path, observed_path, output = (tmp_path / name for name in
                                          ('catalog.json', 'observed.json', 'projection.json'))
    catalog_path.write_text(canonical(catalog))
    observed = snapshot(catalog, [fact('TECH_POTTERY')])
    observed_path.write_text(canonical(observed))
    command = [sys.executable, '-m', 'civ_arena.catalog', 'project', '--catalog', str(catalog_path),
               '--observations', str(observed_path), 'BUILDING_LIBRARY', '--output', str(output)]
    result = subprocess.run(command, capture_output=True, text=True, timeout=10, check=True)
    assert json.loads(result.stdout)['scope'] == 'offline_observed_dependency_projection'
    assert json.loads(output.read_text()) == project(catalog, 'BUILDING_LIBRARY', observed)
    before = output.read_bytes()
    observed_path.write_text(canonical(observed).replace('"turn":12', '"turn":12,"turn":13'))
    rejected = subprocess.run(command, capture_output=True, text=True, timeout=10)
    assert rejected.returncode == 2 and 'duplicate observation JSON field' in rejected.stderr
    assert output.read_bytes() == before
