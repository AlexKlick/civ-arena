# Strategy capability inventory and next experiment

Date: 2026-09-04. Status: source inventory and preregistration; no experiment
results or full-game capability claim. Reliability source inspected at
`5a8611ead13a71f50838fc530b6afb259bd3181e`; CAR-M1 source inspected read-only at
`c4a467fd551d719f16ec5200df97456c47c78700` in the separate `car-m1-v2` worktree.
These branches have not been integrated by this work. The coordinating
[kickoff record](strategy-program-kickoff.md) tracks execution and dependencies.

## Current capabilities

“Implemented” below describes source, not newly executed proof. The latest
[live record](live-validation.md#captured-results-and-handoff)
reports a startup failure before any live seat turn. Simulator source support,
fake-driver tests, and historical live observations do not establish current
full-game reliability.

| Capability | Simulator source | Live adapter source | Remaining boundary |
|---|---|---|---|
| Movement, unit combat, fortification, founding cities | Implemented in [`rules.py`](../src/civ_arena/game/sim/rules.py). `attack` resolves a unit target, not a city. | Seven gameplay action builders in [`firetuner.py`](../src/civ_arena/game/civ6/firetuner.py); [`lua_translator.attack`](../src/civ_arena/game/civ6/lua_translator.py) resolves a target unit. | City attack/capture is absent from the current agent action surface; domination cannot be inferred from unit victories. |
| Economy, production, research | Production, purchases, growth and eight technologies; five unit types and three buildings in [`state.py`](../src/civ_arena/game/sim/state.py). | Research/production/purchase translation exists. | The reduced simulator is not a model of Civ VI's full tech tree or economic systems. Broader engine availability is not agent coverage. |
| Culture, policies, religion, diplomacy and district placement | No corresponding gameplay actions in the simulator legal-action enumeration. | Civic and empty-policy-slot housekeeping exists in [`lua_translator.py`](../src/civ_arena/game/civ6/lua_translator.py), using first available choices. No agent tool chooses civics/policies, religious actions, diplomatic agreements or district placement. | Mandatory-choice recovery is not a strategic policy for these systems. Each new strategic action needs observation, legality, execution and receipt coverage. |
| Objective and game completion | [`value.py`](../src/civ_arena/game/sim/value.py) scores cities/population/gold/technologies/units. [`coordinator.py`](../src/civ_arena/arena/coordinator.py) terminates at the configured horizon or abort. | [`live_driver.py`](../src/civ_arena/game/civ6/live_driver.py) counts completed hotseat rounds; [`validate_run.py`](../src/civ_arena/game/civ6/validate_run.py) explicitly does not prove victory. | No first-class victory outcome is present in the shared [`GameAdapter`](../src/civ_arena/game/adapter.py) contract. Score advantage is not an engine victory. |
| Fog-limited planning | [`belief.py`](../src/civ_arena/planner/belief.py), [`uncertainty.py`](../src/civ_arena/planner/uncertainty.py), eight [`options`](../src/civ_arena/planner/options.py), two-epoch MCTS/MCGS [`search`](../src/civ_arena/planner/search.py) exist. Production planner defaults are MCTS, budget 12, three turns per epoch. | The planner is wired through the live runtime. | Rollouts still use the reduced simulator and hard-coded stock-AI opponent. Long-horizon strategic fidelity and current live strength remain unproven. |
| Learned priors, value and memory | Case base, bandit, learned value seam and strategy/recall services exist. | Availability of services does not prove beneficial live behavior. | Historical M19/M20 results do not justify enabling learned components by default. No new training is part of this kickoff. |
| Persistence and recovery | Adapter advertises rollback and save/load. | Adapter advertises neither rollback nor save/load. Bounded UI/turn recovery and startup save backups are implemented. | Backups preserve evidence; they do not implement automatic live save recovery. Interrupted scored episodes must not silently resume as clean matches. |
| Mutation authority | Reliability branch uses session/referee/watchdog authority and records the configured movement allowance. | Exact admitted own-unit movement rows are audited; foreign/out-of-policy drift remains disallowed. | Separate CAR-M1 V2 adds executor-only mutation, graph-bound revalidation and typed receipts. That contract is not silently supplied by legacy reliability tests. |

## Existing evidence and branch dependencies

The later sections of [the research record](civgraph-program.md) supersede its
early “greenfield” inventory. They report seating dominating planner mirror
outcomes, case and bandit priors worsening the scarce-budget result against
turtler, and a learned value head failing its improvement hypothesis. Those
are historical results with specific opponents, horizons and seeds.

Read-only artifact discovery found the original checkout's
[/home/alexk/documents/civ-arena/runs/league-m20a/league-results.json](/home/alexk/documents/civ-arena/runs/league-m20a/league-results.json),
[/home/alexk/documents/civ-arena/runs/league-m20a-probe/league-results.json](/home/alexk/documents/civ-arena/runs/league-m20a-probe/league-results.json),
[/home/alexk/documents/civ-arena/runs/exp19b/exp19b-results.json](/home/alexk/documents/civ-arena/runs/exp19b/exp19b-results.json), and
[/home/alexk/documents/civ-arena/runs/exp19b-b4/exp19b-results.json](/home/alexk/documents/civ-arena/runs/exp19b-b4/exp19b-results.json). Their underlying match collections were
not independently revalidated here. The retained
`/home/alexk/documents/civ-arena/runs/exp20c-prod/exp20c-results.json` hashes to
`6e6c7ea477960e68afaeb4bdfc81631201b70826c11629ab33bf5edb8880a23a`, matching the
research record. This byte check is not a re-execution or provenance audit.

The separate [CAR-M1 reconciliation](/home/alexk/worktrees/civ-arena-car-m1-v2-20260902/docs/car-m1-reconciliation.md)
and [validation report](/home/alexk/worktrees/civ-arena-car-m1-v2-20260902/docs/car-m1-v2-validation-report.md)
record source/repository completion, a provider pilot with 14/20 valid
proposals, and no completed actual Civ VI four-run gate. The research verdict
is `BLOCKED`. These are read report claims, not refreshed provider or live
measurements. The branch was clean when inspected.

Its frozen boundaries keep further milestone campaigns paused and prohibit
new learning/search/league work inside CAR-M1. Its
[observable contract](/home/alexk/worktrees/civ-arena-car-m1-v2-20260902/docs/car-m1-v2-contract.md)
requires all policies to propose through the transactional executor, makes the
V2 event ledger the authority, refuses unresolved mandatory choices, and
prohibits scored resume/branch imports. V1 evidence is read-only input to its
compatibility reader; it cannot become a resumed V2 episode.

The user's current parallel kickoff supports planning and bounded evaluation
infrastructure on the reliability branch. It does not itself establish a
passed CAR-M1 gate, authorize merging the branches, or make legacy diagnostics
V2 campaign evidence. Before the experiment below executes, record the selected
runtime/schema and integration decision, pass its authority checks, and close
or explicitly supersede the CAR-M1 campaign pause. Keep the two 30-round
reliability runs and CAR-M1's four V2 live runs as separately adjudicated gates.

## Preregistration: S-01 defensive opponent model

This is a future experiment. The separately built
[diagnostic pilot](../configs/strategy-benchmark-pilot.json) schedules existing
policies for 20 rounds with two development and two held-out seeds, at most
24 matches. It has no S-01 defensive-model treatment, and its outputs cannot
be presented as S-01 results.

**Question.** Does a small, declared improvement to the rollout opponent model
improve fixed-budget planning against turtler, without changing search priors
or fitting a value model?

**Source-grounded motivation.** [`search._simulate_epoch`](../src/civ_arena/planner/search.py)
uses [`stock_ai_turn`](../src/civ_arena/game/sim/engine.py), which moves units to
the first legal neighbor and queues warriors. Actual
[`scripted.run_policy`](../src/civ_arena/agents/scripted.py) turtler attacks in
range, fortifies idle military, researches and follows a defensive build
order. This mismatch is observable in source. Its contribution to prior harm
or the value-head null is a hypothesis, not an established causal explanation.

**One intervention.** Add a fixed defensive military model at the rollout
opponent seam. For simulated military units (warrior/spearman/archer), in
numeric unit-ID order, attempt the nearest visible in-range enemy (tie:
numeric target ID), at most two accepted attacks per opponent phase; otherwise
fortify if not already fortified. Use the simulated opponent's projected
visibility and the same legality checker. Keep stock-AI behavior for other
unit types and warrior production. No new research/purchase/settlement model,
learned parameters, real opponent-state inspection or opponent RNG access.
This is a partial defensive approximation, not an exact turtler emulator.

Both arms build the same projection so projection overhead is shared. The
intervention operates only on belief-derived simulated worlds; real mutations
still traverse the selected runtime's authority path. Add this explicit seam
and behavioral checks only after the campaign/runtime gate above is resolved.

| Frozen item | Specification |
|---|---|
| Baseline A | MCTS budget 16, two epochs of three simulated turns, UCT C=140, default value weights, canonical option order, existing stock-AI rollout opponent. Budget 16 is the experiment setting, not the runtime's default 12. |
| Treatment B | Identical to A except the defensive military rollout model specified above. |
| Disabled in both | LLM proposer, case prior, bandit, learned value weights, recall-derived ranking and adaptation across matches. No provider requests. |
| Evaluation opponent | Checked-in turtler, frozen source and configuration. Its identity is declared public experimental metadata. No claim of robustness to unknown opponents. |
| Horizon and seeds | Fresh 40-round simulator matches. Development seeds `910000003 + 7919*i`, i=0..3; held-out seeds with i=4..33. Verify collision-free against retained experiment manifests before freezing the actual manifest; a collision requires a preregistration revision before any run. |
| Pairing | For each map seed run A and B as p0 versus turtler, then A and B as p1 versus turtler: four fresh matches. Fix all per-seat RNG seeds identically between arms (p0=7, p1=22); report this remaining seat/RNG coupling. Balance A/B execution order by seed-index parity. Never compare a p0-only A batch with a p1-only B batch. |
| Resources | Exactly 16 rollouts per decision and the same six-turn rollout horizon. Both arms get the same action/replan ceilings and per-match wall-clock deadline. Capture observed decision counts, rollout counts, legality checks and elapsed time outside deterministic hashes. Any unequal configured limit invalidates the cluster. |
| Campaign ceiling | One development pass (16 matches) and one held-out pass (120 matches), at most 136 matches and three hours total on one worker. This is a proposed future ceiling, not permission to start now. Stop and preserve at a deadline; do not silently shrink the cohort. No extra seeds or retries after reading held-out outcomes. |

**Development gate.** Before exposing held-out outcomes, demonstrate the model
actually changes simulated opponent behavior in a targeted visible-contact
fixture, preserve baseline behavior when the seam is disabled, prove no
ground-truth observation input reaches the model, and replay complete matches.
Run the bounded paired benchmark diagnostic first to establish that pairing,
completion, validity, and seat-specific reporting work. Development is for
implementation correctness, not selecting among alternate model definitions.
Any behavioral revision gets a new preregistration and fresh held-out seeds.

**Primary endpoint.** For seed s and seat p, let d(s,p) be B's final default
score differential minus A's on that same map and seat. The cluster statistic
is D(s)=d(s,0)+d(s,1). All 30 held-out seeds, including D=0, enter descriptive
means and distributions. Conduct one predeclared exact one-sided sign test
over nonzero D, null P(D>0) <= 1/2. Report the zero count and both seats'
separate results; never treat 60 seat results as independent trials. The mean
per-match improvement is sum(D)/60. Binary game-score wins are secondary,
because a saturated opponent can hide decision-quality differences.

**Promotion rule.** Require all 30 complete, valid seed clusters; at least
20 nonzero clusters; exact one-sided p <= .05; mean per-match improvement >=
20 default-score points; nonnegative mean improvement in each seat; zero
authority violations; and no more than 25% increase in aggregate elapsed
compute under the identical deterministic rollout limits. This earns only a
larger, separately registered opponent-diversity evaluation. It does not change
the production default or establish live/full-game strength.

**Stopping and honesty.** Stop at the first implementation/authority/replay
failure, required-artifact write failure, exceeded ceiling or incomplete
match. Preserve every scheduled and attempted row with its reason; the
confirmatory result becomes incomplete. Never drop failed pairs and still
claim the preregistered denominator. If the complete cohort misses any
promotion criterion, retain the existing baseline and record “not promoted”;
a null is not proof that opponent modeling generally cannot help. Seat bias
that dominates both arms is an evaluation limitation, not evidence for B.

**Required artifacts.** Freeze source commits and clean-tree identity,
effective policy/opponent configs, schema/runtime, seed manifest and its hash,
preregistration hash, start-state identities, paired match inventory, event
logs and terminal summaries, validity/replay results, search traces, exact
rollout/resource accounting, and integer sufficient statistics for the sign
test. Keep wall-clock telemetry separate from deterministic content. Derived
summary statistics cannot replace source event evidence.

## Path to full-game capability

After reliable live play and the authority/runtime decision, choose one actual
Civ VI victory condition. Inventory its observable progress signals and
mandatory actions, then add missing observation/action/receipt coverage before
claiming the agent can pursue it. Establish an engine-derived terminal victory
signal with losing/draw/interruption cases, support longer bounded games, and
verify the live planner's model assumptions on those mechanics. Save recovery
requires its own unscored-resume contract and cannot turn an interrupted scored
episode into fresh victory evidence. Diplomacy, religion, culture and broader
learning remain separate milestones rather than implied capabilities of the
current seven-action adapter.
