# Graph display redesign (M3, 2026-09-07)

Third slice of the spectator-observer program (readability → comparison → graphs →
capture). The match room's vertical two-column action graph becomes a horizontal
swimlane journal, and the observed atlas draws a unit's recorded decision row on the
map instead of one dashed line. No game, tuner, provider, model-context or
PuppeteerMod code changes; `/api/run` and the atlas bundle are unchanged.

## What changed

- **Swimlane journal** (`dashboard_static/journal-core.js` + `journal.js`, replacing
  `renderGraph` in `app.js`): one lane per seat, one column per engine turn, time left
  to right over a window of thirteen recorded turns centred on the selected one. The
  selected turn (and any turn Shift-opened beside it) is expanded into one node per
  recorded call in recorded order, with same-lane arrows that mean recorded order and
  nothing else; every other turn is a pill with the recorded call count, the per-kind
  glyph counts (◉ observation ▶ action ✎ note) and the rejection count. Consecutive
  calls with the same tool *and* status fold into one dashed `×n` node that opens in
  place; the foot prints "N calls shown · M folded into runs". Jump pills state how many
  recorded turns sit outside the window and move it without changing the selection. A
  seat with no recorded turn says "no seat turn recorded"; a lane the filter emptied
  says "No matching calls recorded". Filters gain Notes. Keyboard: arrows walk a lane,
  Up/Down cross lanes, Enter/Space activates. The lane gutter stays pinned while the
  strip scrolls. The pure half (filter, fold, pill summary, window, geometry) is
  Node-evaluated by `tests/test_journal_core.py`.
- **Atlas decision overlay** (`minimap_static/geometry.js` `decisionShapes`,
  `weightText`; `app.js` `drawDecision`, `candidatePolygon`, `focusCard`): selecting an
  owned unit draws a ring on the audit's origin hex, an inset outline on every recorded
  candidate hex whose stroke width is `1 + 3 × recorded weight`, a hatched dim outline on
  excluded candidates, thin origin→candidate lines and one solid edge to the recorded
  chosen destination. Labels print at most twice (the chosen hex and the strongest
  unexcluded runner-up); a label that would overprint the chosen one is placed below its
  hex with an `↑` prefix. Printed weights never round toward certainty (`>0.99`,
  `<0.01`). Cards and polygons are two views of one row and highlight each other by
  click or keyboard. The legend's three new Provenance items are drawn by the same
  primitive as the map. The dashed `.path` and its legend item are gone.
- **Copy**: journal legend "→ Recorded order, not causality"; the atlas `#graph-note`
  states outline thickness, hatch and solid edge in the contract's words; the journal foot
  keys both glyph sets.

## How it was built and reviewed

Two Opus 5 implementer lanes worked in parallel worktrees from written specs
(`runs/spectator-compare-graph-evidence-20260907/specs/m3-*.md`): J journal (4274c57,
83 focused tests, 13 smoke checks) and D atlas overlay (a1752fe, 115 focused tests, 9 smoke
groups; two browser-found defects fixed before hand-off: the candidate outline was not a
hit target, and two same-row labels overprinted on real data). The coordinator's read of
lane D produced three rulings before merge (weight text never rounds to certainty, labels
above the badge layer, the chosen polygon says so). Merged at 9ae3060 (focused 199 passed).

Codex `gpt-6-astra` (reasoning high) reviewed the merged head read-only
(`codex-review-m3-r1.md`): NOT MERGEABLE, seven findings, all verified against the source:
four journal state-custody defects (selection lost when an opened turn left the window,
follow-latest bypassing the journal resets, a run hint going stale when a poll grew the
run, stale jump counts when turns appended), two atlas gaps (cards not keyboard
activatable, an inherited-property filter lookup) and one disputed ruling (the dropped
runner-up label). Fix pass 1 (58cf8d5, 73fe6e6) and fix pass 2 (840501d follow-toggle
reset, f984ce7 `↑` prefix) addressed them; Codex r2 (`codex-review-m3-r2.md`) confirmed all
seven fixed and found one regression in the new re-land rule (a poll appending a turn moved
the reader's scroll) plus a documentation precision defect; fix pass 3 (e3b2241 centre-keyed
re-land, 1bd72b1 docs) closed both; Codex r3 (`codex-review-m3-r3.md`) is MERGEABLE with the
re-land predicate traced through seven adversarial states.

## Verified findings

- Repository proof at 5332122: **203 passed, 0 failed, 0 skipped** in 6.91 s for the nine
  focused files (`tests/test_productive_map.py tests/test_minimap.py
  tests/test_minimap_palette.py tests/test_minimap_geometry.py tests/test_dashboard_map.py
  tests/test_dashboard.py tests/test_dashboard_compare.py tests/test_compare_core.py
  tests/test_journal_core.py`; `focused-gate-5332122.log`; M2 baseline 182). Ruff clean
  over `src tests scripts`; `node --check` clean on the five match-room scripts and the
  atlas concatenation; zero `innerHTML`/`fetch(`/`eval` strings in atlas assets; zero hex
  literals in the new JS and the atlas CSS. Wide suite (`pytest tests -q
  --ignore=tests/integration`) at 5332122: **2015 passed, 1 skipped, 0 failed** in 616 s
  (`wide-gate-5332122.log`; M2 baseline 1994).
- Browser proof at 5332122 (`browser-proof.py` r5, `browser-summary.json`,
  `proof-report.md`, log `proof-r5.log`): **55 checks, 55 passed, 0 failed; 0 page errors,
  0 console script errors**, 79 requests all same-origin over the two ephemeral loopback
  servers; per group G1 3/3, G2 7/7, G3 6/6, G4 14/14, G5 4/4, G6 12/12, G7 3/3, G8 1/1,
  G9 1/1, G10 1/1, G11 1/1, perf 1/1. The earlier r4 run on the same head read 54 / 53,
  the one failure being a stale spec formula for the scroll landing (the spec was amended
  to the J6 ruling; the code did not change). Coverage: G4 journal (window T14–T26 and T32–T38 with jump counts, node counts
  equal to a Python re-fold of `/api/run`, pill texts equal to the API counts, pill click /
  Shift-click, a `×3` run opening with "Call 1 of 3", selection surviving a poll as the same
  DOM node, filters, geometry and scroll landing, keyboard walk, copy, same-lane arrows);
  G6 atlas overlay (candidate count and hatch from the embedded data, max stroke width
  `1 + 3·max(weight)`, exact-zero weights at width 1 and never "unscored", one chosen edge
  at `point(dest)`, ≤ 2 labels, card/polygon linkage by click and keyboard, fortify and
  null-selection rows, cleanup, `#research` regression); G1–G3, G5, G7–G11 from M2
  re-proven. `DashboardStore.load()` median of 5 at this head: 0.0889 s in r5 and 0.0897 s in r4
  (1.17× / 1.18× the pre-M2 0.0757 s baseline, inside the 1.25× report-only budget).
- Screenshots for the operator review (evidence dir): `journal-1440.png`,
  `journal-window-t20.png`, `atlas-decision-t38.png`, `atlas-decision-t38-zoom.png`,
  `matchroom-1440.png`, `matchroom-390.png`.

## Follow-up probes

- Unit badges paint above the overlay, so a mouse click at the exact centre of a hex that
  carries a badge selects the actor, not the candidate outline (the outline off-centre,
  the keyboard route and the card route all reach it; proven). Re-ordering layers would
  put candidate outlines over badges and block unit selection instead; leave as is unless
  the operator prefers the other trade.
- The journal's DOM-level behaviours (selection custody, hint refresh, scroll landing,
  keyboard walk) are covered by the browser proof and the lane smoke, not by pytest; the
  pure core is pytest-covered. Codex's surviving-mutation list (`codex-review-m3-r2.md`)
  names the remaining DOM-only lines.
- A `below` runner-up label sits inside the next hex row; the `↑` prefix states the
  attribution, a leader line would state it graphically. Operator's call.

## Blocked checks

None. No live tuner, match, physical display or provider access was used.

## Evidence gaps

Codex reviewed in a read-only sandbox and did not execute tests; its verdicts are
source-review verdicts and the counts above are the coordinator's own runs. Colour and
layout choices are validator- and proof-checked, not yet operator-approved; the milestone
review decides them.
