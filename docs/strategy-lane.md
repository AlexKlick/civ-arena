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
- Caps: 32 goals (counting UNDROPPED — dropping one frees its slot, ids are
  never reused, and REACTIVATING a dropped goal re-consumes a slot) /
  64 predictions / 64 lessons per player.
- Scoring contract (M11): single-direction — a metric claim is `met` when
  the observed value ≥ target, evaluated AS OF the claim's deadline (later
  observations never flip a verdict retroactively, and the DISPLAYED value
  is fetched at the same deadline); "at most" phrasing is text, scored
  `self_assess`. A metric goal with NO deadline is never due and therefore
  always self-assessed. `subject_id` is a label, not a scoring veto — the
  metric always measures the player's own state. Metric-less claims are
  `self_assess`. Overdue goals stay due until amended or closed; a due
  prediction's review is closed by recording a lesson `about` it (the
  instructed verdict flow), and re-opened by amending it. Review renders at
  most 6 items; overflow due goals stay visible in GOALS.

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
  `from_log` requires a typed namespace on BOTH records of a claim pair
  (adjacent seqs, private scope, bools are not ints) — equal-None identity
  pairs authorize nothing;
- claim writes never touch game state; oversized claims AND oversized
  reference fields die at the LLM runtime with zero events (replay
  preservation);
- belief fields ⊆ projection allowlists; own cities classify as own
  (owner-key normalization, conflicting keys fail loudly); hidden entities
  stay absent;
- prompts stay identity-free — the template pin constructs through
  `render_memory`;
- resume-equals-uninterrupted for the memory view (byte-identical turn-3
  header after crash-resume — the pin that rules out snapshot-approximation
  beliefs);
- arena-owned services reach injected runtimes through the explicit
  `bind_services` opt-in hook (construction and resume), never by probing
  attributes a runtime happens to expose.

## Graphiti projection — shipped as M12 (`docs/graph-lane.md`)

The sketch below landed, refined in one place: claim nodes are
per-**revision** (`f"{match_id}:main:claim:p{player_id}:{claim_id}:r{rev}"`)
so SUPERSEDES connects distinct nodes, and an agent/match spine
(`cross_match` group, `agent:{agent_id}` uuids stable across matches) makes
retrieval cross-game. Artifacts under `runs/<id>/graph/` are the portable
truth; the Neo4j instance is a derived, rebuildable index. The full contract,
loader, runbook, and live validation record: `docs/graph-lane.md`.

## Live validation

### 2026-08-29 — M11 complete (review rounds 1–5 converged; 11→8→5→3→3)

`configs/llm-vs-turtler-002.yaml` — the same duel as the M10 baseline
(`llm-vs-turtler-001`: same seed 947381, same MiniMax-M3 vs the scripted
turtler), with the typed memory tools in the model's hands. A 2-turn smoke
gated the paid run at the final HEAD (0 violations, REPLAY OK 138 events).

| Run | Result |
|---|---|
| `llm-vs-turtler-002` (40 turns) | finished, **0 violations**; tokens 550,242 in / 151,466 out (+34% input vs baseline — the memory view's cost); 979 LLM tool calls, 9 model-side malformed-args (self-corrected); 395 counted posts of the 2000 budget; final score ROME **4 cities / 21 pop / 509 gold** / 24 units / 8 techs vs KOREA 2 / 15 / 81 / 20 / 8 — where the M10 baseline played the turtler to a 2-city draw with 86 gold, the memory-enabled run doubled the empire and banked 6× the gold |
| replay of the 40-turn run | **REPLAY OK: 2,884 comparable events identical**, zero network — claims and observation digests in the log are replay-invisible by construction |

Claim adoption was immediate and sustained: 45 `set_goal` (9 goals, 26
amendments — g1 ran 11 revisions before closing itself as "DONE: Founded
4th city Florentia at (-1,2) on t23"), 10 predictions (one closed by its
lesson, two pending past the horizon), 20 lessons carrying explicit
verdicts (8 MET / 1 MISS), 34 `get_strategy` reads, and the diary relaxed
to 30 free-form notes. The belief layer ended with 16 last-known foreign
entities — including the enemy city c3 and three successive respawned
defenders tracked by id across their deaths, exactly the opponent picture
the 2000-char diary could not hold. One lesson is pure engine discovery:
"military units CAN stack on same tile in this engine (confirmed)".

Honest caveats: the model self-assesses most verdicts through lessons
rather than metric-scored goals (g9 — "march on c2, cities>=5 by t50" —
is the only metric-bearing goal still open at match end, so nothing was
arena-scored on the final turn); the +34% input tokens is above the
predicted 10–25%; and the production-gap heuristic is blind on the
baseline (pre-M11 logs carry no digests). Rejections remained fog-of-war
noise (move_unit), with a handful of self-corrected claim rejections.
