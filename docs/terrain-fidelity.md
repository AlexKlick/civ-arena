# Native terrain and directional cold-biome evidence

This additive slice starts at the independently reviewed context-packet commit
`71f72bee5bf0bee216bad6afc69215e2608da7f2`. The earlier curator review and
this terrain slice have separate evidence. The active match remains unchanged.

## Representation and visibility contract

`terrain` remains the existing normalized simulator movement class. The parser
still maps tundra to `PLAINS`, snow to `DESERT`, and recognized hills to `HILL`.
Those compatibility mappings and current scouting scores are unchanged. A new
optional `native_terrain` object preserves the bounded source token and known
biome/hill distinction alongside it:

```json
{"terrain":"HILL","native_terrain":{"type":"TERRAIN_TUNDRA_HILLS","biome":"TUNDRA","hills":true}}
```

The token is retained exactly as received when it contains at most 64 ASCII
uppercase letters, digits or underscores with a leading letter. Legacy wire
names without `TERRAIN_` remain accepted. Unsupported but well-formed tokens
retain their source spelling with null biome/hills; malformed/oversized tokens
produce null fields. Recognized metadata covers grass, plains, desert, tundra,
snow and their hills, plus coast/ocean. It does not invent feature, mountain,
resource, yield, latitude or water-temperature information. Existing unknown
normalization fallback accounting remains in `unknown_terrain`.

Metadata passes through the existing targeted terrain read, adapter cache,
player projection and curator. The adapter rejects returned coordinates outside
its requested own-entity visibility set before admitting them into memory. When
a tile becomes remembered, its last-seen normalized and native terrain remain
frozen; owner/city fields are stripped. The projection gates observable/remembered
coordinates again and copies only closed static metadata fields. Unseen tiles
supply neither metadata nor existence. Returned metadata cannot mutate the
adapter's remembered copy. Simulator observations lacking native metadata remain
without it; absence/null is explicitly unknown in the curated context.

## Directional evidence and hypothesis limits

The curator supplies a bounded `cold_biome_evidence` summary when native metadata
is present and an owned reference exists. The reference is the first owned city
by stable city ID, or otherwise first owned unit by stable unit ID; its actual
coordinate is recorded. This is a declared sampling origin, not a discovered
map center or pole.

North, same-row and south sectors each count known projected tiles, cold land
(tundra/snow), observed non-cold land, and unclassified tiles. Water and missing
or unsupported native biomes are unclassified; coast is not evidence of warmth.
Only known tiles contribute. Remembered observations are included and labeled;
unexplored tiles are never counted as non-cold land. Counts describe unequal
observed coverage, not estimates of the unknown hemisphere.

When one sector has observed cold land, the opposite has zero observed cold land
and some observed non-cold land, the summary may carry
`possible_northern_periphery_unverified` or its southern equivalent. This is an
explicit heuristic hypothesis, not a map edge, a measured latitude, a probability
or an instruction to scout in the opposite direction. The summary exposes the
counts and unknown coverage needed to question it. No hypothesis is produced
when the opposite side is only unexplored, water or unknown. The active map
script, dimensions, wrap and climate generation rules remain unverified.

The grid orientation is source-backed: installed base
`maps/utility/maputilities.lua:431-455` searches y from zero upward for the south
edge and from height minus one downward for the north edge; line 499 also names
height minus one as north. `maps/inlandsea.lua:144-145` independently places its
southern coordinate below its northern one. Existing adapter axial r equals
engine y, so increasing r is north in engine-grid convention, independent of
screen rotation. This establishes direction only. The retained primary-source
excerpts and hashes are in
`runs/terrain-fidelity-evidence-20260906/map-axis-source-proof.log`:

| Installed source | SHA-256 |
|---|---|
| `utility/maputilities.lua` | `a2214f53817e24209ecf0aae4c45fbc065ff928668157771b4c025f518090927` |
| `inlandsea.lua` | `a27a2f03d42d502ddb8c52936b6e4aa95e99d58de482bb2fba16231eb6e75872` |

The same source capture includes configurable terrain-generation thresholds.
Those are not evidence of the running map's script or distance to its edge and
are not used by this implementation.

## Verified findings

The captured focused repository gate passed **189 tests, zero failures/skips**
in 28.32 seconds. Two additional complete-request budget cases passed in 0.05
seconds: native fields plus directional evidence and all strategic metadata fit
an 8,000-character request; an insufficient 1,800-character budget fails before
any model call. Focused Ruff passed. Complete commands/counts and development
logs are retained in `runs/terrain-fidelity-evidence-20260906/summary.json`.

Coverage includes tundra/snow/desert and hill fidelity despite identical
normalized classes; bounded unknown tokens; hidden-owner and unseen-coordinate
exclusion; remembered metadata freezing and copy custody; reversed north/south
cold evidence; unknown/water coverage; executable Lua coordinate fixtures; fake
wire behavior; existing visibility, replay, belief, scouting and curator tests.
These are fixture/repository observations, not measurements of real engine
terrain delivery after integration. Two initial import-order Ruff findings were
corrected; their log is retained.

## Follow-up probes

- Independent read-only review of the exact terrain commit.
- Full release gate after integration with the previously reviewed curator.
- A future bounded host probe of actual terrain tokens, dimensions, wrapping,
  active ruleset and map orientation; until then map-geometry claims stay unknown.
- Browser presentation and live model behavior with this additional evidence.

## Blocked checks

This isolated lane is excluded from the active live controller's tuner/provider
and desktop session. It made no live tuner connections, provider requests or
desktop input. Fake wire tests use local fixture servers only. The running
match's outcomes cannot validate this source change.

## Evidence gaps

No live latency benefit, improved strategy, calibrated geographic inference,
image interpretation, exact pole/boundary detection, map-wide visibility or
completed acceptance run is established. Additional terrain metadata consumes
the existing fixed context budget and may reduce optional distant tiles retained.
Required owned state and adjacent known terrain still fail explicitly if they
cannot fit. There is no gameplay policy change or automatic north/south order.
