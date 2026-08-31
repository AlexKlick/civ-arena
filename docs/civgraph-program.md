# CivGraph — the planning-plane research program (M15+)

Adopted 2026-08-30 from the operator's CivGraph proposal: a graph-authoritative
hierarchical planning system in which typed graphs represent world and belief
state, an action graph enumerates legal causally-ordered operations, a policy
graph carries multi-turn strategy, Monte Carlo **graph** search evaluates
options, and the LLM proposes but never executes. This document is the
proposal **adapted to this repository**: what already exists (with paths),
what is greenfield, the decisions that bend the proposal to the repo's
disciplines, and the full spec of the first buildable milestone (M15).

Scoping decisions, made with the operator 2026-08-30:

- **Deliver sim-first.** The research question runs on the simulator; the
  live Civ VI leg is a later evaluation environment (M17), not the first
  build target.
- **Sequence after M14.** The live lane (dispatch, wedge, tourney) closes
  first; CivGraph is M15+.
- **No learned models in the first cycle.** Hand-crafted value + exact sim
  rollouts validate the graph/option/search architecture before any
  training spend.

## 1. The proposal mapped onto what is already built

The proposal's central governance claim — *the LLM never has final authority
over legality or execution* — is not a proposed change. It is this repo's
core thesis, already enforced in depth:

| Proposal pillar | Repo status |
|---|---|
| Guarded executor, LLM-no-authority | **Built.** Three redundant legality layers (`session/legality.py` schema/whitelist/ownership → `game/sim/rules.py::check_action` domain rules → `simulator.act` re-check), the referee's authorization ledger + watchdog multiset diff (`arena/referee.py`, `arena/watchdog.py`), and structural identity binding — `ToolFacade` closure-binds the session ctx and **no tool takes a `player_id`** (`session/player_session.py`). |
| World/belief graph | **Substrate built; uncertainty layer greenfield.** M11 gives typed, temporally-versioned, id-stable claims (`strategy/claims.py`), last-known foreign beliefs (`strategy/beliefs.py`), and own-state facts (`strategy/facts.py`), all rebuildable from the log. Nothing carries probability, evidence links to observations, or expiry — see §3. |
| Experience graph | **Built twice, disconnected.** The M12 Neo4j `cross_match` spine (`graph/`) is a *human* query surface; the thing the agent actually reads across matches is the M13 `RecallCorpus` (`recall.py`) — lexical, in-memory, and it never touches the graph. Unifying them is future work, not M15. |
| Rollout engine | **Effectively built.** `game/sim/rules.py` + `game/sim/engine.py` are pure, integer-deterministic functions over a plain dict (~16 KB state; a fork is one `deepcopy`). `scripts/playout_hash.py` (79 lines) is a working rollout driver today. |
| Action DAG, options, search | **Greenfield.** Zero hits repo-wide for mcts / monte carlo / planner / option / topological / dependency-graph machinery. This is what M15 builds. |

Empirical caveats the program inherits and must not paper over:

- The corpus is thin: five completed sim LLM matches; recall's live effect is
  n=1 (run 005).
- The `Outcome`/`VERDICT` layer of the M12 graph is **empty in every real
  run** — models self-assess via lessons rather than authoring claims that
  come due at the projection horizon. A calibration story cannot hang on it
  yet.
- `confidence` (int 0..100 on goals/predictions) is model-self-reported at
  authorship, never updated, and **read by no scoring or retrieval code**.
  It projects onto graph nodes as a dead property.

## 2. Adaptation decisions

### 2.1 Sim-first, and what that costs

The sim implements movement/combat/cities/production/research/visibility
(`docs/design-notes.md`) but has **no victory conditions, no city capture,
no diplomacy**; matches end at `max_turns`. The declared objective for the
research cycle is therefore **score differential at a fixed horizon**, using
exactly the components `Arena._scores()` already reports
(`arena/coordinator.py:376`): cities, population, gold, techs, units. This
is a limitation, declared up front: results transfer to full Civ VI as an
architecture validation, not as a strength claim. The live leg (M17)
re-poses the question on the real engine.

### 2.2 Fair rollouts under fog — the load-bearing decision

A planner runtime sits behind the `AgentRuntime` seam and structurally
cannot reach ground truth: the facade exposes 20 tools and nothing else.
Forking the true `SimState` for rollouts would void the arena's entire
fairness thesis and falsify every test in `tests/test_visibility.py`.

**Decision: the planner never forks ground truth.** It builds a
belief-`SimState` from its own projected observation stream only — own
entities full-field, foreign entities restricted to the projection
allowlists (`FOREIGN_UNIT_FIELDS` / `FOREIGN_CITY_FIELDS`,
`arena/visibility.py:30-33`), unknown tiles and unobserved foreign state
filled by seeded determinization. Rollouts run on that reconstruction.
This is the honest path, and it is the thing that actually motivates a
belief graph: the belief state IS the planner's world model input.

### 2.3 Determinism and replay discipline

Everything the planner produces must obey the repo's standing rules:

- **No floats.** Canonical JSON rejects them (`canonical.py`). Any
  probability-like quantity is a fixed-point int (0..10000) if it is ever
  logged or hashed.
- **Seeded RNG only**, threaded through `runtime.rng` so checkpoints carry
  it (`coordinator._checkpoint_state` serializes every runtime's rng).
- **Search is a side artifact, not an event.** Planner internals consume no
  referee calls and emit no events, so model-free replay survives a planner
  by construction — and the search is invisible in the log. Auditability
  comes from a side artifact directory `runs/<id>/planner/` (the
  `spend.jsonl` precedent: beside the log, never inside it). Replay does
  not regenerate it and does not compare it; the event log remains the
  sole trust root.

### 2.4 Zero arena changes

The planner is one new runtime behind the unchanged tool surface:
registration in `agents/runtime.py::build_runtime` (line 41) plus
`VALID_POLICIES` (`config.py:128`), nothing else. The M11–M14 "zero changes
under `arena/`, `session/`" diff-proof discipline applies to every M15
commit. Because both the sim `Arena` and the live driver construct runtimes
through the same `build_runtime`, a proven planner ports to the live leg
without arena work.

### 2.5 Hot path in memory; Neo4j stays post-hoc

The proposal's §16 split (in-memory typed graphs for search; a graph store
for cross-game analysis) is already the shipped architecture — and the
shipped version is stricter: the match loop never touches Neo4j at all.
Preserve that. The M15 search graph is plain Python objects; the M12
projection remains a pure function of a finished log.

**Ops hazard, recorded here:** mem0's documented graph store URL is the same
`bolt://127.0.0.1:7687` this repo's `civ-arena-neo4j` container serves, with
the same default database, and `python -m civ_arena.graph.query stats` runs
an unscoped `MATCH (n)`. Before anyone flips `MEM0_ENABLE_GRAPH=true`, give
one side a distinct database or port.

## 3. What the belief layer is missing (deliberately deferred past M15)

The proposal wants probability / evidence / expiry on uncertain nodes and
edges. Two pinned repo invariants stand in the way, and both deserve an
argued change, not a drive-by:

1. **Canonical JSON bans floats** — probabilities must be fixed-point ints
   or the canonicalizer contract changes.
2. **Beliefs never expire, on purpose** (`strategy/beliefs.py` docstring:
   an unobserved death must not erase the last known position; staleness is
   a render-time label). Any decay/expiry proposal must argue against the
   pinned tests.

M15 sidesteps both: determinization handles uncertainty inside the search
(sampled worlds, seeded), and nothing probabilistic is persisted. A typed
uncertainty layer (evidence-linked hypothesis claims, calibrated
confidence) is M16+ material, and should land only with a consumer — a
scoring or search component that actually reads it, unlike `confidence`
today.

## 4. Milestone ladder

| Milestone | Contents | Gate to enter |
|---|---|---|
| **M15** (specified below) | Foundations, belief forward model, within-turn action DAG + executor, options, MCTS-vs-MCGS comparison — all sim-side | M14 closed |
| **M16** | LLM as proposer over the option library (candidate options + contingencies as untrusted proposals, compiled and validated before search); option discovery from recorded trajectories; typed uncertainty layer with a real consumer | M15 comparison run |
| **M17** | Live-leg planner port + the zero-touch match harness (autonomous launch, save-load, modal handling, screenshot triage — the 2026-08-30 autonomy assessment; proposal Gate 1 reliability) | M15; M14 live lane learnings |
| **M18+** | Learned value/dynamics (graph encoder, expert iteration), league training, human evaluation — the proposal's Phases C–G and Gates 3–5 | Deferred; not planned in detail here |

Everything in M18+ is explicitly out of scope for planning today: it
depends on results, hardware budget (GPU 0 is text-main's), and whether the
M15 comparison validates the architecture at all.

## 5. M15 — the first research build (full spec)

The proposal's own "correct first build": *a typed within-turn action DAG, a
small multi-turn option-policy graph, and a controlled comparison of
option-level MCTS against transposition-aware Monte Carlo graph search* —
adapted sim-side. Four sub-milestones, each independently gated.

### M15a — foundations

1. **Fix the snapshot-aliasing bug.** `SimState.from_doc` is
   `cls(dict(doc))` — a shallow top-level copy (`game/sim/state.py:114-115`)
   — so `restore(snap)` aliases every nested unit/city/tile dict of the
   snapshot; later mutation corrupts it. Already reachable in-tree:
   `Referee.begin_turn`'s rollback loop can restore twice from one
   snapshot. Fatal for any restore-N-times search loop. Fix: deep-copy in
   `from_doc` (or `restore`); pin with a regression test that mutates state
   after restore and re-restores.
2. **`legal_actions(state, pid)`** — a pure enumerator at the rules layer
   beside `check_action` (`game/sim/rules.py`). Enumerates per-unit moves
   within the movement budget, attacks in range, fortify, found-city,
   research/production/purchase choices. Property tests in both directions:
   *soundness* — every enumerated action passes `check_action`;
   *completeness* — randomly sampled actions that pass `check_action` are
   in the enumeration. Branching is small per decision (≤6 reachable
   destinations per movement point, ≤8 techs, ≤8 production items); the
   combinatorial explosion across units is the DAG's problem (M15c), not
   the enumerator's.
3. **`value_of(state, pid)`** — the hand-crafted value head, extracted from
   `Arena._scores()` logic into a pure rules-layer function returning an
   integer score vector (cities, population, gold, techs, units) and a
   scalarization used by search. No learned component.

### M15b — the belief forward model

`BeliefState.from_projections(...)`: construct a partial `SimState` from
the player's own observation docs — own entities full-field, foreign
entities only the allowlisted fields while observed plus last-seen
carryover (the `BeliefStore` shape), remembered tiles terrain-only,
unrevealed tiles unknown. A seeded **determinizer** samples complete
`SimState`s from it (unknown terrain from the map generator's
distribution, unobserved foreign forces from simple priors) for rollouts.

**Fairness is pinned the way visibility already is**: leak-checker-style
tests (the `arena/visibility.py` checker pattern) assert that no
constructed belief state, and no search decision, ever depends on a field
outside the projection allowlists. This is the test surface that makes
"the planner never sees ground truth" a property, not a promise.

### M15c — within-turn action DAG + guarded executor

A turn is a partially ordered set of actions, most of which commute.

- **Nodes**: the enumerated legal actions for the current turn.
- **Edges**, derived from the rules — the `RejectionReason` taxonomy
  (`game/adapter.py:87`) is the edge vocabulary: `CONSUMES` (movement
  points, gold — `NO_MOVEMENT` / `INSUFFICIENT_GOLD`), `CONFLICTS_WITH` /
  `MUTEX` (destination occupancy — `OCCUPIED`; one production choice per
  city — `ALREADY`), `MUST_PRECEDE` (move-then-attack range enablement —
  `OUT_OF_RANGE`), `COMMUTES_WITH` (independent cities' production, distant
  units' moves).
- **Execution**: one canonical topological ordering of the chosen actions
  (partial-order reduction — permutations of commuting actions are one
  plan, not many), issued through the **unchanged** 20-tool facade, with
  revalidation after every accepted action: re-observe, re-derive the
  affected DAG region, and replan if an edge assumption broke. The
  three-layer legality stack stays authoritative; the DAG is a planner
  convenience, never a bypass.
- **Experiment-1 measurement** (proposal §12.1): DAG executor vs the
  existing LLM agent on matched configs — invalid/rejected call rate,
  stale-state actions, turns completed without a forced phase close,
  tool calls per turn.

### M15d — options, and MCTS vs MCGS

- **Option library**: ~10 hand-authored options at sim vocabulary, the
  proposal's schema trimmed to what the sim can express — e.g.
  `expand_to_k_cities`, `walls_first_defense`, `early_warrior_rush`,
  `archery_timing`, `tech_race(target)`, `growth_economy`,
  `fortify_line(region)`, `scout_corridor`. Each is executable data:
  typed initiation / termination / interrupt predicates over the belief
  state, a subgoal list compiled to per-turn action DAGs, and declared
  fallbacks. Options are the search's edges; primitive actions are never
  searched directly above the within-turn layer.
- **`PlannerRuntime`**: policy `"planner"` in
  `agents/runtime.py::build_runtime` + `VALID_POLICIES` (`config.py:128`).
  Per turn: rebuild belief state from observations, evaluate the active
  option's termination/interrupt predicates, search if a decision is due,
  compile the chosen option's current step to an action DAG, execute
  through the facade. Implements `take_turn` / `.rng` / `begin_turn` /
  optional `bind_services` per the existing runtime contract
  (`session/player_session.py`).
- **Search, two implementations behind one interface**:
  1. option-level **MCTS** (tree, PUCT-style selection, progressive
     widening over option parameters) — the baseline;
  2. **MCGS** — transposition-aware graph search with **exact integer
     equivalence keys** (canonical serialization of the horizon-abstracted
     belief state; the state is all-integer, so exact keying is cheap and
     there is no embedding-similarity merging at sim scale — the
     proposal's "dangerous merge" problem is dodged entirely).
  Both use the same determinized rollouts (M15b), the same value head
  (M15a), the same seeded budget.
- **Experiment-3 comparison** (proposal §12.3): equal simulation and
  wall-clock budgets, paired seeds, sides swapped — MCGS vs MCTS vs the
  scripted doctrines vs the LLM agent. Metrics: final score differential,
  node expansions, unique states evaluated, transposition reuse rate,
  decision quality at fixed budget. **H3**: the sim's convergent and
  commuting action sequences make graph search beat tree search at equal
  compute. A null result here is a real result — it gates how much MCGS
  machinery M16+ carries.
- **Audit artifact**: `runs/<id>/planner/` — per-decision search trace
  (root belief-state key, candidate options, visit counts, chosen option,
  seed), canonical JSON, integers only. Beside the log, never in it.

### M15 gates (repo discipline, per sub-milestone)

- Full local pytest + ruff green (`uv run pytest`, teed to a log, counts
  reported from the log).
- Zero diff under `arena/` and `session/` except the two registration
  lines (proven by diff, the M14 pattern).
- A planner-vs-turtler sim match completes with 0 watchdog violations and
  **REPLAY OK** (model-free replay is indifferent to the planner by
  construction — prove it anyway).
- Fairness leak-checker suite green (M15b onward).
- One bounded Codex review round per sub-milestone, findings fixed and
  pinned before the next begins.

## 6. Explicitly deferred (recorded so they are decisions, not omissions)

- LLM in the loop (proposer role) — M16; the first cycle establishes the
  substrate the proposer would feed.
- Uncertainty as persisted data (probabilities, evidence edges, expiry) —
  M16+, only with a reading consumer, and only through the canonical-JSON
  and belief-invariant arguments (§3).
- Recall/graph unification (agent-readable experience graph) — after the
  verdict layer is non-empty in real runs.
- Live Civ VI evaluation, the zero-touch harness, and everything the
  autonomy assessment enumerated — M17.
- Learned value/dynamics models, expert iteration, league training, human
  evaluation, and every world-class gate past reliability — M18+.

## 7. Experiment record

### 2026-08-31 — Experiment-3 EXECUTED: H3 rejected at sim scale; MCGS dropped from M16

`scripts/planner_experiment.py` (paired-seed harness, both sides per
method per seed, scripted baselines) at budgets 8/16/32 — 360 matches
total, all 40 turns, **0 watchdog violations and 0 referee rejections
across every planner match**, dirty pairs 0. Paired analysis =
exact sign test over same-seed/same-side pairs where the search method
is the only difference (`scripts/planner_analyze.py`; artifacts under
`runs/exp3/b{8,16,32}/`, git-ignored like all runs — this section and
the scripts are the durable record).

| budget | mcgs/mcts/ties | mean / median diff | one-sided p (H3) | verdict |
|---|---|---|---|---|
| 8 | 13/27/0 | −134 / −74 | 0.008 (**mcts**) | MCGS harmful |
| 16 | 12/28/0 | −161 / −92 | 0.003 (**mcts**) | MCGS harmful |
| 32 | 21/19/0 | −28 / +12 | 0.44 | null-uncertain |

Both low-budget reversals survive a 3-way Bonferroni; the medians show
they are not outlier-driven. Dominance context: both planner arms
average +495..+522 vs the turtler where the expansionist baseline is
−9 — **the option/DAG/belief substrate is the value carrier, not the
search-graph layer**.

Mechanism (independent glm-5.3-flash audit, claims re-verified against
the raw data): depth-2 state-keyed node sharing averages value
estimates across different determinized WORLDS — biased estimates at
low visit counts, diluting as budget grows. The audit pre-registered
this toxicity before the b16 leg contradicted the b32 null. Supporting
evidence, all three legs: mcgs-LOST pairs carry more transposition
merges than mcgs-won pairs (69v50, 64v57, 48v39); harm is monotone in
budget (worst at 8, gone by 32); at b8 the harm is side-concentrated
(mcgs 12/20 as player 0 but 1/19 as player 1). Secondary verified
findings: MCGS shows a lower-variance profile (b32: wins avg +116 vs
losses −187, blowout tails 2v4, SD 118v209 — shrunken estimates,
conservative play); the methods diverge early (median decision ~2.5 of
14–18 per match), so post-divergence pairs are near-independent
trajectories and paired differentials carry chaos noise.

Audit corrections adopted: the b32 "null" is restated as
null-uncertain (≈10× underpowered for any plausible effect; the
turtler is a saturated opponent that cannot discriminate search
quality); the earlier "merges did not correlate with wins" reading is
withdrawn (it weakly correlates with losing).

**Gate decision for M16**: the graph-search layer is NOT carried
forward. Sim-scale default = MCTS over options (or search-free option
compilation). Graph search may return only with: search depth ≥ 3;
per-world transposition keys (merging never crosses determinizations —
which at depth 2 degenerates to MCTS, i.e. this finding); a
discriminative opponent (direct planner-vs-planner self-play arm — the
turtler saturates); per-decision merge-dispersion instrumentation; and
budgets ≥ 128. The LLM-proposer lane (M16) proceeds on the substrate.

### 2026-08-31 — M16 BUILT: proposer, uncertainty, journal, mining (heads c3674db → 2016f7e)

All four sub-milestones landed, each with a closed Codex round:

- **M16a — resume without amnesia** (`c3674db` + fixes `456a27c`): the
  `PlannerJournal` side artifact carries END-of-turn belief snapshots
  (turn-start observations were unsound — the executor's mid-turn
  reconciliations resurrected killed targets on resume); validation is
  eager at construction (corrupt journals refuse before the resumed
  Arena can truncate the authoritative log); restore is rewind-aware.
  With no live proposer, a resumed match is BIT-IDENTICAL to its
  uninterrupted twin (teeth proven red-without-restore at 40 turns —
  shorter horizons were vacuous because mid-game plans read no fog
  memory).
- **M16b — the LLM proposer** (`7c22565` + fixes `2016f7e`): untrusted
  JSON ranking compiled to a LEGAL, fail-soft prior (legality-
  narrowing: the model can only permute the initiation-true candidate
  set). Consumer = first-visit ordering under untried-first (budget <
  candidates ⇒ the proposal literally chooses what gets explored; ties
  among evaluated candidates resolve canonically). `proposer:` LLMSpec
  on policy `planner` only; spend capped runtime-side and JOURNALED;
  malformed replies degrade. DECLARED LIMIT: a live proposer is
  replay-safe but not resume-identical (the model is re-asked at
  genuinely-new decisions only).
- **M16c — typed uncertainty with real consumers** (`6ad3d8f` + fixes
  `2016f7e`): `staleness_confidence` (int 0..10000) stamped on
  determinized foreign units; the no-expiry store invariant HOLDS (the
  ghost materializes; only confidence decays). Consumers: rush unit
  targets and defend threats gate at 5000/4000; setup-prior units
  decay like turn-1 sightings (ignorance never out-confidence
  observation); rollouts age confidences 2500/turn so search values
  only continuations the runtime can execute; rush termination is
  symmetric (units-only, confidence-gated).
- **M16d — option mining** (`ecf5b4f`): `scripts/option_mining.py`
  over the Experiment-3 corpus (240 matches): rush last-active mean
  diff +556 (n=139) — the strongest closer; tech_race the dominant
  default (1646 held spans); defend wins 7 of 3315 candidacies (dead
  weight vs the turtler). Promote/demote compiler tuning is the
  follow-on, evidence now on disk.

Gates across the day: 426 → 431 → 436 → 439 → 444 passed (+1 skip),
the same 2 pre-existing out-of-lane failures throughout. Codex rounds
found real defects every time (2+1, 3+1+2) — the standing pattern
since M15a.
