# Turn 37: native control transfer, 2026-09-06

## Verified findings

The fresh `minimax100-20260906T193543Z` run failed after 36 complete
rounds and 73 released seat turns. It ran clean commit
`76f4dce0a344f749d6ddbea42b4f92dcba9d649e`, tree
`7de749d64ce92e4db54a7c5b6a8058cecd67e978`, mod 0.3.10 SHA256
`f93a7b173d4dbae19c2efa1314224bb224d7a101b2f2102c3719b5174a807bbf`.
The preceding repository gate was 1,308 passed, zero failed/errors,
one skipped, with 1,309 tests collected exactly once. That gate did not
establish live reliability.

The authority is the preserved [event log](/home/alexk/civ-arena-reliability-20260904/runs/minimax100-20260906T193543Z/events.jsonl),
SHA256 `10648b48d042e993a92fbe74a5e34244738131f5a37e157a8ee820647b8be1d4`.
Its one `MATCH_END` matches the summary and records completed cleanup,
no logical active lease, and a cached final observation. The incoming
native lease remained held. The 100-round structural validator reports
FAIL; this run and its 30-round prefix do not satisfy acceptance.

At event 4825, outgoing seat 0 on turn 37 had two uncommanded mutations:
warrior `u0:458757` appeared and gold fell from 192 to 32. The wire brackets
these changes between the pre-handoff digest and the ledger collected
after `Puppeteer.GuardedHandoff(0,37,1)`. There was no agent purchase
command. The held city was producing a builder; the new unit was a warrior.
The exact native source of the purchase is inferred from that bracket,
the native AI logs and control-state evidence; no native purchase-event
receipt was captured.

The configured `declare_own_endpath_drift=true` allowance admitted 218
exact owned-unit movement rows across 73 audited seat turns. The warrior
spawn and gold loss were outside that allowance and remained a violation.
Both agents submitted accepted game actions. Provider POST counts were
17 and 31. Seventeen informational popups were observed dismissed; no
recovery sweep or deadline expiration was reported.

Popup clearing was already scheduled every five seconds. Events 4800
and 4814 found all allowlisted contexts hidden. A later passive physical
capture shows incoming seat 1's Bronze Working completion dialog after
the runner had stopped. Closing it alone cannot repair the violated run.

The complete read-only [post-stop census](/home/alexk/civ-arena-reliability-20260904/runs/100-round-live-20260906T193543Z/poststop-control-probe-r3.json)
found `IsHotseat=false`, `AutoplayManager.IsActive=false`, local seat 1,
outgoing seat 0 nonhuman, and incoming seat 1 human. The display-switch
API demotes the outgoing seat; movement freezing does not prevent native
economic actions. The earlier two read-only census attempts retained
incomplete receipts because nested mod printers emitted the transport
sentinel. They are not complete probe passes.

A unique [native save backup](/home/alexk/civ-arena-reliability-20260904/runs/100-round-live-20260906T193543Z/native-save-backup.json)
preserved the failed turn before prospective changes: 405,711 bytes,
SHA256 `16f82f2460073116b42e327e2b77210a03eabb23906243eab91c43939fa27e15`.
Native logs were copied with byte counts and hashes into the same support
folder. No original save slot was replaced.

The first prospective attempt stopped before ending a turn because its
immediate InGame `Players[0]:IsHuman()` assertion remained false after
reflag. A separate read showed the configuration and GameCore flag true,
while InGame's player flag remained stale. These are distinct VM views.

The corrected [prospective probe](/home/alexk/civ-arena-reliability-20260904/runs/100-round-live-20260906T193543Z/prospective-human-handoff-r2.json)
observed two handoffs: seat 1 turn 37 to seat 0 turn 38, then seat 0 to
seat 1 turn 38. It restored the inactive next seat's human configuration,
verified both GameCore human flags, ended the current local human turn,
waited for the next lease, and switched only after outgoing deactivation.
Each switch reported outgoing human false; reflag plus GameCore readback
restored both seats to human. The Bronze Working dialog closed through
its existing nonce-bound callback. This was a postmortem diagnostic using
the shipped `UserForced` end-turn action, not model play or a resumed run.

## Resulting implementation

The hotseat driver uses `human_handoff.py`. It validates exact seat, turn,
lease, activity and human identities before switching. It only switches
from an inactive outgoing seat, reflags that inactive seat through InGame
configuration, and verifies the authoritative GameCore human census.
Before normal local end-turn it checks the census again. The next local
switch happens at the next successful engagement. No native movement or
economic drift is newly excused.

Each helper uses one connection, one dispatch and an exact receipt.
No ambiguous command is reconnected or replayed. Each five-second bound
includes lock wait. The transition clock begins before end-turn helper
execution and remains shared with release/engagement recovery. Successful
receipts are event-log audits persisted before the next operation. A later
failure records its exact operation and identity with final effects explicitly
unavailable. The wire remains supporting evidence.
The existing mod's legacy freeze-and-switch helper remains for legacy
adapter callers, but the two-seat hotseat runner no longer invokes it.

The final affected repository gate reports 155 passed, zero failed/skipped,
in 160.76 seconds (`/tmp/civ-human-handoff-affected-r4.log`). The initial
64-test gate and subsequent 153-test gate passed. Independent review of
`5226adb` found a P2: batching audit emission lost a successful switch
receipt when reflag failed. Per-operation emission fixes that gap. The
first correction gate caught an event-envelope `turn` keyword collision
(154 passed, one failed); the receipt is now nested under its own payload
field. The failed log remains `/tmp/civ-human-handoff-affected-r3.log`.
The 155-test gate includes executable Lua control-transfer probes and the
fake TCP driver; it is not the full release gate or native match proof.

## Follow-up probes

- Review the final commit and behavioral gates, including native-AI
  purchase simulation, wrong-seat refusal, connection replacement and
  cancellation after a sent end-turn.
- Prove the checked-in normal end-turn path with a complete native seat
  turn. The diagnostic's forced end-turn did not establish that behavior.
- On the next fresh run, audit both GameCore human flags at every
  engagement and immediately before end-turn; retain the native logs.
- Continue adaptive briefing, health recovery, threat-sensitive production
  and observed minimap work in their isolated branches; their proof must
  bind the implementation actually launched.

## Blocked checks

The 100-round operational target and two consecutive fresh 30-round
acceptance runs remain unmet. A new fresh run requires the affected local
checks, full release gate and independent source review. Do not continue
the failed event log or treat the diagnostic turns as agent completions.

## Evidence gaps

The source and native boundary probes do not establish strategic quality,
full-game victory, complete mod mutation accounting, or freedom from every
possible native AI action. The final observation in the failed run is
explicitly cached. Structural replay is separate from engine-state proof.
No operator input is needed for the candidate sequence, but the failed
match cannot be reclassified as unattended acceptance after postmortem
diagnostic actions.

## Reproduction and stop procedure

The original launch command and configuration are preserved in
[launch.json](/home/alexk/civ-arena-reliability-20260904/runs/100-round-live-20260906T193543Z/launch.json).
The local launcher invocation was:

```bash
PYTHONPATH=src /home/alexk/documents/civ-arena/.venv/bin/python \
  runs/sixty-round-development-20260905/launch_sixty.py \
  configs/live-hotseat-strategic-minimax2-100.yaml 100
```

Use a fresh unique run ID after the release gates. On the first unresolved
watchdog, timeout or recovery failure, stop new agent/UI actions, allow
bounded disconnect/cleanup, and preserve the event log, summary, wire,
native logs and game. Verify process identity before signaling. Never
restart Steam/X or overwrite a save to conceal the failed transition.
