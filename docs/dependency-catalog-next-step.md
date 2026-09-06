# S1 dependency catalog: installed-source survey

Read-only survey, 2026-09-05. Repository observed at `503478c70e23cfd57cdcaed9bf9054015c7d976e`. This note supports S1 in `docs/strategic-autopilot.md`; it is an implementation proposal, not an extractor or match-effective catalog. No tuner, provider, desktop, or game input was used. Active match ruleset, enabled content, and effective database were not queried.

The isolated [offline base-source extractor](base-source-catalog.md) now implements
the bounded first slice; effective-game and forecast gates remain separate.

## Concrete source inventory

Installed asset root (all paths below are relative to it):

`/media/alexk/RAID5_Storage/SteamLibrary/steamapps/common/Sid Meier's Civilization VI/steamassets`

| File under `base/assets/gameplay/data/` | Relevant tables and fields |
| --- | --- |
| `technologies.xml` | `Technologies` (68 base rows: stable `TechnologyType`, cost, era); `TechnologyPrereqs` (90 base edges: `Technology`, `PrereqTech`); boosts are separate. |
| `civics.xml` | `Civics` (51 base rows); `CivicPrereqs` (69 base edges); boosts and modifier requirements are separate. |
| `units.xml` | `Units` (97 base rows: `UnitType`, `PrereqTech`, `PrereqCivic`, costs, resources, traits); `UnitReplaces`, `UnitUpgrades`, `Unit_BuildingPrereqs`. |
| `buildings.xml` | `Buildings` (80 base rows: `BuildingType`, `PrereqTech`, `PrereqCivic`, `PrereqDistrict`, costs, traits, placement fields); `BuildingPrereqs`, `MutuallyExclusiveBuildings`, `BuildingReplaces`, modifiers and requirements. |
| `districts.xml` | `Districts` (21 base rows: `DistrictType`, prerequisite, placement/population flags, cost progression); replacement and adjacency tables. |
| `requirements.xml` | Shared `Requirements` and `RequirementArguments`; many additional requirement rows live in the files that use them. |
| `schema/01_gameplayschema.sql` | Authoritative declared columns, defaults, primary keys, and foreign keys for these base tables. Expansion schemas extend them. |

Counts are raw base-file rows, not active-game totals. XML files can repeat a table element (confirmed for building requirements); parse every occurrence, not only the first. Preserve fully qualified type strings such as `TECH_WRITING` as catalog keys. Adapter-facing stripped names are a separate mapping.

Existing repository entry points are consumers, not a catalog extractor: `lua_translator.py:274` reads currently available research; `:299` reads currently available unit/building production. `scripts/live_resolve_civics.py:61,95` accesses civics/policies for housekeeping. `scripts/probes/db_enums.lua` inspects frontend enum bindings. `graph/projection.py:163` projects event/claim evidence. `game/sim/state.py:32-58` is a small simulator rules subset and must not serve as the full Civ VI rules database.

## Ruleset and content selection

`base/assets/configuration/data/rulesets.xml` defines `RULESET_STANDARD`. Installed expansion manifests include conditional `InGameActions/UpdateDatabase` actions, game-core and ruleset criteria, leader criteria, action load orders, and file priorities. `dlc/expansion2/expansion2.modinfo:26-35` explicitly applies its schema first and removes data second; its subsequent content action includes XP1 files from the expansion2 directory. Therefore concatenating base + expansion1 + expansion2 XML is incorrect. `dlc/expansion1/expansion1.modinfo:15-29` also permits some content based on selected leaders/rulesets independently of its own game core. Installed `dlc/treerandomizer/treerandomizer.modinfo` additionally selects changes by game-mode configuration.

The manifest spells paths such as `Data/Expansion2_Schema.sql`, while installed Linux paths are lowercase (`dlc/expansion2/data/expansion2_schema.sql`). Resolve using an explicit case-normalized asset index; reject ambiguity. Do not assume installation means enablement, or blindly apply scenario/mod files. A later match-effective export must bind the selected ruleset, game core, enabled mod IDs/versions, modes and relevant selected-player content. Until those inputs and application order are proven, label output `scope=base_source_catalog`, not `effective_ruleset=RULESET_STANDARD`.

## Dependency semantics that must stay explicit

`TechnologyPrereqs` (schema line 2688), `CivicPrereqs` (639), and `BuildingPrereqs` (573) declare pairs but no connective column. Preserve the table relationship plus `group_semantics=unverified` rather than assigning a universal AND. Concrete counterexample to blind flattening: Armory has both Barracks and Stable prerequisite rows, while `buildings.xml:237-238` declares Barracks and Stable mutually exclusive. This is strong evidence those building alternatives cannot be interpreted as a simple conjunction, but this survey did not prove the engine's full replacement/alternative semantics.

Modifier `RequirementSets` are a different mechanism: the schema at 2313 explicitly stores `RequirementSetType`, and installed rows use `REQUIREMENTSET_TEST_ALL` and `REQUIREMENTSET_TEST_ANY`. Preserve those explicit operators, membership, and requirement arguments; do not transfer their semantics to tech/building prerequisite tables. `Unit_BuildingPrereqs` also has `NumSupported` (schema 2919); a bare unlock edge loses that constraint. Trait replacements, resources, population, placement, obsolescence and unique-instance constraints are separate conditions, not research edges.

## Minimal first vertical slice

1. Add an offline read-only extractor for the base schema plus the five entity files above. Emit typed nodes, source-level edges and costs, with unknown connective/feasibility semantics explicit. Include source path/SHA256, table, row key, XML operation and occurrence ordinal, schema default provenance, and a deterministic catalog digest. Reject duplicate contradictory node definitions and unresolved references within the declared slice. Do not attempt general mod SQL replay in this slice.
2. Exercise these installed chains: Pottery → Writing → Campus → Library (Library requires both Writing and Campus fields); Animal Husbandry → Archery → Archer; Code of Laws → Craftsmanship. Keep district placement and current-city feasibility unknown. Include Armory/Barracks/Stable as the mandatory unresolved-group fixture. File evidence confirms each chain; this does not show an agent can execute the chain.
3. Render one query such as “What dependencies are recorded for Library?” with provenance on every edge and a visible distinction between known prerequisite facts and unresolved city conditions. No turn estimate or success percentage yet.
4. Gate S1 on deterministic repeated extraction; source-change digest invalidation; correct multi-table occurrence parsing; XML Update/Delete rejection or explicit unsupported status; replacement identity preservation; no AND inference for Armory; zero hidden-state dependencies. Before S2 feasibility, separately verify effective `GameInfo` rows against the selected manifest and compare readiness against existing player-scoped availability facade calls during an authorized later diagnostic. Static dependencies never replace engine action validation.

Forecasting, calibrated confidence, strategic strength, and learning remain later milestones. Selection probabilities from current scouting are heuristic action-choice probabilities and provide no dependency-completion confidence.

## Captured source identities

| Relative source | SHA256 |
| --- | --- |
| `base/assets/gameplay/data/schema/01_gameplayschema.sql` | `922ee8f97410887d07990b3574eb9841a31a94f9aa40ce2715dec7accde13987` |
| `base/assets/gameplay/data/technologies.xml` | `641825487ae48cdab25cdd6e391684b1f393fe5b72cacd03478c123d0bcf98b2` |
| `base/assets/gameplay/data/civics.xml` | `7e346b0c719e157a0b034400774df83d9e5926711044800c2df6fbe0d8c1bbb7` |
| `base/assets/gameplay/data/units.xml` | `ef9d3984d41c5bbb8363ae2195e646eef1353f7b76b7e5cab95d47b3ab762305` |
| `base/assets/gameplay/data/buildings.xml` | `b11ecff3c0721b23f832107c0d5521af1a02c4e281efd4f07441338257fd13f4` |
| `base/assets/gameplay/data/districts.xml` | `3adaf90ef818b03c386fba3664658080e7c5c4d81199ca6843438dc651fac41d` |
| `base/assets/configuration/data/rulesets.xml` | `30e557c99a125aff78f8a679c37916cd222bf65410ab4cd49708d157f9f08a95` |
| `dlc/expansion1/expansion1.modinfo` | `940bae66a6fb24917d398eb6346be3c9c53386f91f9caff75670e878a3af24f7` |
| `dlc/expansion2/expansion2.modinfo` | `69e8c5cb48d8dbf0d8a322eb68ec3aa0c403ba6f30a002ea2aa60b692384ae7e` |
| `dlc/treerandomizer/treerandomizer.modinfo` | `78f70ffe8d1339b995c243040ebcb308c892a3e4c18c4070bdc2e8d6aeb7febb` |

Probe logs: `/tmp/civ-dependency-catalog-survey-probe.log`, `/tmp/civ-dependency-catalog-schema-probe.log`, `/tmp/civ-dependency-catalog-semantics.log`, `/tmp/civ-dependency-catalog-repo-probe.log`. These retain local source observations, not a live-engine proof.
