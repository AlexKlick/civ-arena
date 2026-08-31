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
