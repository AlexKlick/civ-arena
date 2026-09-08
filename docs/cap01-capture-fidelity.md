# CAP-01 — capture fidelity: observation intervals, roster discovery, transport-enforced census

Lane: `feat/cap01-capture-fidelity-20260907` (worktree
`/home/alexk/civ-arena-cap01-fidelity-20260907`), based on `22d98d0`
(the CAP-03 F-04 fix on top of the spectator lane's `5d5221b`).
Packet: CAR-CAPTURE-FIDELITY-002 (CAP-01), findings F-01, F-02, F-03, F-08.

## Resulting implementation

- **Observation-interval semantics (F-01).** Every round event now
  carries `observed_at`, `source_cursor` (the ring entry that triggered
  it), `boundary` (`attach | hook_observed | gap_inferred`) and
  `state_at_boundary`. A round claims boundary state ONLY when its hook
  was the batch's LAST entry AND directly observed — never for attach
  or gap-inferred rounds; a batch containing multiple boundaries marks
  the earlier ones `state_at_boundary=false` (their reads ran after
  later entries existed). Pinned by
  `test_multiple_turn_boundaries_in_one_poll_do_not_forge_historical_snapshots`
  against an adversarial fake that mutates between reads
  (`advance_on` poll sources, between-turns attach).
- **Actor honesty (F-02).** `HUMAN_TURN_END` carries window start/end
  cursors; its ambient rows carry `actor_class=human_seat` as WINDOW
  PROVENANCE with `actor_id` null — an owner's-unit delta inside a
  window can no longer be read as owner-commanded action. Pinned by
  `test_opponent_damage_to_owned_unit_is_not_owner_action`,
  `test_ambient_rows_carry_owner_and_evidence_fields`, and
  `test_net_diff_does_not_claim_complete_command_sequence`.
- **Transport-enforced census (F-03).** `SpectateTransport`
  (spectate_capture.py) wraps the adapter with a capability allowlist:
  observer ops (poll_status/read_trace/refresh_digest/observe) and the
  mod's lease-free ambient-window recorder commands via `read_raw`;
  lifecycle (setup/inject_mod/teardown) permitted and counted
  separately as `recorder_lifecycle` (mod injection executes
  recorder-local Lua, it is not a gameplay mutation). EVERYTHING else —
  `act`, `write_raw`, `begin/end_phase`, `set_puppet`, any unlisted
  attribute — raises `RecorderCapabilityError` BEFORE dispatch. The
  summary's `command_census` is the wrapper's measured census;
  `game_writes` is zero by enforced structure, not declaration. Pinned
  by `test_spectator_transport_rejects_mutating_operation_before_send`,
  `test_transport_census_counts_lifecycle_separately`, and
  `test_bootstrap_and_teardown_are_inside_spectator_command_audit`.
- **Gap fallback + roster discovery (F-08).** Ring wraps and
  engine-turn jumps declare `capture_gap` audits and the next round
  opens `gap_inferred` — coarse and honest, never fabricated
  (`test_ring_wrap_and_source_reset_preserve_gap_and_generation`,
  `test_trace_ring_wrap_declares_gap_and_gap_inferred_round`). The
  roster is DISCOVERED from the first OVERVIEW read, not trusted from
  config: sparse/unconfigured ids (city-states) get explicit
  `minor_or_unconfigured` coverage status, configured-missing is
  reported
  (`test_sparse_player_ids_and_unconfigured_actor_classes_have_coverage_status`).
- **Config.** `poll_s` 2.0 → 0.5 s: the 2026-09-07 pilot lost turns
  21/23/27/35 to 64-entry ring wraps between 2 s polls at 7 majors;
  0.5 s polls hold ~4× more ring headroom. Residual wraps still
  surface honestly as gaps.

## What this lane deliberately does NOT claim

- Per-window diffs remain owner-attributed state-interval evidence;
  exact-action imitation and reasoning stay ineligible at the exporter
  plane (CAP-03's `spectator_interval` class separation).
- The mod-side ring is unchanged (64 entries); larger-ring/mod changes
  were deferred as disposable-scenario-first per the packet.

## Evidence

`runs/cap01-evidence-20260907/`: focused spectate suite 97 passed /
2 failed + ruff clean. Both failures are the INHERITED stale-rehearsal
class, not lane defects: the parent's `5d5221b` config commit (7
majors) landed after the parent's full gate, and
`test_spectate_rehearsal.py` reads that real config file (expects
`(0, 1)` players) while the fake server seeded only the 2-major mini
engine (`KeyError: 2` on the 7-seat window open). Fixed on the
integration branch by CAP-02's rehearsal-assertion update + fake
seat-seeding; the integrated full gate is this lane's authority —
sibling gates would be invalidated by the first merge anyway.
