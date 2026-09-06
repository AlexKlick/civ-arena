# Offline observed dependency projection

The projection overlays explicitly supplied facts onto the reviewed base-source
catalog. It answers “Which source prerequisite rows connect to this goal, and
which target observations were supplied?” Every annotation is labeled as a
`source_fact`, `supplied_observation`, or `unknown`. It does not calculate
readiness, turn estimates, confidence percentages, or legal game actions.

The extractor and catalog version 2 remain unchanged. This separate output uses
`projection_version=1` and `scope=offline_observed_dependency_projection`.
`effective_ruleset=unverified` and `current_feasibility=unknown` remain present,
even when every listed research target has an observed completion fact.

## Snapshot contract

Call `civ_arena.catalog.projection.project(catalog, node_id, observations, depth)`
or use the offline CLI below. The exact input fields are:

| Field | Meaning |
|---|---|
| `observation_version` | Integer 1. |
| `catalog_digest` | Exact digest of the supplied version-2 catalog. |
| `snapshot_id` | Caller-supplied opaque token, 1–128 ASCII letters/digits or `_.:-`. |
| `player_id`, `turn` | Caller-asserted scope; integers 0–63 and 1–1,000,000. |
| `facts` | At most 512 records, each with `node_id`, `predicate`, boolean `value`, and opaque `evidence_id`. |

Supported predicates are `researched` for technologies/civics and `unlocked`
for catalog entities. The latter records only the caller's unlock observation;
it does not establish construction, ownership, research completion, affordability,
or current legal availability. `researched=false` is an explicit supplied
negative fact. An absent fact stays unknown. Duplicate facts—including identical
duplicates—wrong kinds, unknown node IDs, extra fields, non-boolean values and a
mismatched catalog digest reject. The CLI also rejects duplicate JSON keys.

One snapshot describes one asserted player/turn. Facts cannot introduce another
player field. The module cannot authenticate the supplied player, evidence IDs,
visibility provenance or freshness; the output states that limitation. It does
not discover, read, or merge any observation file beyond the one explicitly
passed by the caller. A later live integration needs separately reviewed
player-scoped observation collection and current-snapshot proof.

## Source relations and uncertainty

`nodes` retain source attributes and declarations, with separate observation
annotations. `edges` contain prerequisite relations and their original source
provenance; `other_relations` retains replacements, upgrades and exclusions
without treating them as prerequisites. Together they preserve all selected
source edges. `unexpanded_edges` retains the requested depth frontier.

Each research prerequisite edge may show the supplied target completion fact.
That fact does not propagate into the subject's completion or unlock status.
Building/district prerequisite conditions remain unevaluated because this
snapshot lacks constructed-city state and support limits. For example, an
unlocked Campus does not satisfy Library's district condition.

`prerequisite_options` groups edge IDs by subject, relation and source table for
future rendering. A group is not an executable choice or a conjunction. Every
group retains `connective=unverified`, `evaluation=unknown`, and a visible reason:
the source pair/field declarations do not establish the effective engine's
AND/OR connective. Armory's Barracks and Stable rows remain distinct alongside
their mutual-exclusion relationship.

Explicit modifier RequirementSet `ALL`/`ANY` operators appear under
`source_requirement_sets`, with original declarations and membership provenance.
They apply only to that source set, are never transferred to prerequisite-edge
groups, and are not evaluated. Missing referenced rows remain in
`unresolved_source_references`. Source condition fields/defaults and schema
provenance are preserved rather than replaced with an assumed feasibility score.

`observation_digest` binds the complete normalized supplied snapshot; fact order
is not meaningful. Only relevant node observations appear in the queried graph.
`projection_digest` binds the full resulting artifact. Changes to observation
values, evidence, snapshot scope, source catalog or query alter that result.
Input objects remain unchanged and output mutation cannot alter the catalog.

## Reproducible synthetic example

First extract the installed base catalog using the commands in
[the catalog contract](base-source-catalog.md). For the recorded installed-source
digest, save this **synthetic example, not live player evidence**, as
`/tmp/catalog-observations.json`:

```json
{
  "observation_version": 1,
  "catalog_digest": "374e0d0ac07db839deb4f86ddf8da955da874757d97609a806a354107fa982ff",
  "snapshot_id": "synthetic-example-1",
  "player_id": 0,
  "turn": 12,
  "facts": [
    {"node_id": "TECH_POTTERY", "predicate": "researched", "value": true, "evidence_id": "synthetic-example-1"},
    {"node_id": "TECH_WRITING", "predicate": "researched", "value": false, "evidence_id": "synthetic-example-1"},
    {"node_id": "DISTRICT_CAMPUS", "predicate": "unlocked", "value": true, "evidence_id": "synthetic-example-1"}
  ]
}
```

```bash
PYTHONPATH=src /home/alexk/documents/civ-arena/.venv/bin/python -m civ_arena.catalog project \
  --catalog /tmp/base-source-catalog.json \
  --observations /tmp/catalog-observations.json BUILDING_LIBRARY --depth 3 \
  --output /tmp/library-observed-graph.json
```

If extraction produces a different digest, explicitly bind the synthetic input
to that catalog before querying. The Library result records observed Pottery
completion and Writing non-completion; Campus's unlock fact is retained while
Library's district condition remains unknown. No claim that Library is ready,
blocked by an evaluated engine rule, or achievable in a number of turns is made.

## Verified evidence

The focused catalog plus projection gate passed **56 tests, zero failed/skipped**
in 0.45 seconds; Ruff passed. This includes 25 projection cases for absent versus
false facts, no unlock propagation, city-condition uncertainty, Armory groups,
explicit ALL/ANY provenance, depth frontiers, deterministic hashes, copy custody,
invalid or extra inputs, conflicting observations, bounds, environmental
independence, CLI output and duplicate-key refusal. Initial lint found four long
source lines; the corrected retained invocation passed.

Four examples use synthetic observations over the retained installed-source
catalog: Library, Armory, Archer and Craftsmanship. Their repeated projections
were identical, with respectively 4, 11, 10 and 2 source nodes. CLI Library output
matched the pure API exactly. The installed catalog digest remains unchanged.
Full logs, synthetic input, four projections, command output and digest summaries
are under `runs/observed-dependency-projection-20260906/`, with `summary.json` and
`checks.json`. No broad repository suite or live game action was run for this
isolated addition.

A [standalone browser preview](dependency-browser-preview.md) renders explicitly
supplied artifacts with selectable examples and inspectable provenance. It does
not connect this overlay to current live model thinking.

## Bounds and follow-up

Query bounds remain 512 nodes, 2,000 supporting rows and depth 0–8. Observation
JSON is limited to 256 KiB; full projection JSON to 2 MiB. The provenance-rich
Library example is approximately 28 KiB. These artifacts are a UI-neutral
foundation, not a packet wired into the live controller's 8,000-character
context budget. A future model/browser view should budget presentation while
retaining resolvable provenance, explicit unknowns and snapshot scope.

Independent exact-head review remains pending for this new projection. Effective
ruleset resolution, authenticated player-scoped observations, browser rendering,
model consumption, action forecasting, strategic optimization and calibrated
certainty are separate follow-up slices. None is demonstrated by these offline
fixture and synthetic-example checks.
