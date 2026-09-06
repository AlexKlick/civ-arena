"""Pure source-graph overlay of explicitly supplied observation facts."""
from __future__ import annotations

import copy
import re

from civ_arena.canonical import args_digest, canonical
from civ_arena.catalog.base import CatalogError, query

MAX_FACTS = 512
MAX_OBSERVATION_BYTES = 256 * 1024
MAX_PROJECTION_BYTES = 2 * 1024 * 1024
_TOKEN = re.compile(r'[A-Za-z0-9_.:-]{1,128}')
_PREREQUISITES = frozenset({'requires_technology', 'requires_civic',
                            'requires_building', 'requires_district'})


def _snapshot(value: dict, catalog: dict) -> dict:
    fields = {'observation_version', 'catalog_digest', 'snapshot_id', 'player_id', 'turn', 'facts'}
    if not isinstance(value, dict) or set(value) != fields:
        raise CatalogError('observation snapshot has unsupported fields')
    if type(value['observation_version']) is not int or value['observation_version'] != 1:
        raise CatalogError('unsupported observation version')
    if value['catalog_digest'] != catalog['catalog_digest']:
        raise CatalogError('observation catalog digest mismatch')
    if (type(value['player_id']) is not int or not 0 <= value['player_id'] <= 63
            or type(value['turn']) is not int or not 1 <= value['turn'] <= 1000000):
        raise CatalogError('invalid observation player or turn')
    if not isinstance(value['snapshot_id'], str) or not _TOKEN.fullmatch(value['snapshot_id']):
        raise CatalogError('invalid observation snapshot identity')
    facts = value['facts']
    if not isinstance(facts, list) or len(facts) > MAX_FACTS:
        raise CatalogError('observation fact bound exceeded')
    nodes = {node['id']: node for node in catalog['nodes']}
    seen = set()
    for fact in facts:
        if (not isinstance(fact, dict)
                or set(fact) != {'node_id', 'predicate', 'value', 'evidence_id'}):
            raise CatalogError('observation fact has unsupported fields')
        if not isinstance(fact['node_id'], str) or fact['node_id'] not in nodes:
            raise CatalogError('observation refers to unknown catalog node')
        if fact['predicate'] not in ('researched', 'unlocked') or type(fact['value']) is not bool:
            raise CatalogError('invalid observation predicate or value')
        if fact['predicate'] == 'researched' and nodes[fact['node_id']]['kind'] not in (
                'technology', 'civic'):
            raise CatalogError('researched observation requires technology or civic')
        if not isinstance(fact['evidence_id'], str) or not _TOKEN.fullmatch(fact['evidence_id']):
            raise CatalogError('invalid observation evidence identity')
        identity = (fact['node_id'], fact['predicate'])
        if identity in seen:
            raise CatalogError('duplicate observation fact')
        seen.add(identity)
    result = copy.deepcopy(value)
    result['facts'].sort(key=lambda fact: (fact['node_id'], fact['predicate']))
    if len(canonical(result).encode('utf-8')) > MAX_OBSERVATION_BYTES:
        raise CatalogError('observation byte bound exceeded')
    return result


def _observed(facts: dict, node: str, predicate: str) -> dict:
    fact = facts.get((node, predicate))
    if fact is None:
        return {'evidence_class': 'unknown', 'status': 'not_observed', 'predicate': predicate}
    return {'evidence_class': 'supplied_observation', 'status': 'observed', **fact}


def _condition(edge: dict, nodes: dict, facts: dict) -> dict:
    expected_kind = {'requires_technology': 'technology', 'requires_civic': 'civic'}
    relation, target = edge['relation'], edge['target']
    if relation in expected_kind and nodes[target]['kind'] == expected_kind[relation]:
        result = _observed(facts, target, 'researched')
        return {**result, 'meaning': 'Research completion fact only; source applicability '
                                    'and combined prerequisite feasibility remain unknown.'}
    return {'evidence_class': 'unknown', 'status': 'not_evaluated',
            'reason': 'Building/district existence, city scope and support limits are '
                      'not established by researched or unlocked observations.'}


def _requirement_sets(rows: list[dict]) -> list[dict]:
    operators = {'REQUIREMENTSET_TEST_ALL': 'all', 'REQUIREMENTSET_TEST_ANY': 'any'}
    result = []
    for row in rows:
        if row['table'] != 'RequirementSets':
            continue
        key = row['values']['RequirementSetId']
        memberships = [member for member in rows if member['table'] == 'RequirementSetRequirements'
                       and member['values']['RequirementSetId'] == key]
        result.append({'evidence_class': 'source_fact', 'set_id': key,
                       'operator': operators.get(row['values']['RequirementSetType'], 'unknown'),
                       'declaration': row, 'memberships': memberships,
                       'evaluation': 'unknown',
                       'reason': 'Source operator applies only to this modifier requirement set; '
                                 'member predicates and effective rules were not evaluated.'})
    return result


def project(catalog: dict, node_id: str, observations: dict, depth: int = 3) -> dict:
    """Annotate source options; never infer legal actions, availability, or a forecast."""
    view = query(catalog, node_id, depth)
    snapshot = _snapshot(observations, catalog)
    facts = {(fact['node_id'], fact['predicate']): fact for fact in snapshot['facts']}
    nodes = {node['id']: node for node in view['nodes']}
    groups: dict[tuple[str, str, str], dict] = {}
    edges, other = [], []
    for edge in view['edges']:
        marked = {**edge, 'evidence_class': 'source_fact'}
        if edge['relation'] not in _PREREQUISITES:
            other.append(marked)
            continue
        marked['target_observation'] = _condition(edge, nodes, facts)
        edges.append(marked)
        key = (edge['source'], edge['relation'], edge['provenance']['table'])
        group = groups.setdefault(key, {'source': key[0], 'relation': key[1],
            'source_table': key[2], 'edge_ids': [], 'connective': 'unverified',
            'why_unverified': 'These catalog pair/field declarations do not establish an '
                              'effective engine AND/OR connective. No prerequisite-set '
                              'operator is transferred onto these edges.',
            'evaluation': 'unknown'})
        group['edge_ids'].append(edge['id'])
    result = {**view, 'projection_version': 1, 'scope': 'offline_observed_dependency_projection',
              'source_scope': view['scope'], 'effective_ruleset': 'unverified',
              'observation_digest': args_digest(snapshot),
              'observation_snapshot': {key: value for key, value in snapshot.items()
                                       if key != 'facts'},
              'observation_authority': 'Caller-supplied snapshot; player scope, evidence '
                                       'authenticity and current freshness are not verified.',
              'nodes': [{**node, 'evidence_class': 'source_fact', 'observations': {
                    predicate: _observed(facts, node['id'], predicate)
                    for predicate in ('researched', 'unlocked')
                    if predicate == 'unlocked' or node['kind'] in ('technology', 'civic')}}
                        for node in view['nodes']],
              'edges': edges, 'other_relations': other,
              'prerequisite_options': [groups[key] for key in sorted(groups)],
              'source_requirement_sets': _requirement_sets(view['supporting_rows']),
              'projection_limits': [
                  'Missing observations mean unknown, never false.',
                  'Observed unlocked does not mean built, completed, affordable or legal now.',
                  'No resources, placement, rates, completion costs or turn forecasts evaluated.',
                  'Unknown connective, omitted conditions and effective rules prevent readiness.',
                  'Unexpanded edges retain the frontier; observations never propagate through it.',
                  'This artifact cannot authorize game actions.']}
    result['projection_digest'] = args_digest(result)
    if len(canonical(result).encode('utf-8')) > MAX_PROJECTION_BYTES:
        raise CatalogError('projection byte bound exceeded')
    return result
