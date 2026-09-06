# Preserve known native mountains as impassable terrain

Candidate built from `684efdff`, 2026-09-06. The isolated change maps the five
known base-game mountain terrain names to the existing simulator `MOUNTAIN`
movement class. The parser previously counted these names as unknown and fell
back to `PLAINS`, allowing routine scouting to select them as walkable tiles.

The exact vocabulary is `TERRAIN_GRASS_MOUNTAIN`, `TERRAIN_PLAINS_MOUNTAIN`,
`TERRAIN_DESERT_MOUNTAIN`, `TERRAIN_TUNDRA_MOUNTAIN`, and `TERRAIN_SNOW_MOUNTAIN`.
The parser's existing optional `TERRAIN_` prefix handling remains supported.
Installed base `terrains.xml` declares these five rows with `Mountain="true"`
and `Impassable="1"`; source identity and rows are retained in
`runs/native-mountain-20260906/base-terrain-source.json`.

Native metadata retains the original type token and recognizes its exact base
biome. A mountain has `hills: false`; it is not normalized into a hill. No new
metadata fields are added. Unknown or stacked mountain-like names remain
unclassified and counted under the existing unknown-terrain policy; this change
does not infer terrain semantics from an arbitrary suffix.

The existing scouting land-cost whitelist already excludes `MOUNTAIN`, so no
scouting scoring or movement code changes are required. The normalized class
survives both visible and remembered tile projection. Routine scouting assigns
such destinations zero selection probability, chooses a known walkable
alternative, or holds if every adjacent tile is unsupported. Explicit tactical
orders still pass through their existing validation and engine path. This is a
conservative routine-ground-scout correction, not new support for special
mountain-traversal abilities.

Known tundra/snow mountains now contribute to the existing cold-biome evidence
summary. That summary still labels any periphery suggestion unverified; the
change supplies no map bounds, climate model, or calibrated probability. Mod,
provider settings, movement allowance, and the running live match are untouched.

## Verified findings

The retained live prefix report from `minimax100-20260906T181834Z` joins preceding
model-context terrain to subsequent scouting actions: 37 move submissions,
including six destinations already reported as native mountains, all with
unchanged immediate coordinates. This is partial observational evidence, not a
completed-match result or proof about every submitted move. Its exact prefix hash
is retained in `runs/native-mountain-20260906/mountain-observation-probe.json`.

Focused parser/metadata, projection, curated-budget, and scouting tests:
**79 passed, 0 failed, 0 skipped** in
`runs/native-mountain-20260906/civ-mountain-focused-r2.log`. Targeted Ruff:
`runs/native-mountain-20260906/civ-mountain-ruff-r2.log` (`All checks passed!`).
The new cases drive native wire rows through projection and the actual scouting
planner/executor, covering all five biomes, current versus remembered terrain,
unknown names, and a fully blocked mountain ring.

The first local run reported 69 passed and 10 failed because the new executor
assertion omitted the existing idempotency key; this was a test-fixture defect,
not an implementation regression. The corrected fixture and final logs are
retained. No live engine, provider, or desktop call was made by these tests.

## Follow-up probes

Read-only review of the exact candidate, then the affected release gate after
integration. A fresh live run must show that routine scouting stops selecting
known mountain tiles. Accepted orders still need subsequent engine observations
to establish displacement; no speedup or strategic-strength result is claimed.

## Blocked checks

None for this isolated source/test slice. Live integration and validation are
intentionally deferred while the current match uses its pinned implementation.

## Evidence gaps

There is no fresh live proof of this candidate, no generalized passability model,
and no change to the historical or currently running match's acceptance status.
