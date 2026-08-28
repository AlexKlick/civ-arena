# Design notes — the arena fairness model

The spike's job is to make these properties *measurably true*, not merely
intended. Each maps to tests in `tests/`.

## Information scopes

| Scope | Contents | Reachable by |
|---|---|---|
| `referee` | complete engine state | `Referee.referee_snapshot()` only; a session requesting it is logged as an unauthorized call |
| `public_match` | turn, civ names, alive flags | any session |
| `bilateral` | stub (diplomacy is post-spike) | any session |
| `private_player` | own entities full-field; foreign entities closed-field allowlists; hidden entities ABSENT, not null-masked | the bound session |

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

Rollback and replay: a rolled-back command gets a follow-up
`TOOL_RESULT(status=rejected, rejection=rollback)` record; replay re-runs
the whole match through the same referee machinery (same watchdog config),
so rollbacks reproduce deterministically.

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

## Event log

Append-only fsync'd JSONL, strictly increasing `seq` (the only ordering
authority), 8-tuple namespacing, before/after state hashes plus receipts
(authorized) vs mutations (actual) on every action. A torn trailing line
(from kill -9 mid-write) is physically dropped at construction — otherwise
the next append would concatenate onto it and corrupt the log mid-file.
Mid-file corruption refuses to load.

## Out of scope (post-spike backlog, by decision)

- Criterion 7 (plans/commitments recall) — memory system.
- LLM AgentRuntime implementations; `model:` in config parses but is
  ignored (round-trip tested).
- Diplomacy bus; `bilateral` scope is a stub.
- The live FireTuner leg itself — see `docs/live-validation.md`.
