# Adaptive growth 100-round validation, 2026-09-07 UTC

The subsequent live attempt failed after 38 complete rounds. The current failure
record, fixes and launch/stop procedure are in
[live-productive-growth-100.md](live-productive-growth-100.md). The earlier
prelaunch evidence below remains historical.

## Verified findings

The preceding `minimax100-20260906T193543Z` match failed after 36 complete
rounds and 73 released seat turns. Its terminal record, native save backup and
watchdog evidence remain preserved. The detailed diagnosis is in
[live-human-handoff.md](live-human-handoff.md). Popup clearing already ran every
five seconds; native outgoing-seat AI economy caused the uncommanded purchase.

This isolated integration combines ordered human handoff, persistent health
recovery, native capability-based production, guarded settlement execution,
provider-counted adaptive briefings and the recorded-state browser map. The opt-in
configuration is `configs/live-hotseat-adaptive-growth-minimax2-100.yaml`.
Provider/model, seeds, output limit, request cap and movement allowance retain the
preceding configuration. Count POSTs and generation POSTs both consume the cap.
`max_result_chars: 8000` bounds returned directives/tool results; it no longer
hard-truncates the model briefing. The declared MiniMax-M3 window is 1,000,000
tokens with 4,096 output tokens reserved. Provider-reported input counting and
reported generation usage are distinct measurements, not asserted equal.

Source and earlier evidence are preserved in these isolated worktrees:

- Integrated source: `/home/alexk/civ-arena-growth-live-20260906`.
- Frozen growth execution: `/home/alexk/civ-arena-growth-integration-20260906`.
- Prior failed match and all new live run folders:
  `/home/alexk/civ-arena-reliability-20260904/runs`.

The checked-in normal end-turn path was exercised on the preserved postmortem
session: P1 turn 38 released without watchdog violations, then a separate bounded
follow-up engaged P0 turn 39 and verified both GameCore human flags. The first
probe's premature immediate-engagement assertion failed; that terminal stays
failed. These are diagnostics, not resumed match completions. Evidence:
`runs/native-normal-handoff-20260906/` in the integration worktree.

Native production capability parsing observed five available unit types plus a
granary, including founder/builder/land-combat capabilities, without issuing game
orders. Evidence: `runs/native-production-capabilities-20260906/`.

A real corrected MiniMax client performed one count POST and one admitted JSON
generation POST, both HTTP 200, using retained context. No returned action was
executed. Evidence: `runs/adaptive-provider-qualification-20260906/result.json`.

The dashboard at http://127.0.0.1:8788/ serves the integrated zoomable map. Actual
visible Chrome checks covered recorded turn 37, explicit spectator scope, map zoom
surviving polling and zero captured unexpected console/page errors. The minimap
combines dated observations; it is not an omniscient continuous engine map.
Evidence: `runs/dashboard-delivery-20260906/browser.json` and
`display-proof.json`. The latter confirms Chrome and Civ6 on physical display
`:1`, connected DP-2 at 3440 by 1440. Browser server provenance is separately
recorded; later growth changes do not modify its dashboard files.

## Follow-up probes

Finish independent growth and supplemental audit review, then run the full pytest
release gate and Ruff on the settled integration. Each retained full log and exact
source identity is recorded in `runs/integration-checks-20260906/`. Do not treat a
fixture error, interrupted collection or a truncated tool display as a pass.

Launch a fresh 100-round match only after those checks. Require 200 released seat
turns in order, exactly 100 complete rounds, accepted actions from both agents,
zero watchdog violations/expired deadlines, consistent terminal and summary, and
explicit exact movement rows under `declare_own_endpath_drift=true`. Structural
replay accounting and observed engine behavior are separate proof lanes.

The prepared launcher records the source commit/tree, configuration and mod hashes,
unique run ID, command and backups before live mutation. Each fresh run has a
separate startup folder and save backups. The reproducible invocation, from the
integration worktree with the already configured credential environment, is:

```bash
PYTHONPATH=src /home/alexk/documents/civ-arena/.venv/bin/python \
  runs/integration-checks-20260906/launch_fresh_100.py \
  configs/live-hotseat-adaptive-growth-minimax2-100.yaml 100
```

It forwards startup 2700s, match 7200s, individual agent turn 600s, stalled
transition 180s/eight sweeps and uses `DISPLAY=:1`. The launcher opens a fresh
Architecture-1 game; no model warmup is inserted into each player's first turn.
Once the launcher has produced durable intent, `stage_watchers.py` creates
run-specific read-only audit watchers from that exact source identity. It must not
be run against an old active-run pointer.

## Blocked checks

No external blocker is currently established. Live 100-round completion is pending
review and final gates. The original two consecutive fresh 30-round acceptance
runs remain separately unmet. A 30-round prefix of a longer failed match cannot
satisfy that requirement.

## Evidence gaps

Repository and native boundary tests do not prove strategic optimality, full-game
victory or a live automatic second-city mission. Four-player engine qualification,
strict mod mutation accounting, automatic save recovery and new learning
experiments remain separate work. Unknown contacts do not establish opponent war
intent; terrain-based forecasts and action weights are not calibrated certainty.

## Stop and preserve

On first unresolved failure, stop new model/UI actions and preserve the run,
unique save backups, event log, summary, wire log and native logs. The driver owns
bounded cleanup and durable terminal handling. For an operator stop, read the
run-specific `launch.json`, verify its PID/process-group command and cwd, and send
SIGTERM only to that verified launcher PID; it forwards one signal to its driver. Allow its bounded cleanup to finish;
never restart into the same run folder. Do not kill Steam, X, Chrome or unrelated
processes. A hard kill/storage failure is incomplete evidence and cannot pass.
Never open a second tuner connection while the match driver is active.
