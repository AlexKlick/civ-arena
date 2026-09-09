# CAP-02 — unified validation, immutable provenance, honest replay status

Lane: `feat/cap02-validation-provenance-20260907` (worktree
`/home/alexk/civ-arena-cap02-validation-20260907`), based on `22d98d0`
(the CAP-03 F-04 fix on top of the spectator lane's `5d5221b`).
Exchange-2 findings addressed: **F-05** (certificate/validator guarantee
mismatch, snapshot-deletion counterexample, misleading
`identical`/`replayed_events`) and **F-06** (launch identity rewritten by
later commits), plus the synthetic-replay fallback risk adjacent to F-05.

## Verified findings

- **F-05 reproduced and fixed.** The old `_spectate_certificate` did not
  require any `SPECTATOR_SNAPSHOT`: deleting every snapshot and
  renumbering sequence numbers passed (`identical=True`), and
  `replayed_events=len(records)` named a comparison that never ran.
  Fixed by the shared core + truthful fields (pinned by
  `test_certificate_rejects_snapshot_deletion_after_sequence_renumbering`).
- **Synthetic-fallback risk closed.** A spectate-shaped log with a
  missing or malformed `summary.json` previously fell through
  `_is_spectate_run` into the Arena path — zero TOOL_CALLs would have
  run a fabricated synthetic match. `replay_run` now refuses loudly
  before any Arena construction.
- **F-06 reproduced with a preserving nuance, then fixed.** The real
  closed run launched at `b511346` with a dirty tree (the seven-major
  config edit was uncommitted); its immutable `run_identity` audit event
  preserved that truth, but the summary recomputed identity at closeout
  (`5d5221b`, clean) and contradicted it. Now: `capture_launch_identity`
  freezes once; the summary carries `launch_identity` AND
  `closeout_identity` separately, `identity` aliases launch for compat.
- **The real run classifies honestly** (`test_legacy_dirty_launch_
  remains_honestly_classified`, on a COPY — the original stays
  byte-identical): outcome `interrupted`, structural PASS with the
  unpaired final round legitimate, `decision_eligible=False`, launch
  identity `b511346*` dirty=True surfaced via `derive_provenance`,
  identity divergence flagged.

## Resulting implementation

- `src/civ_arena/spectate_audit.py` (new): the ONE structural core —
  envelope, seq contiguity, forbidden action kinds,
  START/SNAPSHOT/END interleave **with snapshot presence**, per-round
  START↔SNAPSHOT turn agreement, operator/seat consistency, digest
  brackets, summary round cross-check; `classify_outcome` (six classes,
  honest inference for legacy summaries); `final_interval_state`
  (open/closed observed fact); `build_manifest`/`check_manifest`
  (retained trust root); `derive_provenance` (read-only reconciliation).
- `src/civ_arena/replay.py`: certificate rebuilt on the core;
  truthful fields (`audit_valid`, `profile=structural`, `outcome`,
  `final_interval`, `capture_complete_for_declared_scope=None`,
  `reexecution_performed=False`, `comparison_result="not_performed"`,
  `schema_note`); legacy keys retained and documented as derived; CLI
  says "structural audit", never "replayed"; pre-Arena refusal for
  summary-less spectate-shaped logs.
- `src/civ_arena/game/civ6/validate_run.py`: `validate_spectate` on the
  core with the FULL profile and outcome classes — `completed` still
  requires clean; `operator_stopped`/`interrupted`/`timed_out`/`crashed`
  validate their honest prefix (failure fields required) ;
  `running_prefix` (no summary) audits the sealed-prefix shape only.
  **Structure, integrity, completion, and dataset eligibility are
  separate decisions**: `decision_eligible` requires completed +
  consistent snapshots + closed final interval + no gaps; inconsistent
  censuses, open intervals, `trace_gaps`, and round turn-spans surface
  as `eligibility_notes`.
- `src/civ_arena/game/civ6/live_driver.py` (identity lifecycle only):
  `capture_launch_identity`; `match_end` separates launch/closeout
  identities and writes `manifest.json` (per-kind counts + byte length +
  sha256 over the sealed records — payload changes after closeout fail
  validation even with untouched seq numbers; documented as proving
  properties of supplied records only). `phase_spectate` touched ONLY
  for the `outcome` summary key + the identity-capture call (CAP-01 owns
  its loop body).
- **Cross-lane fix (CAP-01's file, blocking defect)**: `fake_tuner_server`
  `reset_board` now seeds seats a spectate config observes beyond the
  2-major mini engine — the 7-seat live config (shipped in `5d5221b`
  without re-running the rehearsal) crashed every fake spectate run
  with `KeyError: 2..6`. Default (non-spectate) fixtures unchanged.
  Also re-pinned the rehearsal's `observed_players` assertion to the
  committed config.

## Capture-gap semantics discovered while validating the real run

A round whose SNAPSHOT says turn 20 but END says turn 21 is NOT
corruption: it is the honest fingerprint of a ring-wrap gap — turn 21's
START was lost, so turn 21's DEACT closed turn 20's round, and one
interval genuinely covers two engine turns. The structural core keeps
only START↔SNAPSHOT agreement; START→END spans are surfaced by the
validator as `capture-gap signature` eligibility notes. First observed
at round 6 (turns 20→21) of the real run.

## Follow-up probes

- `capture_complete_for_declared_scope` is None today; once CAP-01's
  coverage matrix lands, wire it to the declared scope vs captured
  fields.
- The manifest currently hashes `events.jsonl` only; extending it over
  `summary.json` + the wire transcript would harden the whole run dir
  (needs a schema bump).
- Outcome classes for the HOTSEAT validator are not introduced here
  (its outcome surface is the 100-round reliability lane's domain).

## Open decisions (conservative defaults chosen)

- Legacy `identity` key aliases LAUNCH identity (not closeout) — chosen
  because every existing consumer (validators, dashboards) treats it as
  "what ran"; recorded here in case a consumer actually wanted closeout.
- `classify_outcome` maps an ambiguous legacy shape (clean=False, no
  failure recorded) to `completed` so the full profile's clean check
  fails it honestly rather than silently tolerating an open prefix.

## Evidence

`runs/cap02-evidence-20260907/`: focused + affected pytest logs, the
full-gate log, ruff log, and the lane summary. Never any wire payloads.
