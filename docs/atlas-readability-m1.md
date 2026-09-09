# Atlas readability foundation (M1, 2026-09-07)

First slice of the spectator-observer program (readability → comparison →
graphs → capture). It changes only the viewer: the observed atlas' palette,
elevation marks, unit and city badges, observed-ownership borders, generated
map key and selection panel, plus the roster field the bundle needs to classify
owners. No game, tuner, provider, model-context or PuppeteerMod code changes.

## What changed

- **One palette source.** `src/civ_arena/minimap_static/palette.json` holds the
  CSS tokens, terrain fills, elevation marks, unit glyph table, owner colours,
  border styles and the health-ring rule. `minimap.render()` injects it as a
  second non-executable JSON block (`<script id="palette" type="application/json">`)
  and emits its tokens as a `:root{--…}` block; `minimap_static/style.css` now
  contains no literal colours. The observation bundle and its digest never carry
  presentation data. The match room's `--gold`/`--teal` and `--chart-*` tokens
  are pinned to the same file by a drift-guard test.
- **Pure geometry module.** `minimap_static/geometry.js` (hex vertices, the
  edge↔neighbour table, terrain classification and naming, ownership resolution,
  owner classes and colours, unit glyph lookup, border and tint grouping) is
  concatenated in front of `app.js` into the single CSP-hash-pinned script and is
  evaluated under Node by `tests/test_minimap_geometry.py`.
- **Hills ≠ mountains.** Hills keep their biome fill and carry a ridge path;
  mountains use the mountain fill and carry a peak path. Marks are SVG paths,
  not font glyphs. A hex whose native type is unsupplied or unsupported is
  hatched (`UNKNOWN`), including normalized-only `HILL` rows whose biome is lost.
- **Unit and city badges.** Type glyph (St Bd W Sc Sl A Sp WC G Q; `?` with a
  dashed ring for a type outside the table — the raw token is never echoed),
  square for civilians and circle otherwise, a light surface ring plus dark
  stroke and dark glyph ink so the badge reads on every fill, a four-segment
  health ring from `hp_bucket` (or `hp ÷ 25` for own units), an inner ring when
  fortified. Cities are hexagonal badges with the population inside. Own actors
  wear the seat colour, explicit barbarians coral with `!`, foreign actors with
  no `owner_id` white and labelled "owner not supplied".
- **Observed borders.** For each hex whose newest receipt carries an integer
  `owner_id ≥ 0`, each of its six edges is drawn inset: solid when the neighbour
  is observed with a different owner or `-1`, dashed when the neighbour's
  ownership is unsupplied or unobserved (edge of observation, not an extent).
  Segments are grouped into one path per owner/style/staleness (six paths on
  the 10k-hex fixture), plus a faint tint per owner. A missing key is never
  read as unowned; remembered hexes emit nothing.
- **Owner classes from the roster.** `minimap.build()` copies the packet's
  `public.players` into each snapshot as `public_players` (closed fields;
  `null` when unsupplied; malformed → `invalid public roster`). The viewer
  classifies an owner as seat, roster major, non-major (city-state or other —
  never promoted to a civilization) or unclassified when no roster exists, and
  names seats/majors from `civ_name`. A new bundle limit line states the rule.
- **Generated key and selection facts.** `renderLegend()` builds five groups
  (Terrain, Elevation, Actors, Ownership, Provenance) with swatches drawn by the
  same functions as the map. `selectTile()` states the terrain name, the
  three-way ownership sentence (owned / observed unowned / not observed, plus
  the null-field case), the city centre and each receipt's age.
- **Latent bug fixed.** `fit()` divided by a zero-width map during resizes and
  full-page screenshots, writing `-Infinity` into the viewBox; it now keeps the
  raw bounds until the map is laid out.

## Palette decisions

Terrain fills were chosen by a constrained random search over OKLCH ranges per
terrain, scored with the dataviz validator's OKLab/CVD distance functions
(`runs/atlas-readability-evidence-20260907/atlas-palette-search.log`). Best
score 0.919: every pair reaches normal-vision ΔE ≥ 15 and colour-blind ΔE ≥ 8
except grassland/desert (13.8 normal) and coast/mountain (7.4 CVD), both of
which carry secondary encoding — the mountain peak mark, the legend label and
the selection panel's terrain name. Dark glyph ink `#0a1419` has ≥ 4.5:1 on
every land fill and badge colour; the light halo has ≥ 3:1 on coast and ocean
(pinned by `tests/test_minimap_palette.py`). Owner colours: seats gold/teal
(match-room tokens), barbarian coral, foreign white, non-major orchid, roster
majors periwinkle/lime/pink with a grey overflow; the teal/orchid and
gold/lime pairs are below the CVD floor and rely on the legend name, the `P<n>`
label and the selection panel (documented secondary encoding). The dataviz
lightness band and chroma floor are chart-series rules and were not applied to
map fills (snow must be light, ocean dark).

## Verified findings

- Repository proof: **114 passed, 0 failed, 0 skipped** in 1.51 s for
  `tests/test_productive_map.py tests/test_minimap.py tests/test_minimap_palette.py
  tests/test_minimap_geometry.py tests/test_dashboard_map.py tests/test_dashboard.py`
  (log `/tmp/observer-m1-gate-r5.log`); Ruff clean over `src` and `tests`
  (`/tmp/observer-m1-ruff.log`); the concatenated script passes `node --check`
  inside the gate. Baseline before the change: 94 passed. Wide suite
  (`pytest tests -q --ignore=tests/integration`): **1926 passed, 1 skipped,
  0 failed** in 666 s (`/tmp/observer-m1-wide.log`).
- Browser proof: **11 groups passed, zero page/console errors** with
  `/usr/bin/python3` Playwright and headless `/usr/bin/google-chrome`
  (`runs/atlas-readability-evidence-20260907/browser-proof.py`, summary
  `browser-summary.json`, log `/tmp/observer-m1-proof-r7.log`). Groups: hills
  vs mountain fills and marks; generated legend equal to map fills; unit badge
  glyphs, rings, dashed unknown, coral+`!` only for explicit barbarians, white
  for foreign without owner; city population badges; borders only from owners
  0/1/6 with solid/dashed styles and non-major classification; selection panel
  three-way ownership and receipt age (keyboard Enter on a hex under a badge);
  390×844 without horizontal overflow; spectator union skew with stale layers at
  opacity 0.5 and newest-receipt ownership; CSP-hash execution with only
  `file:` requests; 10,000-hex / 40-actor fixture redraw in 312 ms with 6 border
  paths; the retained run `minimax100-20260907T183425Z` at turn 38 through an
  ephemeral loopback dashboard (273 hexes, hills and mountains present,
  non-major borders for real owners 2 and 4, load 0.28 s).
- Screenshots for the operator review: `atlas-fit-desktop.png`,
  `atlas-zoom-3x.png`, `atlas-mobile.png`, `atlas-union.png`,
  `live-spectator-t38.png`, `live-spectator-t38-zoom.png`,
  `live-spectator-t38-zoom-p1.png` in the evidence directory.

## Follow-up probes

- Foreign units and cities carry no `owner_id`/`name` in retained packets
  (`agents/llm/context_curator.py` foreign field lists); the viewer shows
  "owner not supplied". Adding those fields is a model-context change for a
  separate decision.
- The playwright harness dispatches a synthetic click on a hex that sits under
  a unit badge because the badge intercepts real clicks by design (it selects
  the actor); the hex remains keyboard-selectable. A pointer affordance for the
  hex under a badge (e.g. click the badge rim vs centre) is a design question.
- The 10k-hex timing is a synthetic single-seat fixture; a two-seat union at
  map scale with many stale receipts has not been measured.

## Blocked checks

None. No live tuner, match, physical display or provider access was used.

## Evidence gaps

Earlier proof attempts (`/tmp/observer-m1-proof-r0..r6.log`) failed on harness
issues — `inner_text` on SVG text, a badge intercepting a hex click, a CSS
uppercase transform in a text assertion, the file export opening on a seat
rather than the union — and on the real `fit()` bug above; each was fixed and
the final run is the authority. Colour choices are validator-scored, not
operator-approved; the milestone review decides them.
