# Codex review round 1 — integrated head 2e49af9 (2026-09-07)

Model: gpt-6-astra, effort high, 6m59s. 13 findings (6 P1, 7 P2).
**All 13 verified against source by the integrator; the headline finding
additionally verified against the pilot's recorded data.** Remediation
ticket: CAP-R1 (this branch).

## P1 — verified

1. **Shared ambient snapshot corrupts all windowed ambient data.**
   `PuppeteerMod.lua` has ONE `ambient_snapshot`; `BeginAmbientWindow`
   overwrites it, so the driver's multi-player window pattern (open all
   observed seats; close/reopen AIs at round start) diffs player X
   against player Y's snapshot. DATA-VERIFIED on the closed pilot run:
   AI manifests are 63%+ spawn/despawn storms (1,361 spawned + 1,431
   despawned of ~4.4k rows); human windows' `before` values match
   player-0's round-start census in **0/146** checked rows (e.g. row
   says `u0:196608 before=30,28`, census says `6,32` — the before is
   p6's unit). Census snapshots/digests/hooks are UNAFFECTED (no window
   dependency). Fix: per-player snapshot table in the mod (keyed
   `ambient_snapshots[playerID]`), version bump, regression via an
   adversarial fake matching mod semantics + a wire-order test.
2. **Exporter cannot join production decision audits.** `_emit` audits
   route through `strategy_audit_event`, which nests `decision_id` /
   `directive_id` / `provider_requests` inside `strategy_payload_json`;
   `export_dataset` reads them top-level → strategic runs export null
   identities/costs with NO pre-boundary flag. Fix: exporter reads the
   nested field (both shapes), flag when absent.
3. **Turn-keyed boundaries merge hotseat agents.** Boundaries dict +
   observation/action/reference collection key by turn only; two agents
   on one engine turn become one mixed sample. Fix: key by
   (turn, agent_id)/player_id.
4. **Economy-refresh directive replacement loses identity.** Boundary
   emitted at :303 before `_economy`; the exhaustion path (:720+) calls
   `_decide` (a second provider request) and swaps `self.directive`
   without updating `_directive_id` or emitting a follow-on boundary —
   request counts understated, quiet turns cite the superseded
   directive. Fix: emit replacement-accept boundary (new decision_id,
   same-turn accounting) in the refresh path.
5. **Allowlist prefix check admits compound Lua.**
   `lua.startswith(_ALLOWED_READ_RAW)` passes
   `Puppeteer.DumpAmbient(); Puppeteer.FinishAllMoves(0)`. Fix: exact
   call match (parse the single call + numeric arg, reject anything
   else), test allowed-prefix-then-mutation.
6. **`state_at_boundary` still overclaims.** Batch-last-entry predicate
   (:1286) doesn't cover engine advance between the trace read and the
   census reads. Real but narrower than stated: the census digest
   bracket (`consistent`) already surfaces mid-read movement; fix is a
   corroboration requirement (label true only when bracket consistent
   AND batch-last), not a structural rewrite.

## P2 — verified

7. **Fresh mid-turn attach with an empty ring loses the attach
   interval**: round open requires HOOK_ENTER; the in-flight turn is
   dropped and the next turn mislabeled `attach`.
8. **Roster discovery can't see minors**: OVX iterates
   `GetAliveMajors()`; city-states are undiscoverable through the live
   query (the fake returns all `mod.players` — test proves an
   unreachable behavior). Fix: dedicated roster read enumerating all
   players, or drop the minor-coverage claim.
9. **Teardown before MATCH_END**: a teardown failure in the finally
   skips summary/MATCH_END entirely (closeout evidence lost; original
   failure masked). Fix: teardown in its own suppress-and-record
   wrapper AFTER match_end, or census the lifecycle without awaiting
   teardown before the summary.
10. **Digest bracket contradiction passes**: validator trusts
    `consistent=true` without checking `before == after`. Fix: equality
    check in the shared core; fix the default fixture.
11. **Engine-jump gaps invisible at validation/export seam**:
    `capture_gap` audits (CAP-01's fallback) aren't in the validator's
    eligibility nor the exporter's interval cursors (hard-coded null at
    :261 despite rows carrying `source_cursor`). Fix: consume
    capture_gap in eligibility + carry cursors.
12. **Manifest under-hashes inputs**: samples depend on summary.json +
    llm_costs.jsonl but the manifest digests only events.jsonl. Fix:
    digest every consumed input.
13. **Key-rotation redaction gap**: successful `response_doc` forwarded
    unredacted; WireLog sweeps only the current env key — an echoed OLD
    key (rotated mid-flight) reaches disk. Fix: sweep the record at
    fire time with the key actually sent (`sent_key`), not at write
    time with the env key.

## Notes

- Codex's focused pytest was blocked by its sandbox (/tmp read-only);
  these are source-backed findings — the integrator's verification
  above is the confirmation record.
- Finding 1 retroactively explains the pilot's small human-window row
  counts (cross-player diffs book only where positions happen to
  differ) — the "sane-looking" rows were p6-before/p0-after composites.
