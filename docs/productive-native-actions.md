# Typed district and project production

This adds a native action path for productive city choices beyond units/buildings.
It does not choose a district, alter strategic policy, activate a live game, or
extend the mod's mutation recorder. Policy integration is a separate change.

## Interface and authority

`get_available_production(city_id)` retains unit/building rows and adds:

```json
{"kind":"district","item_id":"DISTRICT_CAMPUS","cost":78,"turns":7,"placements":["8,29"]}
{"kind":"project","item_id":"PROJECT_ENHANCE_DISTRICT_CAMPUS","cost":25,"turns":4}
```

These are schema examples; they do not assert those costs/coordinates are current.
New rows use exact full native Type IDs. Costs/turns come from the current native
build queue; -1 turns means the accessor returned unknown. District placements
are canonical axial coordinates converted from native engine X/Y. Projects have
no placement field. Counts, typed spellings, duplicate IDs, numeric ranges and
placement arrays are closed and bounded. Legacy units/buildings cannot use the
reserved `PROJECT_` or `DISTRICT_` suffix namespace, including in current queues.
This prevents a stripped unit name from being mistaken for a district/project.

`set_city_production(city_id, item_id, dest=None)` remains the single action.
The facade omits dest entirely for ordinary unit/building calls. Projects reject
any destination; districts require one exact canonical destination. Purchases
are unchanged and do not gain project/district support. Referee identity and
lease checks still apply, followed by the adapter's owner-qualified ID checks.
The native city/local-player/owner and current **empty queue** are checked again
inside the new action, immediately before engine legality checks/submission.

Only currently enabled new choices are emitted (`CanProduce` catalog plus full
eligibility and `CanStartOperation`). District targets must come from fresh
`GetOperationTargets`, be currently native-visible and owned, and pass exact
coordinate-specific `CanStartOperation`. New placement excludes existing city/
district plots and any native feature, resource or improvement. This is a
conservative execution restriction, **not knowledge of hidden resource identity**:
resource IDs/names never enter option rows, result receipts or rejection detail.
The generic refusal does not say what exists on the plot. It also does not prove
that creating a district has no yield/opportunity cost. Already-placed district
resume, plot purchasing, feature removal, swapping city plots and wonder placement
are outside this first path.

Unavailable accessors, non-booleans, malformed numbers, absent PLOTS result keys,
sparse/duplicate targets and bound exhaustion raise; none becomes an empty catalog.
An explicit empty PLOTS array is observed no targets. The query permits at most
128 district rows, 128 project rows and 256 total target entries. Project/district
hashes in `get_cities` retain their full exact Type ID rather than an opaque queue.

## Request and receipt lifecycle

Only the new typed path uses `productive_native.execute_once`: it requires an
already connected InGame state, holds the existing connection lock and invokes
one `_locked_execute`. It does not use the vendor reconnect-and-resend wrapper.
No automatic connection/reconnect, fallback or second submission occurs. Injected
non-vendor test transports must explicitly implement `execute_write_once`.
Cancellation propagates and releases the lock. Native reader/writer/state identity
is checked through readback; changed generations cannot confirm an old request.
Legacy action/connection behavior is unchanged.

The vendor transport removes its `---END---` sentinel, so three retained protocol
markers establish completeness: `PRODUCTIVE_OPTIONS_END|1`,
`PRODUCTIVE_REQUEST_END|1` and `PRODUCTIVE_STATE_END|1`. Direct Lua fixtures may
include the trailing transport sentinel; actual transport need not. Strict new
receipt parsers reject missing/extra/trailing records and mismatched typed IDs.

The native request calls `CityManager.RequestOperation(city, BUILD, params)` once,
with PARAM_PROJECT_TYPE or PARAM_DISTRICT_TYPE plus engine X/Y. Its result is only
a submission. A bounded subsequent read must match the exact queue hash. District
readback additionally matches requested plot index, district type index, owner,
city district membership and the restricted feature/resource/improvement predicate.
Readback proves queue/placement admission, **not construction/project completion**.
Both submission and verification use the existing five-second production bound;
there is no match deadline, simulator clock or replay-hash change. Timeout is an
ambiguous outcome that may still apply; the runtime must stop, not retry it.

An accepted result contains a closed `production_readback` object: kind, item_id,
production_hash, plot_index, district_index, city_id, dest, verification, observed,
completion_proven=false and coverage. Observed has production_hash, plot_index,
district_index, owner_id, belongs_to_city and consequence_free. For projects the
placement values are -1/false. Here consequence_free names only the restrictive
native predicate above; it is not a strategic benefit claim.

The mod still excludes districts, queues and tile ownership from its ledger/digest.
The receipt explicitly states `separate_observation_not_mod_digest_or_mutation_ledger`.
No broad mutation permission was added: `_TOOL_ATTRS` remains unchanged. An unchanged
hash or zero watchdog violations therefore does not prove placement or its side
effects. Existing unit/building actions still use their previous receipt format.

## Verified findings

Repository evidence is under `runs/productive-native-evidence/`. The settled
commands and exact counts are recorded in `summary.json`; complete gate logs are
`focused-final2.log` and `ruff-final2.log`. The focused gate includes translated
real Lua, parser/shape refusal, actual GameConnection/FakeTunerServer socket
execution (including stripped sentinel), asynchronous exact readback, no-op and
wrong-owner/plot refusal, one-shot dead-socket/cancellation/generation controls,
legacy tool/schema/replay behavior and fixture-owned FakeMod state.

`legacy-byte-proof.log` compares three unit/building request strings and two
purchase requests against base f768aa2; all are byte-identical. A prior fixture
incorrectly appended a request-count assertion after a Lua top-level return;
wrapping that fixture in a local function corrected eight test failures. The
existing growth catalog Lua fixture needed explicit city ownership and empty
project/district catalogs for the new strict availability contract. Intermediate
logs remain retained. The sentinel mismatch was corrected before the final gate
and is exercised over the actual local vendor transport, not only canned rows.

Shipped source usage: `base/assets/ui/panels/productionpanel.lua:1919–1923`
city district membership/type, `:1961` district cost, `:1979–1982` district X/Y;
`ui/strategicview_mapplacement.lua:205–219` exact placement checks and `:289`
operation targets; `productionpanel.lua:411–418` project submission. Full path root
is `/media/alexk/RAID5_Storage/SteamLibrary/steamapps/common/Sid Meier's Civilization VI/steamassets`.
The parent's separate stopped-state read qualification is retained under main
`runs/100-round-live-20260907T011202Z/productive-read/` and
`productive-accessors-read/`: it qualified offered Campus plots, current cost/turns
and city-center district membership accessors. Those reads did not execute this
candidate's district/project writes.

## Follow-up probes

Independently review the frozen commit, then qualify the exact generated options
query on the stopped native state using the parent's single-client guard. Before
fresh play, separately capture a legal district submission and exact placement
readback on the preserved stopped game. Once the district completes during fresh
play, capture a freshly enabled project and exact queue readback there. This first
native project qualification is part of the bounded live run; it is not a prior
pass or a requirement to artificially complete construction. Retain full
wire and event custody. Root owns strategic/curator integration and its full gate.

## Blocked checks

No tuner, provider, desktop, browser or service calls were permitted in this lane.
Native action qualification remains blocked pending that separately controlled
stage. No full repository release suite was run here, as assigned.

## Evidence gaps

Source/API and fake-transport checks do not prove native mutation timing or live
continuation. Simulator district/project rules are not implemented; a simulator
cannot reproduce these accepted native effects and must not be advertised as such.
The ordinary fake catalog remains units/buildings unless a test explicitly seeds
productive_options. The qualified stock map has legal plots, but future maps/rulesets
may have none. This first slice deliberately refuses placement consequences it
cannot represent; it is not a complete city-development planner.
