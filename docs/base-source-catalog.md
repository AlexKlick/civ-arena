# Offline base-source dependency catalog

This S1 implementation reads five installed base XML files and the declared base
schema. It emits source facts for technologies, civics, units, buildings and
districts. It does not read Civ VI state, choose an active ruleset, enumerate
installed DLC, access a provider, change a live controller or infer an executable
strategy. The [source survey](dependency-catalog-next-step.md) remains the prior
inventory and proof boundary.

## Contract and query

```bash
PYTHONPATH=src /home/alexk/documents/civ-arena/.venv/bin/python -m civ_arena.catalog extract \
  --asset-root "/media/alexk/RAID5_Storage/SteamLibrary/steamapps/common/Sid Meier's Civilization VI/steamassets" \
  --output /tmp/base-source-catalog.json
PYTHONPATH=src /home/alexk/documents/civ-arena/.venv/bin/python -m civ_arena.catalog query \
  --catalog /tmp/base-source-catalog.json BUILDING_LIBRARY --depth 3 \
  --output /tmp/library-source-graph.json
```

The artifact uses `catalog_version=2` and is labeled `scope=base_source_catalog`,
`effective_ruleset=unverified`, and `current_feasibility=unknown`. Stable node IDs
retain the full source vocabulary such as `TECH_WRITING`, with a separate typed
kind. Graph edges point **from a subject to its source-declared relation target**;
this is not an execution order. Relations include technology/civic/building/
district prerequisites, replacements, unit upgrades and mutual exclusions.
Replacement nodes retain distinct identities. Every edge has a deterministic ID,
source-file SHA-256, relative path, table, declared row key, Row operation,
per-file table occurrence and row ordinal. Multiple occurrences of a table and
both attribute-style and child-element Row fields are parsed.

Node attributes use declared schema types. Integers and booleans remain typed;
REAL values use an exact `{ "decimal": "1.5" }` representation rather than a
binary float. Each node records its schema table, declarations and explicit
columns. Omitted fields use the captured declared default; the schema table
retains each column's type, required flag, primary-key position, raw default SQL
and typed default, plus schema-file and CREATE-statement digests. Thus a default
such as `Unit_BuildingPrereqs.NumSupported=-1` has separate schema provenance
rather than appearing to have been written in the XML row.

Only selected `CREATE TABLE` declarations are inspected in an in-memory SQLite
database. The schema script is never executed as a whole. NavigationProperties
inserts, mod SQL, load order, manifest conditions and effective database updates
are not evaluated. This is schema declaration introspection, not a database
rebuild. The artifact records that restricted schema scope.

Prerequisite edges retain `group_semantics=unverified`. Armory's Barracks and
Stable rows remain alongside their mutual exclusion. This avoids inventing a
universal AND or assuming that observed exclusion proves all engine alternative
rules. Explicit `RequirementSetType` ALL/ANY tags are preserved in supporting
rows and never transferred to unrelated prerequisite tables. Modifier and
requirement references remain source records, not evaluated conditions.

Supporting-row traversal follows declared foreign keys to actual source rows in
both directions. Every composite reference is matched as one complete typed
column tuple. A shared argument name, a partial primary key or references to an
absent common target cannot connect unrelated rows. Schema foreign-key records
retain their constraint `id` and within-constraint `sequence`. Unresolved
composite references expose ordered `columns`, `target_columns` and
`target_values`; single-column references retain the singular fields. Version 1
catalogs are explicitly rejected and must be regenerated. Implicit foreign-key
target columns are unsupported and reject rather than being guessed.

The query returns a bounded outgoing-relation neighborhood, edge provenance,
relevant supporting rows, schema/default provenance and unresolved source
references. Rows from `Types`, resources, traits, eras or other unmodeled tables
can produce unresolved references even when the source table exists elsewhere
in the installation. `not_defined_in_selected_source_rows` describes the
extractor's coverage; it is not a claim that the game database is broken. Missing
core entity references in the modeled prerequisite/replacement graph reject
extraction. City feasibility, district placement, completion state, enabled
content and forecasts remain explicitly unknown.

At the requested depth boundary, `unexpanded_edges` retain known relationships
and provenance that were not traversed; `query_complete` reports whether this
neighborhood has such a frontier. It never means full effective-game coverage.
Query results are independent copies and cannot mutate the input catalog.
The JSON is suitable for a future static graph viewer; no browser integration is
included here.

## Bounds and failure behavior

Only six fixed relative input paths are resolved. Case-insensitive component
resolution rejects ambiguity and paths whose symlinks escape the supplied asset
root. Each file is limited to 4 MiB, the bundle to 20,000 XML rows, and values to
4,096 characters. Only UTF-8 XML, with an optional UTF-8 BOM, is accepted.
Other encodings or encoding declarations reject before XML parsing. Entity and
DOCTYPE declarations are checked after decoding and reject without expansion. Contradictory duplicate
row keys, missing required values, unsupported scalar types, duplicate fields,
unknown declared columns and missing entity tables reject. Identical duplicate
node declarations preserve all source occurrences.

XML `Update`, `Delete`, `Replace` and other non-Row operations reject even in a
table this slice does not model. Unmodeled Row-only tables are explicitly
inventoried with occurrence/count/source provenance rather than silently merged
into the graph. No expansion source is automatically applied. Query depth is
0–8, with at most 512 selected nodes and 2,000 supporting rows; catalog input
artifacts have a 64 MiB CLI limit. A query verifies the canonical catalog digest
before selection. The digest detects changed source/schema bytes and artifact
modification; it does not authenticate a wholly rewritten source bundle.

## Verified findings

The corrected isolated fixture gate passed **31 tests, zero failed/skipped**, 0.46 s;
focused Ruff passed. Tests cover deterministic extraction and source/schema
change invalidation, complete edge/default provenance, repeated table occurrences,
XML child fields, unsupported updates/deletes/replacements, contradictory and
identical duplicates, Armory's unresolved alternatives, replacement identity,
explicit ALL/ANY records, unresolved supporting references, case ambiguity,
symlink confinement, XML entity rejection, input bounds, query frontier/copy
custody, tamper rejection, and offline CLI artifacts. Extra files and environment
values cannot inject player or hidden-state data into the fixed input bundle.

Current installed-source extraction was repeated with identical canonical bytes:

| Entity table | Raw base nodes |
|---|---:|
| Technologies | 68 |
| Civics | 51 |
| Units | 97 |
| Buildings | 80 |
| Districts | 21 |

The artifact has **317 nodes, 526 source edges, 529 supporting rows and 841
unresolved selected-source references**. These are extractor coverage counts,
not active-game totals or errors. Catalog digest:
`374e0d0ac07db839deb4f86ddf8da955da874757d97609a806a354107fa982ff`.

The installed Library query contains exactly Library, Campus, Writing and
Pottery, with four source edges. Separate checks verified Archer → Archery →
Animal Husbandry, Craftsmanship → Code of Laws, and Armory's two prerequisite
rows plus mutual exclusion. Full source hashes, repeated artifacts, all four
query artifacts, commands and logs are retained under
`runs/base-catalog-corrections-final-20260906/`, including
`installed-summary.json` and `checks.json`. Prior evidence is preserved under
`runs/base-catalog-evidence-20260906/`. Early development logs retain corrected source-path quoting,
case-insensitive boolean parsing and lint findings; those are not final failures.

Independent review of `867fb5f` identified two confirmed defects: decomposed
composite keys included unrelated supporting rows, and raw-byte declaration
scanning allowed UTF-16 entity expansion. This correction adds regressions for
both, full composite foreign-key joins and unresolved tuples, absent shared
targets, six rejected UTF-16/32 variants, accepted UTF-8/BOM with declaration
rejection, and explicit old-version refusal. The first correction lint check
found one long SQL fixture line; its corrected check passed. Original independent
review evidence remains in `/tmp/civ-catalog-independent-probes-867fb5f.log`.

The separate [observed-fact overlay](observed-dependency-projection.md) now
projects supplied research/unlock facts onto this source graph without changing
the catalog or evaluating effective-game feasibility.

## Follow-up probes

Independent read-only review must bind this exact commit. The next integration
step is a static graph viewer of the artifact; current-player feasibility needs
separately authorized effective GameInfo/manifest proof and actual player-scoped
availability observations. Source graph reachability must never authorize actions.
Full repository release testing remains separate from the isolated fixture gate.

## Blocked checks

No environment dependency blocked offline extraction. Live engine, provider,
tuner, desktop and active-match integration were excluded; no such calls were
made and no running-main files were edited.

## Evidence gaps

This artifact is not an effective ruleset, a complete prerequisite evaluator,
a calibrated forecast, an optimized opening or an action plan. It contains no
current city/resource/technology ownership, economic rates, hidden map state,
confidence percentage, native engine legality proof or completed live match.
