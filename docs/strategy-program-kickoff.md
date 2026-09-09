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

| Deliverable | Current result | Evidence |
|---|---|---|
| Display readiness guard | Implemented in `e232b4b`; 17 focused tests passed | [Host diagnosis and tests](live-startup-followup-20260904.md) |
| Paired diagnostic runner | Implemented in `952c72d`; 16 focused tests passed | [Runner, protocol and captured pilot](strategy-benchmark-pilot.md) |
| Development pilot | FAIL under the declared strict diagnostic gate: 1/12 matches, zero complete pairs, 11 scripted rejections, zero watchdog violations | [Complete output](../runs/strategy-kickoff-20260904T222910Z/benchmark-development.log), [results](../runs/strategy-kickoff-20260904T222910Z/benchmark-development/results.json) |
| Strategy/full-game inventory | Source-backed capability matrix and S-01 preregistration; no treatment implemented or executed | [Next experiment](strategy-next-experiment.md) |
| Source custody | Original checkout patch/HEAD and CAR-M1 HEAD/clean state unchanged | [Captured check](../runs/strategy-kickoff-20260904T222910Z/custody-check.log) |

The pilot ran at clean `1ad8119`, with unchanged source hashes at completion.
Held-out seeds were not executed. The benchmark received a separate read-only
review with no confirmed blocker within its immediate-execution scope; this
does not supply an independent CAR-M1 review or any live-engine proof.
The [retained-byte check](../runs/strategy-kickoff-20260904T222910Z/benchmark-custody-check.log)
verified 110 match/source hashes plus the manifest, with zero mismatches, and
equal before/after state hashes on all 11 rejections. This is custody evidence,
not simulator or live replay.

### Combined repository gate

The full suite ran once with source, tests and configuration from `1ad8119`:
**600 passed, 0 failed, 1 skipped** in 503.94 seconds. Ruff also passed. No
rerun or flaky-test plugin was requested. Only documentation changed while the
suite ran and in the final reporting commit; implementation/test/configuration
bytes were unchanged. The result supersedes historical suite counts for this
kickoff's implementation, and does not supply live or strategy-strength proof.

- [Full pytest log](../runs/strategy-kickoff-20260904T222910Z/pytest-release.log)
- [Full Ruff log](../runs/strategy-kickoff-20260904T222910Z/ruff-release.log)
- [Gate identities and outcome](../runs/strategy-kickoff-20260904T222910Z/release-outcome.json)

Reproduction from this worktree, using its source with the existing project
interpreter:

```bash
PYTHONPATH=src /home/alexk/documents/civ-arena/.venv/bin/python -m pytest -q > /tmp/civ-strategy-release.log 2>&1
/home/alexk/documents/civ-arena/.venv/bin/python -m ruff check . > /tmp/civ-strategy-ruff.log 2>&1
```

Keep a new complete capture when checking changed source. Read these retained
logs for follow-up questions about this gate; do not rerun to rediscover counts.

## Follow-up probes

Resolve Steam readiness before another launch; review pilot completeness and
source/artifact identities; review the capability matrix and experiment's
causal hypothesis. Inventory the overlap between V2 and the reliability
driver before scheduling a port.

The pilot's first exclusion names the next evaluation task: explicitly decide
whether to create a new legal-action-aware scripted baseline or preregister
rejection eligibility categories. Repeated already-selected research, attempted
occupied settlement, and unaffordable purchase are recorded separately. Keep
the failed pilot immutable; a protocol change cannot retroactively validate it.
Any changed opponent is a new baseline with a new source identity. Re-establish
development validity before exposing held-out results.

Read-only branch comparison found 12 overlapping changed paths, including
`config.py`, `firetuner.py`, `live_driver.py`, `lua_translator.py`, and `replay.py`.
This is an overlap inventory, not a merge-conflict resolution or proof of V2
compatibility. Port the window helper first, then recovery/termination semantics,
and validate V2 receipts and executor authority on every policy path before
scheduling V2 live gates.

## Blocked checks

The previous live attempt completed zero seat turns because no Civ6 process,
window, or tuner appeared. Neither 30-round acceptance run has started.
The separate CAR-M1 record also lacks its mandatory provider and four V2 live
gates. This kickoff does not relabel those requirements as complete.

Current live status is **BLOCKED** by the unusable gaming display. The existing
`/home/alexk/.local/bin/gaming-mode` helper needs sudo privileges unavailable to
this session. No fresh live match or provider request was attempted during this
kickoff. Restored display readiness must be followed by Steam authentication,
game-window and tuner checks; the display repair alone cannot pass the gate.

## Evidence gaps

There is no current proof of full-game victory, live engine replay, or general
strategic superiority. Simulator observations, repository tests, provider
round trips, and live engine execution will be reported separately.

The [normalization scan](../runs/strategy-kickoff-20260904T222910Z/qa-normalize-final.log)
covered 11 documents and raised six prompts. Three concern historical documents:
the old relative M20c artifact path resolves only in the original checkout,
and older date/blocker wording is not refreshed by this kickoff. The actual
M20c path and byte check are explicit in the capability inventory. The other
three flag provider wording: the prior live record links its captured provider
response, while the two new documents explicitly record no provider execution
or proof. These heuristic warnings do not establish new provider failures or
readiness. Local links in the updated deliverables were checked separately.
