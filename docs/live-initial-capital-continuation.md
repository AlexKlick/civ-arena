# Bounded initial capital progress

With `growth_autopilot: true`, a cityless seat with one observed owned `SETTLER`
uses one guarded in-place `found_city` action by default. The model can instead
request immediate founding or one adjacent relocation through its existing
one-turn tactical directive. A relocation continues to guarded founding only after
arrival is observed. The controller never inserts its default into model tactical
overrides. Growth-disabled behavior and ordinary escorted expansion retain their
existing paths.

The current projection must show the selected owned settler, health at least 90%
with valid health observations and no active recovery overlay, known land with
known unowned/own ownership, no city within four hexes and no observed foreign
unit within two hexes. Foreign contacts represent uncertainty, not automatic war.
These are conservative execution restrictions, not native legality, visibility
freshness or safety proofs. The existing facade performs native legality checks.
Automatic founding uses positive observed movement or the existing verified,
untouched opening frozen allowance; it introduces no movement restoration.
Explicit model input remains on the existing tactical path and is validated
against the current capital guards before dispatch. The controller also rechecks
the exact pending founder/action/origin/site and these guards immediately before
each explicit founder facade call, using the latest projection refreshed after
any preceding unit action. A newly revealed guard aborts with `capital_unresolved`
and a `strategy_initial_capital` audit outcome of `not_dispatched`, empty
`execution`, and the proposed `attempted_action`. This is a withheld proposal, not
an accepted or rejected native receipt. The ordinary scouting selector can also
withhold an unavailable target before this callback; the capital audit then
records `selected_founder_action_not_dispatched` in its progress failure reason.

A rejected relocation, or an accepted relocation with no arrival at the first
fresh later own-turn observation, requires a founder-specific resolution. A
warrior-only directive cannot clear this requirement. The existing two-attempt
format-repair mechanism admits a guarded founding at the current tile, a different
adjacent relocation, or a hold justified by an observed guard. No new provider
request loop or request cap is added. A failed origin/destination edge cannot be
sent again by this plan. Explicit revisions archive the previous mission and
pending receipt instead of silently overwriting their correlation.

Operational heuristics bound the entire initial plan: at most two relocation
submissions, two guarded hold turns, and six elapsed own-turn intervals from the
first cityless observation. Revisions do not reset these budgets. These constants
are uncalibrated reliability limits for establishing a first capital, not strategy
quality targets, production deadlines or additional match clocks. Exhaustion
raises `capital_unresolved`; the runner retains its ordinary terminal and cleanup
behavior. Accepted but still unconfirmed founding stops at the next own-turn
observation. Rejected founding permits a meaningful explicit relocation review,
but never another founding request at the same already-attempted tile. An unknown
input outcome poisons the current controller rather than authorizing a resend.

## Observed completion and audit contract

All three founding paths use the same completion correlation: an accepted
`found_city` response, a newly observed owned city at the selected site, and the
selected founder absent from all projected units. `strategy_initial_capital.mission`
and `.progress.mission` carry `unit_id`, `site`, `selection_basis`,
`observed_city_id`, `observed_player_id`, `founder_consumed`,
`observed_completion_turn`, and
`completion_basis=accepted_founder_consumed_new_owned_city_at_site`.

`selection_basis` distinguishes `controller_default_guarded_in_place`,
`explicit_model_founding`, `explicit_model_relocation_then_guarded_founding`, and
`explicit_guard_hold_then_controller_default`. Explicit actions remain in the
scouting graph; automatic founding has its own execution row. Only after turn
closure does `strategy_turn_closed.initial_capital_completion` include the same
identity plus `completed_turn`. A failed closure never emits that completed turn.
An unrelated owned city disables the initial path without claiming correlated
founding, and cannot later be upgraded into success. Later city loss does not
reactivate an opening plan.

## Verified findings

The stopped `minimax100-20260907T024441Z` event log contained 11 complete rounds
and 22 seat turns. P0's turn-1 relocation from `30,11` to `31,11` was accepted
without observed displacement; the turn-3 progress review contained only a
warrior order, and turn 10 repeated the same settler edge. This establishes a
missing progress-resolution contract, not the native cause of that failed move.
The retained diagnostic is `runs/capital-reliability-review-a941` in the separate
dashboard-productive worktree.

Current repository evidence is recorded under `runs/capital-progress-evidence`.
The initial focused run had 18 failures/11 passes: obsolete continuation
expectations plus a real delayed-arrival guard defect, which was corrected.
The next run passed 29 cases. Added regressions initially had three fixture
failures/34 passes: fake responses were indexed from total request count, and
actor loss occurred after a scouting refresh rather than before the initial
request. Corrected fixtures passed all 37 capital cases. The affected gate passed 261 tests with no failures, errors, skips or deselections
in 39.55 seconds (`focused-final.log`). Six new line-length warnings were corrected;
Ruff then passed all three Python files (`ruff-final.log`). The post-format capital
check passed 37 tests in 0.19 seconds (`capital-after-format.log`); formatting did
not change behavior. These checks are not the full repository release gate.

## Follow-up probes

Independently review the frozen candidate, then bind the parent integration and
release gate. A fresh live match must show both initial capitals with causal
founder/city receipts and completed seat turns, plus the existing request and
movement-allowance accounting. Completed seat counts alone do not establish this
capital milestone.

## Blocked checks

No native, provider, desktop or service action was performed in this worktree.
The parent owns live qualification and the full repository release gate.

## Evidence gaps

Projected fixtures do not prove native city founding, an optimal capital site,
complete information about nearby danger, victory or a fresh 100-round result.
The conservative contact guard can stop a viable but uncertain opening. Multiple
initial settlers are intentionally ambiguous. This repair establishes a bounded
first-capital action contract, not general city-loss recovery or global strategy.


## Dispatch correction evidence

Independent review of `0b9ef875` confirmed that checking explicit founder guards
only before a scouting batch allowed another unit's accepted action to reveal
health loss or a nearby foreign contact before founding/movement. The correction
checks again in the controller's execute callback; no scouting or native API is
changed. Original immutable review evidence remains under
`runs/independent-0b9ef875`. Correction evidence is under
`runs/capital-dispatch-correction`.

The unchanged six independent cases plus the original 37 capital cases passed
43/0. Expanded coverage initially passed 53 cases and failed one audit assertion:
the existing scouting selector itself withheld a missing move target before the
new dispatch callback. The assertion now distinguishes this existing selector
refusal from the new dispatch-guard audit. No unsafe request occurred in that
fixture. The final affected gate passed 278 tests with zero failures, errors, skips or
deselections in 39.52 seconds (`affected-final.log`); Ruff passed the three changed
Python files (`ruff-final.log`). Exact source binding is recorded in the
correction directory. Native execution remains a separate parent-owned check.
