# Historical district and project receipts in the observed atlas

## Verified findings

The map now displays historical district placements and project queue admissions from the event log's `set_city_production` call/result pairs. A dashed square with D identifies a district placement at its exact recorded axial coordinate. Selecting it opens the request → accepted readback → completion unknown graph. Projects appear in the city graph and journal as queued; there is no project map marker because the receipt does not supply a project position.

The pure `productive_map` projector joins adjacent records only when the full run, player, agent, phase, turn, tool, visibility and idempotency identity matches, including field types. The call argument digest must match. Admission requires an accepted, nonduplicate result and the closed `production_readback` shape. Exact item/city/destination, hash and plot/type indices must agree. District receipts also require observed ownership, city membership and the clear-placement predicate. Missing, rejected, duplicate, mismatched or malformed receipts remain journal-only and never create map geometry. Unlogged actions cannot be reconstructed from this surface.

The existing selected-player export remains private: other seats' calls, receipts and packet details are not embedded. Explicit spectator exports retain separate perspectives. Original call/result SHA-256 digests bind each annotation. Dashboard redaction still occurs after raw custody verification; redaction that corrupts a placement coordinate refuses rendering. Extra native metadata is not passed through the annotation schema.

Historical packet selection admits only receipts at or before that packet's sequence and turn. The separate latest-packet-plus-receipts option includes subsequent selected-seat tool receipts up to the dashboard's already bounded event prefix, while leaving the actual model packet's actors and terrain unchanged and dated. Every receipt card names its result sequence/turn, turn age within the chosen receipt window, and the board packet's sequence/turn. The existing global first-future-turn cutoff also excludes late records carrying stale turn labels. A receipt-only tile does not create terrain or ownership observations.

Repository validation on 2026-09-07 is retained under `runs/dashboard-productive-evidence/`: `focused-final2.log` records 94 passed, zero failed/errors/skipped/deselected; `ruff-final2.log` passes four Python files; `node-final.log` passes JavaScript syntax. The focused command was:

```text
PYTHONPATH=src:tests /home/alexk/documents/civ-arena/.venv/bin/python -m pytest tests/test_productive_map.py tests/test_minimap.py tests/test_dashboard_map.py tests/test_dashboard.py -q
```

`browser-final.log` and `browser-result.json` record seven passing synthetic browser groups, zero page/console errors and zero HTTP requests. The proof used a newly launched headless Chrome and file URLs, closed only that browser, and never connected to the running dashboard or physical browser. Checks cover scoped annotations, exact marker coordinates, keyboard selection and receipt graph, historical cutoff, city project journal, 390px layout and spectator filtering. `player-desktop.png`, `player-mobile.png`, `player.html` and `spectator.html` retain the viewed artifacts. Synthetic inputs are in `synthetic-events-final.jsonl`; these are not engine observations.

The first focused pass was 93 tests; a later strict numeric-identity regression raised the final total to 94. The first Ruff check found one new overlong prose line, corrected before the final check. The first synthetic board contained only one terrain tile and magnified the label; the final fixture uses a larger synthetic board and the marker uses a compact D glyph. Earlier captured logs remain available.

## Follow-up probes

Parent can independently review and integrate this local commit after the running match's frozen-source stage. Inspect `/map` with retained genuine district/project receipts on that later source, including a receipt arriving after the latest model request. Native project admission should be displayed only when an authoritative receipt is actually recorded.

## Blocked checks

Current-match installation and integration were deliberately excluded to preserve the active run's source and services. No native game query, action, provider call, installed-mod edit, dashboard restart or active-browser access occurred in this lane. The full repository release gate remains parent-owned and was not run here.

## Evidence gaps

The browser evidence is synthetic and does not establish rendering against the active match. The native district qualification belongs to the parent's separate evidence lane and is not counted as browser proof here. Historical placement/queue receipts do not establish completion, current presence, current queue, or continued ownership. District/project changes are not covered by the existing mod state hash or mutation ledger; the annotation explicitly cites separate readback. These visual additions do not establish stronger strategy, faster model decisions or full-match reliability.
