# Native nonmajor unit observations

The native unit read now enumerates `PlayerManager.GetAlive()` rather than only alive major civilizations. Barbarian and city-state units therefore reach the omniscient adapter observation used by the referee. This changes observations only: it does not issue orders, change mod state, or infer diplomatic hostility.

Each native row appends the owning player's `Player:IsBarbarian()` result. The Lua read requires an actual boolean; a missing or malformed API result fails the read instead of inventing a classification. The parser accepts exactly the historical eleven fields or twelve fields with lowercase `true`/`false`. Older replay and FakeTuner rows retain an absent classification. `is_barbarian=False` is not a peace assertion, and an absent field is not `False`.

The existing visibility boundary still runs before observations reach agents. Own units retain the optional boolean; foreign units carry it only while their coordinate is currently observable. Remembered-only and unseen barbarian/city-state units remain absent. The closed foreign allowlist includes only this extra boolean; movement, orders and arbitrary source fields remain private. Consumers must test `is_barbarian is True`, never treat every foreign owner as hostile.

## Verified findings

A single bounded post-stop GameCore_Tuner probe on 2026-09-06 verified `GetAlive()` and `IsBarbarian()` against the stopped 100-round attempt. It read 41 units, including 19 explicitly classified barbarians. Offline projection using each seat's own-unit sight rings retained a barbarian warrior at axial `14,15` for P0 and a barbarian scout at `47,14` for P1. This was a read-only candidate observation, not an accepted model action or resumed match. No established port-4318 client existed before or after the probe. Local ignored evidence: `runs/native-nonmajor-observations-20260906/native-probe.json`, `native-probe.log` and `native-query.lua`.

Behavioral tests execute generated Lua with major, city-state and barbarian players, parse new and historical rows, reject malformed booleans, and verify visible versus hidden/remembered projection. Existing identity, coordinate-frame, fake-driver and visibility checks cover compatibility.

## Follow-up probes

Integrated model request rendering and any policy consuming the new boolean require separate review. Fresh model decisions and native live match execution remain unverified by this change.

## Blocked checks

None in this scoped implementation. The native probe disconnected successfully.

## Evidence gaps

The adapter's visibility remains its existing own-unit/city radius approximation, not native line-of-sight or fog proof. It may disagree with the UI around terrain/occlusion. `cities_read()` still enumerates major civilizations, so this unit-only change does not add city-state city observations. No diplomatic war-state classification or nonmajor-turn control is included. No full release gate was run in this isolated lane; integration owns that gate.
