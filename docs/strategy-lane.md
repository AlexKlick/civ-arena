# Strategy lane — the cognition plane (M11)

The diary was the smallest memory rung. The strategy plane is the next: a
typed, temporal, replay-safe record of what the agent is trying to do, what
it expects, and what actually happened — beside the exact game state, never
inside it.

## The three slices (one store, one trust root)

`StrategyStore` (`strategy/store.py`) holds:

| Slice | Source | Rebuild |
|---|---|---|
| **Claims** — goals, predictions, lessons | model-authored via `set_goal` / `record_prediction` / `record_lesson` (validated non-actions, the `write_diary` shape) | adjacency-paired accepted TOOL_CALLs, namespace-identity-checked |
| **Beliefs** — last-known foreign entities | auto-fed from observation projections (LRU 32/kind/player, **no expiry**: an unobserved death must not erase the last known position) | the `observed` digest riding on observation TOOL_RESULTs |
| **Facts** — own-state samples | observation digests only (**never AMBIENT** — referee-scope records carry the opponent's economy) | same digests |

Verdicts and review lists are **derived at render time, never stored** —
`scoring.py` recomputes "due this turn" from `(claims, facts, turn)`, so
resume/replay need no extra story for them. The store sits deliberately
OUTSIDE the checkpoint content hash (the diary argument verbatim): the
event log is its only durable store, and `StrategyStore.from_log` rebuilds
over the truncated prefix exactly like `DiaryStore.from_log`.

## Claims

- `Goal`: text, `by_turn` (0 = none; ≥ authorship turn), `metric` ∈
  {cities, units, techs, gold, population} + `target`, `status` ∈
  {active, done, dropped}, `confidence` 0..100 (int — canonical JSON
  rejects floats; turns are the time axis, no wall-clock).
- `Prediction`: text, `review_turn` ≥ authorship turn, optional
  `subject_id` (entity/claim id ≤ 16 chars), metric/target/confidence.
- `Lesson`: text, optional `about` (own goal/prediction id).
- Amendment is **id-stable**: revising g2 appends revision 2 of g2 and
  closes revision 1 (`valid_to_turn = amend_turn - 1`, clamped), so a
  prediction's `subject_id` and a lesson's `about` survive amendments.
- Caps: 32 goals (counting UNDROPPED — dropping one frees its slot; ids are
  never reused) / 64 predictions / 64 lessons per player.
- Scoring contract (M11): single-direction — a metric claim is `met` when
  the observed value ≥ target, evaluated AS OF the claim's deadline (later
  observations never flip a verdict retroactively); "at most" phrasing is
  text, scored `self_assess`. `subject_id` is a label, not a scoring veto —
  the metric always measures the player's own state. Metric-less claims are
  `self_assess`. Overdue goals stay due until amended or closed; a due
  prediction's review is closed by recording a lesson `about` it (the
  instructed verdict flow), and re-opened by amending it.

## Observation digests — how beliefs became rebuildable

Observation payloads are NOT logged (only receipts). M11b adds one
additive `observed` field on accepted `get_units` / `get_cities` /
`get_overview` TOOL_RESULTs: a compact digest of the PROJECTED doc —
foreign lists field-for-field within `FOREIGN_UNIT_FIELDS` /
`FOREIGN_CITY_FIELDS`, never more than the player saw (own-city
projections carry `owner` while foreign ones carry `owner_id`; the split
normalizes both). The digest is canonically validated at build time, so a
non-canonical value fails before any record is written. `replay._strip`
ignores additive fields, so model-free replay cannot diverge on it; the
log already carries referee-scope AMBIENT manifests, so this exposes
nothing new. Cost on the 40-turn reference run: ~+40–70 KB. `from_log`
accepts digests only from typed, privately-scoped result records.

## The memory view

`strategy/view.py::render_memory` — a pure function of
`(store, player_id, turn)`, identity-free and template-pure, injected as
the third input of `turn_header(turn, diary, memory)`. Budget 2,400 chars;
fixed drop order RESOLVED → LESSONS → LAST SEEN → GOALS(→3); REVIEW DUE is
never dropped, only item-truncated. `get_strategy` returns the same shapes
(`view_for`), so a mid-turn read can never disagree with the next header.

## Invariants (all test-pinned in `tests/test_strategy.py`)

- live store == `from_log(log)` / prefix-wise / after resume / after
  model-free replay; a REJECTED claim mutates nothing (no empty buckets);
  `from_log` requires a typed namespace — equal-None identity pairs
  authorize nothing;
- claim writes never touch game state; oversized claims AND oversized
  reference fields die at the LLM runtime with zero events (replay
  preservation);
- belief fields ⊆ projection allowlists; own cities classify as own
  (owner-key normalization); hidden entities stay absent;
- prompts stay identity-free — the template pin constructs through
  `render_memory`;
- resume-equals-uninterrupted for the memory view (byte-identical turn-3
  header after crash-resume — the pin that rules out snapshot-approximation
  beliefs);
- arena-owned services (diary, strategy store) reach injected runtimes too,
  not only built ones.

## Graphiti projection (M12) — shape only, no dependency, no infra

When cross-game retrieval justifies a graph DB, event-logged claims and
digests project deterministically (no LLM extraction — the claims are
already structured):

- `group_id = f"{match_id}:main"` (`timeline_id = "main"` everywhere in
  M11; the keys are branch-aware from day one);
- nodes: claims `f"{match_id}:main:claim:p{player_id}:{claim_id}"`,
  entities `f"{match_id}:main:entity:{entity_id}"` (sim entity ids are
  match-unique);
- edges: `AUTHORED` (player→claim), `SUPERSEDES` (revision n → n−1, from
  the history lists), `REFERENCES` (claim→entity via subject/about),
  `OBSERVED` (player→entity, effective turn = last_seen_turn), `VERDICT`
  (claim→derived outcome, recomputed).

## Live validation

(recorded in the LLM lane table format after the M11 validation runs)
