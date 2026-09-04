# Strategy program kickoff — 2026-09-04

Objective: build a research platform that produces a stronger full-game Civ VI
agent, with separately measured execution reliability and strategic quality.
This work starts three parallel tracks. A completed implementation or simulator
pilot does not establish live reliability, victory, or strategic superiority.

## Bound starting points

| Surface | Identity | Role |
|---|---|---|
| Reliability worktree | `fix/hotseat-reliability-20260904`, start `5a8611ead13a71f50838fc530b6afb259bd3181e` | Current implementation and operational validation |
| Original checkout | `/home/alexk/documents/civ-arena`, `3074f1c`, original three-file patch | Preserved source; no edits or integration in this kickoff |
| CAR-M1 worktree | `/home/alexk/worktrees/civ-arena-car-m1-v2-20260902`, `car-m1-v2`, `c4a467f` | Separate V2 architecture candidate; read-only dependency review |

The CAR-M1 branch and reliability branch diverge from `f60282f`. V2 changes
the driver, config, executor, and ledger contracts. Moving reliability changes
into V2 requires a deliberate port and review; a passing V1 run does not satisfy
the V2 live gates. Neither branch is merged or published by this kickoff.

## Parallel work and gates

| Track | First deliverable | Work that can proceed now | Gate for further work |
|---|---|---|---|
| Live execution | Steam launch diagnosis and bounded startup repair if a source defect is confirmed | Read-only host diagnosis, launcher tests, preserved diagnostics | Current game window and tuner, then fresh rehearsal, smoke, acceptance A/B |
| Evaluation | Reproducible simulator diagnostic pilot | Existing fixed agents, complementary seatings, explicit seed splits and budget controls, complete artifact validation | Clean paired results and a benchmark that can expose decision differences before strength claims |
| Strategy and full-game scope | Capability inventory and one preregistered experiment | Source inspection, scenario design, resource limits, promotion/stopping rules | Resolve CAR-M1 campaign and branch dependencies before new learning or production policy promotion |

Only the live-execution worker may control the gaming desktop or open a tuner
client. Simulator and documentation workers do not send desktop input, use a
provider, or mutate the game. Work has separate file ownership; integration
tests run once after source changes settle, with complete output retained.

## Milestone sequence

1. **Operational baseline.** Complete a fresh planner/scripted three-round
   rehearsal, a fresh MiniMax/MiniMax three-round smoke, and two consecutive
   fresh 30-round acceptance matches. Each acceptance requires 60 ordered,
   released seat turns, accepted actions by both agents, honest terminal
   evidence, and no unresolved recovery or watchdog violation.
2. **Evaluation baseline.** Measure seating effects and performance against
   several fixed opponents. Treat both seatings of one seed as one paired
   experimental unit. Keep development and held-out seeds distinct; report
   ties, incomplete pairs, dirty runs, and resource use. A small diagnostic
   pilot establishes harness behavior, not statistical power or strength.
   Different-budget planner controls measure sensitivity to compute; they do
   not support an equal-budget algorithm comparison. Existing scripted agents
   intentionally tolerate rejected calls. A strict zero-rejection diagnostic
   must identify those exclusions separately from watchdog violations.
3. **Strategic improvement.** Run one preregistered hypothesis at a time only
   after the relevant gates. Retain the existing planner as the baseline.
   Record equal search/provider budgets and promote a candidate only on
   held-out improvement with acceptable reliability and cost.
4. **Full-game capability.** Add the observable state, legal action support,
   and terminal victory evidence needed for longer games. Validate each new
   mechanic before treating a long-running score benchmark as a full game.

Research rationale: the later entries in [the CivGraph program](civgraph-program.md)
report seating-dominated mirrors, harmful learned priors, and a null learned
value result at the tested simulator scale. These are historical recorded
findings, not fresh reproductions. They motivate benchmark discrimination and
calibration before more learning machinery.

## Preserved operational constraints

Startup is bounded by 2,700 seconds; play by 7,200 seconds; each agent turn by
600 seconds; a stalled transition by 180 seconds and eight recovery sweeps;
cleanup by 20 seconds. Existing MiniMax provider configuration, model,
request/token limits, and movement allowance remain unchanged.

The movement allowance admits only owned-unit `moves`, `movement`, `pos`, `q`,
and `r` rows from the end path, with exact rows and ownership recorded. It is
part of the reliability claim and does not establish strict mod mutation
accounting. Clocks and diagnostics stay outside simulator replay hashes.

Stop at the first failed live stage, preserve its unique run and save backups,
and diagnose before a future attempt. Never rerun into an existing run folder.
The exact launch and stop procedure is in [live validation](live-validation.md).
A code change resets the two-consecutive-acceptance requirement.

## Verified findings

At kickoff, the reliability worktree was clean at `5a8611e`. The separate
CAR-M1 worktree was clean at `c4a467f`. Their source and validation records were
read to establish scope; prior suite and provider results are historical.
Current deliverables and their captured results will be recorded below after
the parallel workers finish.

## Follow-up probes

Resolve Steam readiness before another launch; review pilot completeness and
source/artifact identities; review the capability matrix and experiment's
causal hypothesis. Inventory the overlap between V2 and the reliability
driver before scheduling a port.

## Blocked checks

The previous live attempt completed zero seat turns because no Civ6 process,
window, or tuner appeared. Neither 30-round acceptance run has started.
The separate CAR-M1 record also lacks its mandatory provider and four V2 live
gates. This kickoff does not relabel those requirements as complete.

## Evidence gaps

There is no current proof of full-game victory, live engine replay, or general
strategic superiority. Simulator observations, repository tests, provider
round trips, and live engine execution will be reported separately.
