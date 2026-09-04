# Strategy benchmark diagnostic pilot

This is bounded evaluation plumbing on the legacy V1 simulator. It does not
satisfy the CAR-M1 campaign gate, promote an agent, test a learned treatment, or
establish live Civ6 strength. The prior M20 findings motivate checking whether
our evaluation distinguishes strategy from seating before further learning work.

## Frozen scope

`configs/strategy-benchmark-pilot.json` specifies 20 turns, development seeds
700003/707922, held-out seeds 800003/807922, and three complementary seating pairs:
MCTS budget 4 versus turtler; expansionist versus turtler; MCTS budget 4 versus
MCTS budget 8. The last comparison calibrates search budgets; it is not an
equal-budget policy-strength test. Each map/pair contributes one cluster. A seed
where different variants win opposite seatings is reported as a seat split,
never as two independent strength observations. Ties remain in descriptives.

The script accepts only four frozen deterministic variants, at most 24 total
scheduled matches, and at most 40 turns. There are no provider calls, fitting,
case bases, bandits, proposers, live input, or mutable learned state. Variant RNG
seeds travel with the variant across seatings (MCTS=7, turtler=22,
expansionist=37). This controls one source of confusion in older seat-fixed RNG
experiments, but it does not disentangle board starts from phase order; that
would require a separate attribution experiment. The development
split is the default. Held-out seeds remain untouched unless explicitly selected.

## Validity and evidence

Each match must have exactly the expected ordered lease grants, lease releases,
and completed turns, one start and terminal event, a terminal event matching the
summary, the requested horizon, a final digest, zero watchdog violations, and
zero tool rejections on **either** seat. Any dirty match stops the pilot. Missing,
duplicate, foreign, or schedule-mismatched rows suppress all aggregates. Existing
scripted policies intentionally attempt actions that may be rejected; their
exclusion by this stricter contract is an evaluation compatibility finding, not
an engine defect or a strategy-strength result. Every rejection's seat, turn,
tool, and reason is retained for diagnosing that boundary.

`manifest.json` binds the frozen schedule/configuration, variants, score weights,
Git HEAD and dirty state, and actual source-file hashes. `results.json` retains
final source identity, event/config/summary/trace byte hashes and immediate
execution audit. Source-content changes during the pilot invalidate it. Unrelated
tracked changes are recorded without silently substituting a clean-head claim.
Wall time is isolated in non-contractual `telemetry.json`. Planner traces include
actual decision/node counts and per-decision rollout budgets. Event logs remain
the authority. This runner has no offline revalidation command and performs no
structural or simulator replay: retained hashes are custody references, not a
claim that later modified artifacts have been revalidated.

Output must be a new directory, including refusal of empty existing directories.
Repository outputs are confined to `runs/`; aliases resolve before creation.
The harness retains partial results and a failed terminal report on controlled
runtime exceptions. A hard timeout/kill may leave only partial evidence and can
never pass. These pilot reports do not replace per-match event-log termination.

## Reproduction and stop procedure

Run from the implementation worktree, with a unique output path:

```bash
PYTHONPATH=src PYTHONUNBUFFERED=1 timeout --signal=TERM --kill-after=20s 600s \
  /home/alexk/documents/civ-arena/.venv/bin/python scripts/strategy_benchmark.py \
  --config configs/strategy-benchmark-pilot.json --split development \
  --out runs/strategy-benchmark-UNIQUE > /tmp/strategy-benchmark-UNIQUE.log 2>&1
```

Keep the complete console log. Exit 0 means the immediate diagnostic audit
passed; exit 2 means it failed. A timeout or missing `results.json` is incomplete.
Never re-enter that output directory. Preserve the first failed match and report
its exclusions before proposing policy changes or another evaluation. Held-out
execution is separately explicit (`--split held_out`) and must use the unchanged
preregistered configuration, source identity, and scoring rules; this pilot does
not claim historical global non-use of those seeds.

## Verification record

Focused regression suite before kickoff: 16 passed, 0 failed, 0 skipped;
`/tmp/strategy-benchmark-focused-final.log`. Ruff passed for the new script/tests;
`/tmp/strategy-benchmark-ruff-final.log`. This included a real two-seating MCTS
match audit and negative probes for rejections, malformed inventories, missing
lease release, source aliases, output reuse, and interrupted execution.

The first integration fixture also exposed actual scripted `found_city` /
`occupied` and `set_research` / `already` rejections. The strict audit rejected
that fixture; the clean success fixture uses the existing MCTS pair. No scripted
policy was changed or rejection waived to obtain a passing gate.

## Captured development diagnostic

At clean commit `1ad8119c7055f9d1cb91336fbcab4f56a6f02433`, the development
command ran once with the frozen configuration and a 600-second outer limit.
[Complete output](../runs/strategy-kickoff-20260904T222910Z/benchmark-development.log)
and [exit status](../runs/strategy-kickoff-20260904T222910Z/benchmark-development.exit)
record exit **2**, an excluded diagnostic result.

Of 12 scheduled development matches, one completed all 20 turns before the
pilot stopped. It had zero watchdog violations, zero planner-seat rejections,
and 11 turtler-seat rejections: one `found_city/occupied`, eight
`set_research/already`, and two `purchase/insufficient_gold`. There are zero
complete paired clusters; aggregates are suppressed. The remaining development
matches and every held-out match were not attempted. The raw score difference
from this excluded single seating is not a strategy-strength result.

[Results and exact rejection rows](../runs/strategy-kickoff-20260904T222910Z/benchmark-development/results.json)
and [manifest](../runs/strategy-kickoff-20260904T222910Z/benchmark-development/manifest.json)
bind source identity and retained match artifacts. Source was clean at both
observations and source-file hashes were unchanged. Seven planner decisions
expanded 35 nodes at budget four. The 1,020ms elapsed observation overlapped
the full pytest gate and cannot establish isolated cost performance.

Follow-up: reconcile the intentional scripted-policy rejection behavior with
the benchmark validity contract before another evaluation. Any baseline change
needs its own identity and fresh development evidence; any eligibility-rule
change needs an explicit protocol revision before evaluating held-out seeds.
Do not reinterpret this failed diagnostic using a post-hoc relaxed rule.

Read-only tracing narrowed that follow-up. `scripted.py` selects the first
available doctrine technology even when it is already selected. Its settler
loop reuses the cities observation after the first successful founding, so the
second settler attempts to found in newly claimed own territory. Its city loop
reuses initial gold after purchases: the earlier purchases left 22 and 9 gold,
then another city attempted a 100-gold item. These are own-observation issues,
not missing foreign information. All 11 rejected-call before/after hashes are
equal; the referee correctly refused them.

The smallest proposed repair is a separately versioned scripted baseline that
checks current research and refreshes own city/treasury observations after
accepted actions. Suppressing invalid purchases also suppresses an RNG draw
used in the idempotency key; the same RNG drives later scouting. Preserve the
legacy policy, and test the new baseline's observation and RNG behavior before
using it for comparisons. No baseline repair was implemented in this kickoff.

The CAR-M1 campaign gate remains **BLOCKED**. This pilot establishes no
learning, provider, live-engine, replay, or promotion result.
