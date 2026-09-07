# Growth production continuity

The prior `538f84b` run stopped at P0 turn 39 with an empty production queue and
no eligible supported choice. Raising model unit targets did not solve the policy
constraint. The retained intent selected at turn 13 still named `5,32`, but that
tile was now remembered terrain with unavailable ownership. A fresh safe-site
search also found no currently feasible alternative. Ownership guards correctly
prevented travel/founding; they should not require every production prerequisite
to wait until a destination is visible.

With growth execution enabled, one preparatory founder may now train when:

- Native supported options currently offer a unit with observed founder capability.
- Owned, queued and accepted reserved founders total zero; the reserve limit is one.
- The empire is in growth mode and below its city cap.
- Each city has a distinct verified healthy nearby guard, with a healthy spare
  land escort adjacent to the producing city.
- No sent, training-committed, expired or otherwise terminal expansion would be
  replaced, and the model's explicit unit cap permits the production.

This grants production only. Accepted preparatory training retires an infeasible
unsent site and records that transition in `strategy_growth_preparation`. Surveying
must establish a currently feasible site before any travel mission starts. Guards,
ownership, health, foreign-contact constraints, once-frozen movement allowance and
observed founding completion remain unchanged. Ordinary training against a valid
site preserves that intent. Completing one preparation cannot commit a later
unrelated expansion.

Before a founder is queued or owned, an uncommitted escort can survey instead of
waiting indefinitely. Training reserves a currently suitable escort. No extra
native input, movement restoration, provider request per quiet turn or army cap
increase is introduced.

Unsent infeasible intents can also retarget to an observed feasible alternative.
The default bound is three successful retargets per intent lineage, with at most
two distinct observed-state searches per own turn and the existing bounded route
search. Repeated polls of the same observation do not create new searches. Owned
or queued founders, accepted reservations and recorded mission attempts prevent
automatic retargeting. Rejected or ambiguous sent actions still cannot repeat.

## Verified findings

The reconstruction uses the authoritative event log and supporting wire log from
`minimax100-20260907T011202Z`. It rebuilt all 107 projected tiles from 83 P0 map
reads and matched every one of the 49 retained briefing terrain rows exactly.
The final native map read contained 56 currently observed coordinates. Retained
`5,32` has native tundra metadata and no ownership field. Event sequences 4147,
4153, 4158 and 4160 bind the failed policy, briefing and one strategy refresh.

The exact retained projection is in `tests/fixtures/growth_t39_projection.json`.
Its regression selects one preparatory `SETTLER`, keeps all other unit choices
ineligible and grants no route/founding proposal. The facade economy probe queues
that founder with zero additional fake-model requests and records the retired
unsent site. It is repository evidence, not execution in the preserved game.

The settled affected gate passed **386 tests, 0 failed, 0 skipped** in 1.97 seconds.
Full Ruff over `src tests scripts` passed. Complete logs, source/event/wire hashes,
reconstruction and mutation records are retained under
`runs/growth-continuity-evidence-20260907/`. The initial 22 tests passed; an added
lifecycle fixture later assumed a fixed founding turn and failed because its
escort had already surveyed before training. It now observes founding and closure
instead. The original 385-pass/1-fail log remains preserved.

## Follow-up probes

Independently review the frozen commit and run the final integrated release gate.
Qualify productive district/project capabilities separately before claiming longer
gameplay continuity. Observe the reserve, surveying, accepted production and
subsequent safe settlement in a fresh native match.

## Blocked checks

This candidate's facade still enumerates and executes units/buildings only.
District placement and city projects are unrepresented, with native availability
unobserved by this candidate. An empty building list does not prove that the game
has no infrastructure to build. If the bounded founder reserve and supported
choices are exhausted, the runner still stops honestly with that capability gap.

## Evidence gaps

One founder reserve does not establish 100-round durability, native settlement
success, optimum unit counts, optimum city sites or an endless production fallback.
The separate native infrastructure implementation and fresh live validation remain
necessary. The failed run and the frozen initial-capital worktree were not modified.
