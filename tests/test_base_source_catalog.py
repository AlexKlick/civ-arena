"""Offline fixtures cover provenance and uncertainty without game/provider state."""
from __future__ import annotations

import copy
import json
import subprocess
import sys

import pytest

from civ_arena.canonical import args_digest, canonical
from civ_arena.catalog.base import PREFIX, SCHEMA_FILE, XML_FILES, CatalogError, extract, query

SCHEMA = """
CREATE TABLE Technologies (TechnologyType TEXT PRIMARY KEY, Cost INTEGER NOT NULL);
CREATE TABLE Civics (CivicType TEXT PRIMARY KEY, Cost INTEGER DEFAULT 0);
CREATE TABLE Units (UnitType TEXT PRIMARY KEY, PrereqTech TEXT, Cost INTEGER DEFAULT 10);
CREATE TABLE Buildings (BuildingType TEXT PRIMARY KEY, PrereqTech TEXT, PrereqDistrict TEXT,
 Cost INTEGER DEFAULT 50, Coast BOOLEAN DEFAULT 0, RequiresAdjacentRiver BOOLEAN DEFAULT 0);
CREATE TABLE Districts (DistrictType TEXT PRIMARY KEY, PrereqTech TEXT,
 Cost INTEGER DEFAULT 0, CostProgression REAL DEFAULT 1.5);
CREATE TABLE TechnologyPrereqs (Technology TEXT, PrereqTech TEXT,
 PRIMARY KEY(Technology, PrereqTech),
 FOREIGN KEY(Technology) REFERENCES Technologies(TechnologyType),
 FOREIGN KEY(PrereqTech) REFERENCES Technologies(TechnologyType));
CREATE TABLE CivicPrereqs (Civic TEXT, PrereqCivic TEXT, PRIMARY KEY(Civic, PrereqCivic));
CREATE TABLE BuildingPrereqs (Building TEXT, PrereqBuilding TEXT,
 PRIMARY KEY(Building, PrereqBuilding));
CREATE TABLE MutuallyExclusiveBuildings (Building TEXT, MutuallyExclusiveBuilding TEXT,
 PRIMARY KEY(Building, MutuallyExclusiveBuilding));
CREATE TABLE BuildingReplaces (CivUniqueBuildingType TEXT PRIMARY KEY, ReplacesBuildingType TEXT);
CREATE TABLE Unit_BuildingPrereqs (Unit TEXT, PrereqBuilding TEXT, NumSupported INTEGER DEFAULT -1,
 PRIMARY KEY(Unit, PrereqBuilding));
CREATE TABLE Requirements (RequirementId TEXT PRIMARY KEY, RequirementType TEXT NOT NULL);
CREATE TABLE RequirementSets (RequirementSetId TEXT PRIMARY KEY, RequirementSetType TEXT NOT NULL);
CREATE TABLE RequirementSetRequirements (RequirementSetId TEXT, RequirementId TEXT,
 PRIMARY KEY(RequirementSetId, RequirementId),
 FOREIGN KEY(RequirementSetId) REFERENCES RequirementSets(RequirementSetId),
 FOREIGN KEY(RequirementId) REFERENCES Requirements(RequirementId));
CREATE TABLE Modifiers (ModifierId TEXT PRIMARY KEY, SubjectRequirementSetId TEXT,
 FOREIGN KEY(SubjectRequirementSetId) REFERENCES RequirementSets(RequirementSetId));
CREATE TABLE BuildingModifiers (BuildingType TEXT, ModifierId TEXT,
 PRIMARY KEY(BuildingType, ModifierId),
 FOREIGN KEY(BuildingType) REFERENCES Buildings(BuildingType),
 FOREIGN KEY(ModifierId) REFERENCES Modifiers(ModifierId));
"""
XML = {
 'technologies': """<Technologies><Row TechnologyType="TECH_POTTERY" Cost="25"/>
 <Row TechnologyType="TECH_WRITING" Cost="50"/>
 <Row TechnologyType="TECH_ANIMAL_HUSBANDRY" Cost="25"/>
 <Row TechnologyType="TECH_ARCHERY" Cost="50"/></Technologies>
 <TechnologyPrereqs><Row Technology="TECH_WRITING" PrereqTech="TECH_POTTERY"/>
 <Row Technology="TECH_ARCHERY" PrereqTech="TECH_ANIMAL_HUSBANDRY"/></TechnologyPrereqs>""",
 'civics': """<Civics><Row CivicType="CIVIC_CODE_OF_LAWS"/>
 <Row CivicType="CIVIC_CRAFTSMANSHIP"/></Civics>
 <CivicPrereqs><Row Civic="CIVIC_CRAFTSMANSHIP"
 PrereqCivic="CIVIC_CODE_OF_LAWS"/></CivicPrereqs>""",
 'units': """<Units><Row UnitType="UNIT_ARCHER" PrereqTech="TECH_ARCHERY"/></Units>
 <Unit_BuildingPrereqs><Row Unit="UNIT_ARCHER" PrereqBuilding="BUILDING_BARRACKS"/>
 </Unit_BuildingPrereqs>""",
 'districts': """<Districts><Row DistrictType="DISTRICT_CAMPUS" PrereqTech="TECH_WRITING"/>
 </Districts>""",
 'buildings': """<Buildings><Row BuildingType="BUILDING_LIBRARY" PrereqTech="TECH_WRITING"
 PrereqDistrict="DISTRICT_CAMPUS" RequiresAdjacentRiver="False"/>
 <Row BuildingType="BUILDING_ARMORY"/><Row BuildingType="BUILDING_BARRACKS"/>
 <Row BuildingType="BUILDING_STABLE"/><Row BuildingType="BUILDING_UNIQUE_LIBRARY"/>
 </Buildings><BuildingPrereqs><Row Building="BUILDING_ARMORY" PrereqBuilding="BUILDING_BARRACKS"/>
 <Row Building="BUILDING_ARMORY" PrereqBuilding="BUILDING_STABLE"/></BuildingPrereqs>
 <MutuallyExclusiveBuildings><Row Building="BUILDING_BARRACKS"
 MutuallyExclusiveBuilding="BUILDING_STABLE"/></MutuallyExclusiveBuildings>
 <BuildingReplaces><Row CivUniqueBuildingType="BUILDING_UNIQUE_LIBRARY"
 ReplacesBuildingType="BUILDING_LIBRARY"/></BuildingReplaces>
 <Requirements><Row RequirementId="REQ_ONE" RequirementType="CHECK_CITY"/></Requirements>
 <Requirements><Row><RequirementId>REQ_TWO</RequirementId>
 <RequirementType>CHECK_TERRAIN</RequirementType></Row></Requirements>
 <RequirementSets><Row RequirementSetId="SET_ALL" RequirementSetType="REQUIREMENTSET_TEST_ALL"/>
 <Row RequirementSetId="SET_ANY" RequirementSetType="REQUIREMENTSET_TEST_ANY"/></RequirementSets>
 <RequirementSetRequirements><Row RequirementSetId="SET_ALL" RequirementId="REQ_ONE"/>
 <Row RequirementSetId="SET_ALL" RequirementId="REQ_EXTERNAL"/></RequirementSetRequirements>
 <Modifiers><Row ModifierId="MOD_LIBRARY" SubjectRequirementSetId="SET_ALL"/></Modifiers>
 <BuildingModifiers><Row BuildingType="BUILDING_LIBRARY" ModifierId="MOD_LIBRARY"/>
 </BuildingModifiers>""",
}


@pytest.fixture
def assets(tmp_path):
    root = tmp_path / 'assets'
    schema = root / SCHEMA_FILE
    schema.parent.mkdir(parents=True)
    schema.write_text(SCHEMA)
    for name, rows in XML.items():
        (root / PREFIX / (name + '.xml')).write_text('<GameInfo>' + rows + '</GameInfo>')
    return root


def test_repeatable_hash_source_change_and_declared_schema_change_invalidate(assets):
    first = extract(assets)
    assert canonical(first) == canonical(extract(assets))
    path = assets / XML_FILES[0]
    path.write_text(path.read_text().replace('Cost="25"', 'Cost="26"', 1))
    second = extract(assets)
    assert first['catalog_digest'] != second['catalog_digest']
    schema = assets / SCHEMA_FILE
    schema.write_text(schema.read_text() + '\n-- source change only\n')
    assert extract(assets)['catalog_digest'] != second['catalog_digest']


def test_library_query_has_all_dependency_provenance_and_exact_depth(assets):
    catalog = extract(assets)
    full = query(catalog, 'BUILDING_LIBRARY', depth=3)
    assert {node['id'] for node in full['nodes']} == {
        'BUILDING_LIBRARY', 'TECH_WRITING', 'TECH_POTTERY', 'DISTRICT_CAMPUS'}
    assert len(full['edges']) == 4
    assert 'TECH_POTTERY' not in {node['id'] for node in query(
        catalog, 'BUILDING_LIBRARY', depth=1)['nodes']}
    for edge in full['edges']:
        source = edge['provenance']
        assert source['path'] in XML_FILES and len(source['sha256']) == 64
        assert source['operation'] == 'Row' and source['row_key']
        assert source['row'] >= 1 and source['table_occurrence'] >= 1
        assert edge['group_semantics'] == 'unverified'
    assert full['current_feasibility'] == 'unknown'
    assert full['scope'] == 'base_source_catalog'


def test_armory_keeps_both_alternatives_and_exclusion_without_inferred_and(assets):
    view = query(extract(assets), 'BUILDING_ARMORY')
    required = [edge for edge in view['edges'] if edge['relation'] == 'requires_building']
    assert {edge['target'] for edge in required} == {'BUILDING_STABLE', 'BUILDING_BARRACKS'}
    assert all(edge['group_semantics'] == 'unverified' for edge in required)
    assert any(edge['relation'] == 'mutually_exclusive' for edge in view['edges'])


def test_replacement_identity_defaults_constraints_and_decimal_values_preserved(assets):
    catalog = extract(assets)
    replacement = query(catalog, 'BUILDING_UNIQUE_LIBRARY')
    assert any(edge['relation'] == 'replaces' and edge['target'] == 'BUILDING_LIBRARY'
               for edge in replacement['edges'])
    archer = query(catalog, 'UNIT_ARCHER')
    edge = next(edge for edge in archer['edges'] if edge['relation'] == 'requires_building')
    assert edge['constraints'] == {'NumSupported': -1}
    default = archer['schema']['Unit_BuildingPrereqs']['columns']['NumSupported']
    assert default['default_sql'] == '-1' and default['default_value'] == -1
    campus = next(node for node in catalog['nodes'] if node['id'] == 'DISTRICT_CAMPUS')
    assert campus['attributes']['CostProgression'] == {'decimal': '1.5'}
    assert campus['attributes']['Cost'] == 0
    assert 'Cost' not in campus['declarations'][0]['explicit_columns']


def test_repeated_tables_child_rows_explicit_requirement_operators_and_missing_references(assets):
    catalog = extract(assets)
    requirements = [row for row in catalog['supporting_rows'] if row['table'] == 'Requirements']
    assert {row['provenance']['table_occurrence'] for row in requirements} == {1, 2}
    assert {row['values']['RequirementId'] for row in requirements} == {'REQ_ONE', 'REQ_TWO'}
    sets = [row for row in catalog['supporting_rows'] if row['table'] == 'RequirementSets']
    assert {row['values']['RequirementSetType'] for row in sets} == {
        'REQUIREMENTSET_TEST_ALL', 'REQUIREMENTSET_TEST_ANY'}
    library = query(catalog, 'BUILDING_LIBRARY')
    assert any(row['target_value'] == 'REQ_EXTERNAL'
               for row in library['unresolved_source_references'])
    assert any(row['table'] == 'RequirementSets' for row in library['supporting_rows'])


@pytest.mark.parametrize('operation', ['Update', 'Delete', 'Replace'])
def test_unsupported_operations_reject_even_in_unmodeled_tables(assets, operation):
    path = assets / XML_FILES[0]
    path.write_text(path.read_text().replace('</GameInfo>',
        f'<Unmodeled><{operation}/></Unmodeled></GameInfo>'))
    with pytest.raises(CatalogError, match='unsupported XML operation'):
        extract(assets)


@pytest.mark.parametrize('changed', [False, True])
def test_duplicate_node_definitions_preserve_sources_or_reject_conflict(assets, changed):
    path = assets / XML_FILES[0]
    cost = 99 if changed else 25
    path.write_text(path.read_text().replace('</GameInfo>',
        f'<Technologies><Row TechnologyType="TECH_POTTERY" Cost="{cost}"/>'
        '</Technologies></GameInfo>'))
    if changed:
        with pytest.raises(CatalogError, match='contradictory duplicate'):
            extract(assets)
    else:
        node = next(node for node in extract(assets)['nodes'] if node['id'] == 'TECH_POTTERY')
        assert len(node['declarations']) == 2


def test_unresolved_core_dependency_fails_instead_of_inventing_node(assets):
    path = assets / XML_FILES[0]
    path.write_text(path.read_text().replace('PrereqTech="TECH_POTTERY"',
                                            'PrereqTech="TECH_UNKNOWN"'))
    with pytest.raises(CatalogError, match='unresolved entity reference'):
        extract(assets)


def test_case_normalization_ambiguity_and_symlink_escape(assets, tmp_path):
    path = assets / XML_FILES[0]
    mixed = path.with_name('Technologies.XML')
    path.rename(mixed)
    extract(assets)
    path.write_bytes(mixed.read_bytes())
    with pytest.raises(CatalogError, match='ambiguous source'):
        extract(assets)
    path.unlink()
    mixed.unlink()
    outside = tmp_path / 'outside.xml'
    outside.write_text('<GameInfo/>')
    path.symlink_to(outside)
    with pytest.raises(CatalogError, match='escapes asset root'):
        extract(assets)


def test_entities_and_unsupported_field_shapes_reject(assets):
    path = assets / XML_FILES[0]
    path.write_text('<!DOCTYPE GameInfo [<!ENTITY secret "x">]><GameInfo/>')
    with pytest.raises(CatalogError, match='declarations/entities'):
        extract(assets)


def test_unknown_column_and_ambiguous_child_attribute_reject(assets):
    path = assets / XML_FILES[0]
    text = path.read_text()
    path.write_text(text.replace('Cost="25"', 'Cost="25" Secret="hidden"', 1))
    with pytest.raises(CatalogError, match='unknown declared column'):
        extract(assets)
    path.write_text(text.replace('<Row TechnologyType="TECH_POTTERY" Cost="25"/>',
        '<Row TechnologyType="TECH_POTTERY" Cost="25"><Cost>26</Cost></Row>'))
    with pytest.raises(CatalogError, match='ambiguous XML field'):
        extract(assets)


def test_extra_unselected_files_and_environment_cannot_supply_hidden_state(assets, monkeypatch):
    baseline = extract(assets)
    (assets / 'players-secret.json').write_text('{"unseen_enemy":true}')
    (assets / 'expansion.xml').write_text('<GameInfo><Units><Delete/></Units></GameInfo>')
    monkeypatch.setenv('CIV_PLAYER_ID', '1')
    monkeypatch.setenv('CIV_HIDDEN_STATE', 'enemy_secret')
    assert extract(assets) == baseline
    assert len(baseline['source_files']) == 6
    assert 'enemy_secret' not in canonical(baseline)


def test_query_rejects_tampered_catalog_and_invalid_depth(assets):
    catalog = extract(assets)
    altered = copy.deepcopy(catalog)
    altered['nodes'][0]['attributes']['Cost'] = 999
    with pytest.raises(CatalogError, match='digest'):
        query(altered, 'BUILDING_LIBRARY')
    for depth in (-1, 9, True):
        with pytest.raises(CatalogError, match='depth'):
            query(catalog, 'BUILDING_LIBRARY', depth)
    assert catalog['catalog_digest'] == args_digest({key: value for key, value in catalog.items()
                                                   if key != 'catalog_digest'})


def test_offline_cli_extract_and_query_json_artifacts(assets, tmp_path):
    artifact, answer = tmp_path / 'catalog.json', tmp_path / 'library.json'
    extracted = subprocess.run([sys.executable, '-m', 'civ_arena.catalog', 'extract',
        '--asset-root', str(assets), '--output', str(artifact)], capture_output=True,
        text=True, timeout=10, check=True)
    assert json.loads(extracted.stdout)['scope'] == 'base_source_catalog'
    subprocess.run([sys.executable, '-m', 'civ_arena.catalog', 'query',
        '--catalog', str(artifact), 'BUILDING_LIBRARY', '--output', str(answer)],
        capture_output=True, text=True, timeout=10, check=True)
    assert json.loads(answer.read_text())['root'] == 'BUILDING_LIBRARY'


def test_query_frontier_is_explicit_and_answer_cannot_mutate_catalog(assets):
    catalog = extract(assets)
    answer = query(catalog, 'BUILDING_LIBRARY', depth=1)
    assert not answer['query_complete']
    assert any(edge['target'] == 'TECH_POTTERY' for edge in answer['unexpanded_edges'])
    assert all(edge['provenance'] for edge in answer['unexpanded_edges'])
    digest = args_digest(catalog)
    answer['nodes'][0]['attributes']['Cost'] = 999
    answer['schema']['Buildings']['columns']['Cost']['default_value'] = 999
    assert args_digest(catalog) == digest


def test_bundle_file_bounds_and_missing_definitions_fail(assets, monkeypatch):
    import civ_arena.catalog.base as base
    monkeypatch.setattr(base, 'MAX_BYTES', 10)
    with pytest.raises(CatalogError, match='source size'):
        extract(assets)
    monkeypatch.setattr(base, 'MAX_BYTES', 4 * 1024 * 1024)
    for relative in XML_FILES:
        (assets / relative).write_text('<GameInfo/>')
    with pytest.raises(CatalogError, match='missing entity table'):
        extract(assets)


@pytest.mark.parametrize('encoding', ['utf-16', 'utf-16-le', 'utf-16-be',
                                     'utf-32', 'utf-32-le', 'utf-32-be'])
def test_non_utf8_xml_entities_reject_before_parser(assets, encoding):
    path = assets / XML_FILES[0]
    body = path.read_text().replace('Cost="25"', 'Cost="&cost;"', 1)
    document = ('<?xml version="1.0" encoding="UTF-16"?>'
                '<!DOCTYPE GameInfo [<!ENTITY cost "25">]>' + body)
    path.write_bytes(document.encode(encoding))
    with pytest.raises(CatalogError, match='UTF-8 encoding'):
        extract(assets)


@pytest.mark.parametrize('declaration,bom', [('UTF-8', False), ('utf-8', True)])
def test_declared_utf8_and_bom_remain_supported(assets, declaration, bom):
    path = assets / XML_FILES[0]
    body = path.read_text()
    path.write_bytes((f'<?xml version="1.0" encoding="{declaration}"?>' + body).encode(
        'utf-8-sig' if bom else 'utf-8'))
    extract(assets)
    path.write_bytes((f'<?xml version="1.0" encoding="{declaration}"?>'
                     '<!DOCTYPE GameInfo [<!ENTITY cost "25">]>' + body).encode(
        'utf-8-sig' if bom else 'utf-8'))
    with pytest.raises(CatalogError, match='declarations/entities'):
        extract(assets)


def test_other_xml_encoding_declaration_rejected_even_when_bytes_are_ascii(assets):
    path = assets / XML_FILES[0]
    path.write_text('<?xml version="1.0" encoding="ISO-8859-1"?>' + path.read_text())
    with pytest.raises(CatalogError, match='UTF-8 encoding'):
        extract(assets)


def test_support_query_joins_complete_foreign_keys_without_partial_primary_key_bleed(assets):
    schema = assets / SCHEMA_FILE
    schema.write_text(schema.read_text() + '''
CREATE TABLE RequirementArguments (RequirementId TEXT, Name TEXT, Value TEXT,
 PRIMARY KEY (RequirementId, Name),
 FOREIGN KEY(RequirementId) REFERENCES Requirements(RequirementId),
 FOREIGN KEY(Name, Value) REFERENCES ModifierArguments(Name, Value));
CREATE TABLE ModifierArguments (Name TEXT, Value TEXT, PRIMARY KEY(Name, Value));
''')
    path = assets / PREFIX / 'buildings.xml'
    path.write_text(path.read_text().replace('</GameInfo>', '''<RequirementArguments>
<Row RequirementId="REQ_ONE" Name="Shared" Value="wanted"/>
<Row RequirementId="REQ_TWO" Name="Shared" Value="unrelated"/>
<Row RequirementId="REQ_ONE" Name="Other" Value="missing"/>
</RequirementArguments><ModifierArguments>
<Row Name="Shared" Value="wanted"/><Row Name="Shared" Value="other"/>
<Row Name="Other" Value="wanted"/>
</ModifierArguments></GameInfo>'''))
    catalog = extract(assets)
    view = query(catalog, 'BUILDING_LIBRARY')
    arguments = [row['values'] for row in view['supporting_rows']
                 if row['table'] == 'RequirementArguments']
    assert len(arguments) == 2
    assert {row['RequirementId'] for row in arguments} == {'REQ_ONE'}
    requirements = [row['values']['RequirementId'] for row in view['supporting_rows']
                    if row['table'] == 'Requirements']
    assert requirements == ['REQ_ONE']
    assert [row['values'] for row in view['supporting_rows']
            if row['table'] == 'ModifierArguments'] == [{'Name': 'Shared', 'Value': 'wanted'}]
    missing = [row for row in view['unresolved_source_references']
               if row['target_table'] == 'ModifierArguments']
    assert len(missing) == 1
    assert missing[0]['target_columns'] == ['Name', 'Value']
    assert missing[0]['target_values'] == ['Other', 'missing']
    # Both individual components exist in the source, but their tuple does not.
    assert query(catalog, 'BUILDING_LIBRARY') == view


def test_missing_shared_foreign_target_is_not_a_row_join(assets):
    path = assets / PREFIX / 'buildings.xml'
    path.write_text(path.read_text().replace('SubjectRequirementSetId="SET_ALL"',
        'SubjectRequirementSetId="SET_MISSING"').replace('</GameInfo>',
        '<Modifiers><Row ModifierId="MOD_UNRELATED" '
        'SubjectRequirementSetId="SET_MISSING"/></Modifiers></GameInfo>'))
    view = query(extract(assets), 'BUILDING_LIBRARY')
    assert [row['values']['ModifierId'] for row in view['supporting_rows']
            if row['table'] == 'Modifiers'] == ['MOD_LIBRARY']
    assert any(row.get('target_value') == 'SET_MISSING'
               for row in view['unresolved_source_references'])


def test_prior_catalog_version_rejected_explicitly(assets):
    catalog = extract(assets)
    catalog['catalog_version'] = 1
    catalog['catalog_digest'] = args_digest({key: value for key, value in catalog.items()
                                            if key != 'catalog_digest'})
    with pytest.raises(CatalogError, match='version mismatch'):
        query(catalog, 'BUILDING_LIBRARY')
