# CAP integration — spectator-capture + CAP-01/02/03 on one head

Branch: `feat/cap-integration-20260907` (worktree
`/home/alexk/civ-arena-cap-integration-20260907`), based on the
spectator lane's `5d5221b` → CAP-03's `1a4a53f`, merging CAP-01
(`c8d5fad`) then CAP-02 (`fa7c278`). Packet CAR-CAPTURE-FIDELITY-002,
all three tickets.

## Merge record

- CAP-01 merged clean (its `phase_spectate` rewrite and CAP-03's
  dispatch-path sinks touch disjoint regions of `live_driver.py`).
- CAP-02 conflicted once — `phase_spectate`'s finally block: CAP-01's
  teardown-inside-census ordering vs CAP-02's outcome class. Resolution
  keeps both: outcome computed first, then teardown (so its lifecycle
  entry stays inside the census the summary reports), then the summary.
- The inherited stale-rehearsal failures (2 on CAP-01's and CAP-03's
  solo heads: the `5d5221b` 7-major config vs 2-major fake seeding)
  resolved BY the merge via CAP-02's assertion update + seat seeding.

## Gate (the lane authority — sibling gates invalidated by merges)

- Combined focused suites (spectate + cap01/02/03 + wire-log):
  **139 passed**.
- Full gate: **2045 passed, 1 skipped, exit 0** (847 s); ruff clean.
  Logs: `runs/cap-integration-evidence-20260907/`.

## What lands here (per-finding index)

- F-01 poll-time reads ≠ boundary state → interval semantics
  (`observed_at`/`source_cursor`/`boundary`/`state_at_boundary`) —
  CAP-01, docs/cap01-capture-fidelity.md.
- F-02 owner ≠ actor → window-provenance `actor_class`, `actor_id`
  null — CAP-01.
- F-03 declared census → `SpectateTransport` capability allowlist,
  measured `command_census` — CAP-01.
- F-04 single-seat sink signature → `_provider_request_audit` —
  CAP-03 (`22d98d0`).
- F-05 certificate overstates → shared `spectate_audit` core, truthful
  replay fields — CAP-02.
- F-06 identity drift → `capture_launch_identity` freeze +
  launch/closeout split — CAP-02.
- F-07 usage-or-0, no correlation → null-usage semantics,
  `logical_request_id`/`decision_id` joins — CAP-03.
- F-08 ring-wrap losses → 0.5 s polls, `capture_gap` audits,
  `gap_inferred` rounds, roster discovery — CAP-01.

## Open (not this branch)

- Independent Codex review of the integrated diff (running;
  findings fold in before any master-ward move).
- The whole stack still sits on the unmerged 115-commit burst
  (`fdeaae2`); master remains at `3074f1c` — the merge-the-burst
  ticket owns that reconciliation.
- Follow-on lanes queued: human-seat hotseat (`policy: human`),
  bounded case-memory experiment, publication lane.
