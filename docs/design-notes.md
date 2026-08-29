# Design notes — the arena fairness model

The spike's job is to make these properties *measurably true*, not merely
intended. Each maps to tests in `tests/`.

## Information scopes

| Scope | Contents | Reachable by |
|---|---|---|
| `referee` | complete engine state | `Referee.referee_snapshot()` only; a session requesting it is logged as an unauthorized call |
| `public_match` | turn, civ names, alive flags | any session |
| `bilateral` | stub (diplomacy is post-spike) | any session |
| `private_player` | own entities full-field; foreign entities closed-field allowlists; hidden entities ABSENT, not null-masked. Foreign cities appear only while their tile is CURRENTLY OBSERVED — a remembered tile must not reveal a city founded after you explored it, or live population/HP drift of an unwatched city (last-known snapshots are a post-spike refinement). Observations are DEEP copies: an agent mutating a returned entity never touches live state. | the bound session |

The projection allowlists live in `arena/visibility.py`. The no-leak
checker's expectations are hardcoded separately from the policy, and a
meta-test injects a deliberately leaky policy to prove the checker has
teeth (`test_leak_meta_catches_broken_policy`).

`player_id` is never an agent-facing parameter: tools take an opaque ctx,
and `test_no_tool_parameter_is_player_id` introspects the registry.

## Authorization model (the watchdog)

Authorization comes from the referee's OWN ledger:

- at `begin_phase`, the referee REQUESTS the ambient batch and receives its
  manifest — those mutations are authorized;
- accepted command receipts are authorized;
- everything else the journal shows is a violation.

The mutation's `origin` tag is diagnostic only — a chaos mutation labeled
`ambient` is still flagged (`test_lying_origin_tag_still_caught`). Ambient
classification is by **declaration at a phase boundary**, never by mutation
shape: an undeclared ambient-shaped tick is an `AMBIENT_DEVIATION` (flag,
don't abort — this is the seam that absorbs real engine surprises on the
live leg); anything else is `UNAUTHORIZED_ACTION`.

Three response tiers: pre-commit **reject** (never a watchdog event),
**flag-and-continue** (default), and **rollback** (capability-gated; the sim
has it, live Civ cannot — the same code path degrades to flag/abort).

Rollback granularity: begin-phase violations restore the PRE-phase snapshot
and re-run `begin_phase` (ambient re-applies on clean state; a consumed
chaos event does not re-fire). Command violations restore a PER-COMMAND
snapshot — an earlier accepted command in the same lease always survives,
and only the violating command's key is forgotten from the dedupe index.
End-phase violations (fired inside `adapter.end_phase`, after the lease) are
flag-and-count only: restoring there would undo a phase the engine already
closed. When the violation limit trips, the referee still rolls the current
command back before raising `MatchAborted`, and the coordinator closes any
open phase.

Rollback and replay: a rolled-back command gets a follow-up
`TOOL_RESULT(status=rejected, rejection=rollback)` record; replay re-runs
the whole match through the same referee machinery (same watchdog config),
so rollbacks reproduce deterministically. `DedupeIndex.from_log` skips keys
that were subsequently rolled back, so resume matches live forget-semantics.

## LLM-lane accounting semantics

- **Spend budget spans resume, including the crash window**: every counted
  model attempt appends one fsync'd line to `spend.jsonl` in the run dir
  (the coordinator wires the client's `on_post` hook to it). That ledger
  lives OUTSIDE the event log on purpose — resume truncates the log to the
  checkpoint prefix to roll back incomplete turns, and spend must survive
  the rewind (money spent is not game state). On resume the client's
  counter is restored monotonically as the max over the live counter, the
  checkpointed `llm_posts`, and the ledger count, so repeated crash-resume
  cannot resurrect budget.
- **Tokens/telemetry are cumulative across legs** (checkpointed snapshot
  folded back in), EXCEPT `total_ms`: durations are wall-clock envelope
  data and are stripped from checkpoints to preserve the
  same-seed-same-checkpoint determinism pin, so a resumed summary's
  `total_ms` covers the post-resume leg only.
- **Model-side protocol failures** (unknown tool name, malformed arguments,
  type mismatches) are counted in `telemetry.model_errors`, never in
  `tool_calls` and never as event records: nothing executed, and
  event-ifying them would diverge the model-free replay (a replayed match
  consults no model, so it produces none).
- An llm-policy match **refuses to resume** from a checkpoint that predates
  this accounting rather than silently resetting spend.

## LLM determinism

An LLM match **replays model-free by construction**: replay re-executes the
recorded `TOOL_CALL`s through `ReplayRuntime` and compares every
`after_state_hash`; the model is never consulted, so a replayed LLM match
needs no network and cannot drift. Crash-resume restores to the last
turn-boundary checkpoint and the interrupted turn *re-queries the model* —
fresh decisions, expected divergence from the aborted attempt's partial log
(which `truncate_to` already discards). The checkpoint content hash does
not cover the diary (see above) and no LLM runtime state besides the
dummy `rng` is checkpointed; per-turn conversation is discarded at
`end_turn`.

## Determinism

- Canonical JSON rejects floats outright (`TypeError`); the sim is
  all-integer arithmetic.
- One threaded `random.Random` per sim and per agent; no module-level RNG
  (a test greps for it); no wall clock inside sim or policies — `ts` and
  `duration_ms` live only in the event envelope.
- **Observation order is a contract**: units/cities are returned sorted by
  numeric entity id. This was learned the hard way — checkpoint JSON is
  `sort_keys=True`, so a restored state dict iterates lexicographically
  (`u1, u10, u11, u2…`), which silently reordered bot decisions after
  resume.
- `log_prefix_hash` strips `ts`, `duration_ms`, AND `game_instance_id`
  (process-lifetime label) so prefix identity holds across restarts.

## Idempotency

Default key = player + tool + TURN + canonical args: retry-safe inside the
failure window that matters, allows the same content next turn. Repeatable
within one turn (e.g. two purchases of the same item) requires an explicit
nonce key — a deliberate tradeoff documented in
`arena/idempotency.py`. The event log is the only durable store; the dedupe
index, resume, and replay all rebuild from it.

## The diary (LLM-lane memory, smallest rung)

`write_diary` is a validated tool (lease, then shape: str, 1–2000 chars
after strip) that stores one per-player note, last write wins within a turn.
It is NOT a game action: no adapter call, no MutationRecords, no
before/after state hash — a tampered diary cannot move a game outcome, only
what a model reads next turn. For exactly that reason the diary sits
DELIBERATELY OUTSIDE the checkpoint content hash: the event log is its only
durable store, and resume rebuilds it with `DiaryStore.from_log` over the
truncated prefix — the same trust root and the same rebuild pattern as the
dedupe index (no schema bump, no new event kinds). Replay re-issues recorded
`write_diary` calls through the normal facade path, so replayed diary state
is verified for free by the existing comparable-event projection.

## Event log

Append-only fsync'd JSONL, strictly increasing `seq` (the only ordering
authority), 8-tuple namespacing, before/after state hashes plus receipts
(authorized) vs mutations (actual) on every action. A torn trailing line
(from kill -9 mid-write) is physically dropped at construction — otherwise
the next append would concatenate onto it and corrupt the log mid-file.
Mid-file corruption refuses to load.

## The strategy plane (Criterion 7 — graduated in M11)

Plans/commitments recall is no longer out of scope: `docs/strategy-lane.md`
documents the typed strategy store (goals/predictions/lessons claims,
last-known-entity beliefs, observation-derived facts). It follows the
diary's rules exactly — validated non-actions, the event log as the only
durable store, `from_log` rebuild over the truncated prefix, deliberately
outside the checkpoint content hash — plus observation digests: an additive
`observed` field on observation TOOL_RESULTs that keeps beliefs/facts
rebuildable without logging payloads and without touching the replay
projection. Verdicts are derived at render time, never stored.

## Out of scope (post-spike backlog, by decision)

- LLM AgentRuntime implementations; `model:` in config parses but is
  ignored (round-trip tested).
- Diplomacy bus; `bilateral` scope is a stub.
- The live FireTuner leg itself — see `docs/live-validation.md`.
- Cross-game graph projection (Graphiti/Neo4j) — M12; the M11 schema is
  shaped for it (`docs/strategy-lane.md`).
