# CAP-03 — decision-linked telemetry and evidence-qualified dataset export

Lane: `feat/cap03-decision-telemetry-20260907` (worktree
`/home/alexk/civ-arena-cap03-telemetry-20260907`, based on the
spectator-capture lane's `5d5221b`). Packet: CAR-CAPTURE-FIDELITY-002
(CAP-03), findings F-04 and F-07.

## Resulting implementation

- **Slice 1 — F-04 (single-seat sink signature)**: `_provider_request_audit`
  module-level closure (the `(event, **payload)` shape) used by the
  single-seat dispatch call site; the pre-fix lambda passed
  `_strategic_audit`'s one-dict signature where the sinks invoke
  `(tag, **fields)` — first provider POST would have raised TypeError.
  Regression test drives the PRODUCTION callable end to end.
- **Logical request identity (F-07)**: attempt records carry
  `logical_request_id` (uuid per `_post_doc` — one id across retries),
  `request_set_key` (payload_hash:request_kind — same-payload-distinct-
  decision cases stay distinguishable), and the ambient `decision_id`.
  Usage extraction is presence-based: absent/uncoercible → `null`,
  explicit 0 stays 0.
- **Decision boundaries**: every strategic-controller turn assigns a
  `decision_id` onto the client BEFORE any provider call; an accepted
  directive gets a `directive_id` that quiet turns keep citing (standing
  directive). Each turn emits a `decision_boundary` HEARTBEAT audit
  {decision_id, directive_id, provider_requests, source} — quiet turns
  land with provider_requests: 0 and stay in the cost join. Legacy-path
  runtime turns assign one decision_id per turn.
- **Ledger identity + sink isolation**: `llm_costs.jsonl` lines carry
  decision_id/logical_request_id/request_set_key; the composed sinks
  guard every ledger write — a sink fault is audited
  (`ledger_write_failed`, exactly once per sink) and NEVER fails or
  retransmits the provider request. Key-rotation redaction pinned: each
  attempt redacts the key actually sent, end-to-end through WireLog.
- **Exporter v0** (`civ_arena.research.export_dataset`): two strictly
  separated sample classes. `controlled_decision` (decision/directive
  identity, cost joins keyed by decision_id, observation/action/receipt
  refs into events.jsonl — payloads never copied; pre-boundary and
  pre-ledger runs export nulls + explicit quality flags). 
  `spectator_interval` (owner-attributed OBSERVATION intervals;
  `actor_attribution: owner_only`; exact-action-imitation and reasoning
  explicitly ineligible). Deterministic sample bytes; manifest with
  source digests separate. Sibling runs/restarts share `split_group`
  (= match_id) while `game_instance_id` distinguishes executions.

## Real-data smoke (read-only, `runs/cap03-evidence-20260907/real-run-smoke.log`)

- Driven LLM run `minimax100-20260907T183425Z`: 38 controlled_decision
  samples, ref integrity 0 failures, flags `pre_boundary_run`,
  `pre_ledger_run`, `run_aborted` (costs honestly null — nothing
  fabricated).
- Live spectate pilot `live-spectator-human-001-20260907T203439Z`:
  66 spectator intervals — 65 closed, 1 open (the interrupted final
  turn), and **7 `turn_label_mismatch` intervals**: rounds whose DEACT
  was lost to the 64-entry ring wrap are closed by the NEXT turn's END.
  The exporter pairs intervals CHRONOLOGICALLY (never by turn-label
  equality) and carries both labels + the mismatch flag — a
  wrap-second-order shape discovered by running the exporter over the
  real corpus and now pinned by test.

## Open decisions / limitations (conservative choices)

- Legacy-path turns emit NO decision_boundary audit (only the strategic
  controller does); their attempt records still carry decision_id. The
  exporter treats boundary-less runs as `pre_boundary_run`.
- Cost joins are keyed by decision_id; pre-boundary LEDGER rows (none
  exist today — the first ledger run postdates boundaries) would need a
  timestamp-window join. Deferred until such a run exists.
- `outcome_horizon` is derived from summary final_turn — a label for
  offline joins; the schema deliberately has NO actor-input section, so
  no future/privileged field can masquerade as a feature.
- Manifest `exported_at` is outside samples; sample bytes are
  deterministic by test.
- The sim coordinator's own spend wiring (not the live sinks) does not
  yet have failure isolation — out of this lane's file ownership; noted
  for the integration pass.

## Evidence

`runs/cap03-evidence-20260907/`: per-step focused/affected logs, the
real-run smoke, full pytest + ruff logs (counts in the lane summary).
