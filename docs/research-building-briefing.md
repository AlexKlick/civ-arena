# Curated research and City Center building hints

The optional `agents[].llm.research_building_briefing: true` setting adds direct
research-to-building facts to the strategic controller's existing context. It
requires `decision_mode: strategic_autopilot` and an existing `adaptive_context`
configuration. The default is false. No match configuration was changed for
this implementation.

When the controller needs fresh research choices, it makes one combined
`get_available_research(research_building_briefing=True)` facade observation.
The keyword belongs to the controller; the model tool schema is unchanged.
The referee records the keyword in the existing tool event, and replay forwards
it. A fresh observation is still necessary after actions that may change research
availability. Quiet turns with an active research choice defer this catalog.

The loaded `GameInfo` tables supply exact current research IDs, technology costs,
direct `Buildings.PrereqTech` links, declared yields, building prerequisites,
conditional fields, and unevaluated modifier references. Building hints are
restricted to player-applicable City Center buildings with known-false
placement, wonder, purchase-only, and internal flags. The complete applicable
building set is assembled before removing replaced base buildings and before
filtering to currently researchable technologies. This follows the installed
`base/assets/ui/techandcivicunlockables.lua` ordering. Leader and civilization
traits are resolved for the requested current player; barbarian-only and other
players' unique buildings are excluded.

`building_unlocks.source` identifies effective loaded database relations;
`base_source_catalog_used` is false. The installed base UI's OR interpretation of
`BuildingPrereqs` is labeled separately and applies only to that table. The
effective slice hash binds the returned player, turn, IDs and values, not the
entire ruleset or installed content. Technology cost is `GameInfo.Cost`, not a
measured effective cost, research progress, or turn estimate.

City ownership is projected through the existing facade. Owned-city building and
pillaging state, river and terrain requirements, completed districts, satisfied
prerequisites, and future native build legality remain explicitly unknown. An
empty hint list does not establish that a technology has no other unlocks.
Districts, projects, placement buildings, wonders and indirect modifier effects
are outside this slice. No technology ranking, automatic research choice, or new
action authority is introduced; production still requires its own fresh native
catalog and legality checks.

## Bounds and failure behavior

The single read bounds source tables at 1,024 technologies/buildings/replacements,
2,048 leader/civilization traits and building prerequisites, and 4,096 yield or
modifier rows per table. The closed output allows at most 128 technologies,
64 buildings, 768 field rows, 512 yields, 256 prerequisites, 512 modifier
references and 128 traits. These are protocol admission bounds, not token
truncation: overflow, missing required tables, malformed rows, mismatched player
identity and incomplete frames fail the observation. No partial slice is sent
to the model. Missing optional scalar fields are represented as unknown.

Every selected building carries all 12 declared field slots. The parser retains
its own final record and counts even if the transport removes `---END---`.
Projection reparses the closed schema and checks the complete normalized
envelope; a self-consistent attacker-supplied hash does not admit extra fields.
The existing turn and transport watchdogs bound the read.

The complete accepted hint snapshot is mandatory adaptive context. A soft text
target cannot cut it in half or silently remove rows; the existing provider
whole-request token counter and admission limits still apply. This option is
rejected without adaptive context, so it cannot inherit the legacy briefing
character ceiling. The simulator explicitly reports this native observation as
unsupported; structural replay forwarding is covered, but native semantic replay
of the new data is not established by simulator tests.

## Verified findings

Repository evidence is retained in `runs/research-briefing-evidence/` in the
isolated `feat/research-building-briefing-20260907` worktree, based on
`a941896a22acfc7ffef88687aa21d49642365834`. `binding-start.json` binds the base
and the installed source files used to establish direct relation semantics.
The reviewed source catalog `512a0c4` was reference material only and was not
imported or used as runtime data.

- `affected-r2.log`: 213 passed, 0 failed, 0 skipped, 62.92 seconds. This is the
  focused and affected gate, not the repository release suite. It includes local
  Lua execution, effective-value changes, trait and replacement filtering,
  malformed/incomplete output, forged projection data, disabled behavior,
  recorded keyword forwarding, and complete count/generation request equality.
- `ruff-r3.log`: Ruff passed for `src tests scripts`.
- `disabled-byte-proof-final.log`: 12 disabled context variants match the
  baseline bytes across three observation scenarios and four rendering modes;
  the model tool schema also matches the baseline bytes.
- Earlier failures remain in `focused-r1.log` (44 passed, 1 failed),
  `affected-r1.log` (208 passed, 2 failed), and `ruff-r1.log` (nine E501 findings).
  The initial test fixture confused the directive-result size limit with the
  adaptive context target. Another fixture expected no research refresh after a
  real action invalidated the catalog. Both were corrected without weakening
  admission or freshness. The actual source regression was a feature check
  preceding the trusted-match guard; guard order was repaired. All original
  failing logs remain available.

## Follow-up probes

After independent source review, qualify the exact generated read through the
native GameCore transport for each intended human seat. Retain the generated
query hash, complete response, parser result, active player/turn, and loaded
content identity. Verify `PlayerConfigurations` and the leader/civilization
accessors, trait tables, database boolean representations, researchability
methods, support flags and optional field availability. Compare at least one
ordinary and one unique/replaced building against the player's effective rules.
This is a read-only qualification; any future game action uses the existing
separate production/research checks.

## Blocked checks

Native, provider and desktop operations were excluded from this implementation
lane. Consequently no GameCore read, provider request, browser behavior, or live
game outcome was tested here. The independent-review and native-read gates are
required before enabling the flag in a live match. No active or retained match
configuration was changed. The full local release suite was excluded by the
parent's bounded task and was not run.

## Evidence gaps

Local Lua fixtures establish query logic and parsing, not native API availability.
The effective loaded ruleset identity remains `unverified`; the slice hash is not
a content-manifest hash. Dynamic trait modifiers, modifier effects and actual
city prerequisites are not evaluated. Model use of the hints and improved
research choices have no provider or live evidence. This change makes no claim
about 100-round completion or stronger strategy.

A separate read-only examination of the preserved
`minimax100-20260907T024441Z` run found that accepted P0 settler moves in event
pairs 49/50 and 979/980 did not change position in subsequent observations.
The retained query checked generic `CanStartOperation(..., MOVE_TO, nil, true)`
before constructing destination parameters. The axial-to-native coordinate
conversion in the wire was consistent. Destination-specific legality, path cost,
river and feature causes were not observed, so the reason for no movement remains
unproven. `capital-move-wire-review.json` retains the exact existing evidence
paths and missing-parameters diagnostic; no movement code was changed here.
