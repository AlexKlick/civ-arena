# Comparison views (M2, 2026-09-07)

Second slice of the spectator-observer program (readability → comparison →
graphs → capture). It adds the research diff, the base-catalog tech tree, eight
per-seat timeline small multiples and the per-turn decision strip to the match
room, a research block to the observed atlas, and the projection that feeds
them. No game, tuner, provider, model-context or PuppeteerMod code changes.

## What changed

- **Projection** (`src/civ_arena/dashboard_compare.py`, wired into
  `dashboard.project_events`): per-seat research sets folded from each seat's own
  bound request packets (custody = recorded length, digest, marker and an integer
  seat identity), a delta-only history with a synthetic `baseline` row when the
  history is capped, a latest-packet diff, per-seat-turn timeline rows with a
  source tag per field (`audit` / `events` / `tool` / `packet` / `null`), watchdog
  violation marks, and per-turn `strategy_delta` / `economy` / `growth` blocks.
  Every string is redacted, every list bounded with a warning. See
  `docs/browser-match-room.md` for the field-level rules.
- **Tech tree asset and route**: `scripts/derive_tech_tree.py` derives
  `dashboard_static/tech-tree.json` (68 technologies, 90 prerequisite links,
  per-era `[11, 8, 7, 9, 8, 7, 8, 10]`) from the base source catalog
  (`base-source-catalog.json`, sha256 `f6e0dffa…3ebd`, catalog digest
  `374e0d0a…82ff`); `GET /api/tech-tree` validates the asset on every request
  and withholds it as a 404 otherwise. The tree is base-catalog layout with
  unverified group semantics, never the effective ruleset of the match.
- **Match room** (`dashboard_static/compare-core.js` + `compare.js`): the pure
  logic (research fold, diff, chart scales and segments, decision sentences,
  tree depth, half states, option gating) lives in `compare-core.js`, which the
  browser and the Node-evaluated tests run unchanged; `compare.js` only wires it
  to the DOM. Charts place x by turn number and join points only across
  consecutive recorded turns; a missing value or a missing turn breaks the line.
  The directive pill names its source (`model`, `autopilot`, or any other
  recorded token) and says "First recorded directive" when there is nothing to
  compare against. The tree legend prints the catalog provenance note verbatim.
- **Atlas** (`minimap.py`, `minimap_static/app.js`): each snapshot carries a
  closed `research` object (researching, researched or `null` when unsupplied,
  recorded options and their source token); the aside prints one block per
  displayed seat and shows option chips only when the packet observed them.
- **Copy corrections** the review demanded: the journal legend now reads
  "Recorded order, not causality"; the strategy panel now says "Listed values
  are seeded selection weights, not probabilities of success."

## How it was built and reviewed

Three Opus 5 implementer lanes worked in parallel worktrees from written specs
(`runs/spectator-compare-graph-evidence-20260907/specs/`): A Python projection
(137 passed), B match-room UI (11 smoke checks), C atlas block (132 passed);
merged at 7978686 (focused gate 155 passed, wide 1967 passed / 1 skipped,
integrated Playwright proof 26/27 — the one failure was the pre-existing
"not causality" copy gap).

Two independent reviews then ran on the merged head: the coordinator's own
read of every source file, and a Codex `gpt-6-astra` (reasoning high)
adversarial pass (`codex-review-r1.md`). Codex returned NOT MERGEABLE with seven
blocking findings, all verified real by reading the source: raw directive keys
reached the payload unredacted; a packet without a researched list read as an
empty set (a false removal); a capped history lost its baseline; charts were
spaced by row index and bridged missing turns; a null directive source read as
"Model update" and a first record as "Unchanged"; a boolean `you.player_id`
passed packet custody; the honesty copy was missing. It also listed eight
single-line mutations of the match-room script that no repository test caught.

Fix pass 1 (e807fe7) addressed all of them and moved the pure logic into
`compare-core.js` with `tests/test_compare_core.py`. The Codex re-check
(`codex-review-r2.md`) confirmed six fixed and found one follow-on defect in the
baseline repair: a baseline row's names were treated as acquisitions at the
baseline turn, in the tree status and in `tech_added`. Fix pass 2 (a35d4f8)
corrected that and moved three more renderer decisions into the core module;
the final Codex confirmation (`codex-review-r3.md`) is MERGEABLE with no new
defect.

## Verified findings

- Repository proof at a35d4f8: **182 passed, 0 failed, 0 skipped** in 6.35 s for
  `tests/test_productive_map.py tests/test_minimap.py tests/test_minimap_palette.py
  tests/test_minimap_geometry.py tests/test_dashboard_map.py tests/test_dashboard.py
  tests/test_dashboard_compare.py tests/test_compare_core.py`
  (`/tmp/observer-m2-fix2-gate.log`; M1 baseline 114); Ruff clean over
  `src tests scripts`; `node --check` clean on the three match-room scripts. Wide
  suite (`pytest tests -q --ignore=tests/integration`) at the final head:
  **1994 passed, 1 skipped, 0 failed** in 610 s (`/tmp/observer-m2-wide3.log`;
  M1 baseline 1926 passed).
- Browser proof at a35d4f8: **29 checks, 29 passed, 0 page errors, 0 console
  script errors**, 42 same-origin requests (`browser-proof.py`,
  `browser-summary.json`, `proof-report.md`, log `/tmp/observer-m2-proof-r5.log`)
  over a copied `minimax100-20260907T183425Z`: research chips equal a Python
  recomputation at turns 38, 1 and 9; exactly one `/api/tech-tree` request across
  two polls; 68 nodes / 90 edges with the right `done` and `researching` halves;
  eight charts with turn-proportional ticks (max drift 0), gold end labels equal
  to the last recorded values, the turn-38 `UNAUTHORIZED_ACTION` mark, hover and
  click wiring; decision strip texts for model, autopilot and first-record
  turns; the atlas research block for both seats; all honesty strings present
  and the forbidden ones absent; no horizontal overflow at 1440 and 390 px;
  reduced-motion clean; DOM identity stable across polls; routes as specified.
- Load time (`DashboardStore.load()` median of 5 on the same run): 0.0757 s on
  the M1 tree, 0.0838 s after lane A (1.11×), 0.0856 s at the merged head; at
  a35d4f8 a quiet 9-call probe reads 0.0844 s (1.12×) while the r5 sample taken
  under a concurrent wide pytest run and a Codex review read 0.1078 s (1.42×).
  The budget (≤ 1.25×) is report-only; the projection path did not change
  between e807fe7 and a35d4f8.
- Screenshots for the operator review (evidence dir): `matchroom-1440.png`,
  `research-tree-1440.png`, `timelines-1440.png`, `decision-strip-1440.png`,
  `matchroom-390.png`, `atlas-research-t38.png`.

## Follow-up probes

- The renderer wiring (renderer registration, the cutoff passed at the call
  site, chart click → `selectTurn`) is covered only by the browser proof, not by
  the pytest suite; the pure core is covered by `tests/test_compare_core.py`.
- The `baseline` / "history not retained" rendering and the consecutive-turn
  segment breaking have no live-data coverage on this run (10 history rows, all
  real; turns 1–38 contiguous). They are covered by unit tests only.
- The server's `research.diff` block is computed from each seat's latest packet;
  the client recomputes the diff from the folded per-seat sets at the selected
  turn (equal at the latest turn, proven). One of the two could go.
- Contended-load timing should be re-measured on an idle host before any
  optimisation is considered.

## Blocked checks

None. No live tuner, match, physical display or provider access was used.

## Evidence gaps

Codex reviewed in a read-only sandbox and did not execute tests; its verdicts
are source-review verdicts and the counts above are the coordinator's own runs.
Colour and layout choices are validator- and proof-checked, not yet
operator-approved; the milestone review decides them.
