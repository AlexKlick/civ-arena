# Faster live turns and a sixty-round match

Operator request, 2026-09-05: repair the game parked at turn four, reduce
avoidable decision overhead, and play a fresh two-model game through sixty
complete rounds on the connected monitor. The target is 120 ordered completed
seat turns, with the browser match room available throughout play. No separate
rehearsal game is required for this attempt.

## Verified findings

The previous watch run stopped after three rounds because its movement ledger
and observations disagreed on unit identity. It remains preserved as aborted:
`runs/minimax-resume-20260905T004340Z`. The new implementation uses explicit
`u<owner>:<raw-id>` and `c<owner>:<raw-id>` identities across observations,
actions, ledgers and digests. Live commands preserve the complete raw ID and
verify ownership and returned identity. The adapter requires mod 0.3.4.

A read-only probe on the actual parked turn-four game found raw unit ID 131073
and raw city ID 65536 for both owners zero and one. InGame full-ID lookup
returned the exact ID and owner in each case. No game mutation was performed.
[Cross-VM evidence](../runs/sixty-round-development-20260905/cross-vm-id-probe.json).

The map now uses the engine-verified odd-row transform
`q = x - floor(y/2), r = y`, consistently across units, cities, terrain and
action targets. A direct `Map.GetPlotDistance` probe confirmed all six
neighbor directions at four parity combinations (24 distances equal to one).
It also disproved the previous column-based transform: one purported neighbor
was distance two. Ledger and digest positions remain raw engine coordinates.
The parser recognizes engine `TERRAIN_` names, retaining water and hill types.
Eighteen executable-Lua/parser and host-fixture map checks passed.
[Engine geometry](../runs/sixty-round-development-20260905/hex-row-frame-probe.log),
[Map checks](../runs/sixty-round-development-20260905/map-frame-odd-row-final.log).

Paced live LLM turns begin with a bounded briefing assembled through the
existing audited facade. Map visibility refresh precedes entity projections;
the briefing includes current own cities and their production options. The
prompt encourages useful early actions and grouped independent calls. Recall
is omitted when the actual referee has no recall service. Identical read calls
share results only within one returned tool batch, with invalidation on any
mutation attempt; no cache survives a provider wait or a new turn. Live freeze
semantics are explained without inventing remaining movement.

The initial paced-loop fixture used one model request instead of two while
preserving its eight facade calls, accepted action and turn closure. This is
synthetic evidence, not a measured live speedup. Fifty-six focused LLM tests
passed; a real-session fixture also replays identically.
[Pacing evidence](../runs/llm-pacing-evidence-20260905T013048Z/).

Planner compatibility uses a separate exact owner/raw-to-integer mapping at
its simulator boundary and reverses commands only for currently observed
entities. Numeric simulator behavior is unchanged. Existing planner versus
scripted hotseat and current-version resume checks passed. Pre-migration live
planner journals have no encoding marker and are outside the compatibility
claim; use fresh runs. Both agents in this live attempt use the LLM policy.
[Planner evidence](../runs/planner-identity-evidence-20260905T014039Z/).

Startup clients now consistently honor the existing reconnect cooldown. The
standalone production resolver also retains complete qualified city IDs. The
implementation through `d43c3bb` and the subsequent odd-row correction each
received a read-only review with no remaining confirmed defect in scope.
Full release and live results are
recorded separately below; focused checks do not establish live reliability.

The configured MiniMax client completed a fresh two-request tool round trip
in 2.26 seconds, returning MiniMax-M3 in both replies. Provider credentials
were not printed. These two preflight requests are separate from match usage.
[Provider evidence](../runs/sixty-round-development-20260905/provider-probe.log).

## Repository validation

At implementation commit `aa57ac8`, the final full pytest release gate ran
all 807 collected tests exactly once in four disjoint file groups with
independent temporary directories: **806 passed, 1 skipped, 0 failed,
0 errors**. JUnit test identities exactly match collection; no reruns were
used. Full Ruff over `src tests scripts` also passed. The earlier serial
796-pass gate predates the final geometry correction and is retained only
as historical evidence.
[Final gate](../runs/sixty-round-development-20260905/release-final-summary.json),
[Complete shard logs](../runs/sixty-round-development-20260905/),
[Ruff](../runs/sixty-round-development-20260905/ruff-odd-row-release.log),
[Geometry review](../runs/sixty-round-development-20260905/odd-row-peer-review.json).

## Launch and limits

Run from the linked worktree with an unused UTC run ID:

```bash
cd /home/alexk/civ-arena-reliability-20260904
PYTHONPATH=src PYTHONUNBUFFERED=1 DISPLAY=:1 \
  /home/alexk/documents/civ-arena/.venv/bin/python scripts/live_zero_touch.py \
  --session arch1 --kill-first \
  --config configs/live-hotseat-llm-minimax2-060.yaml --rounds 60 \
  --startup-timeout 2700 --match-timeout 7200 --agent-turn-timeout 600 \
  --recovery-timeout 180 --recovery-sweeps 8 --run-id minimax60-UNUSED_UTC
```

The launcher allocates a fresh startup folder and backs up all existing saves
before replacing the game process. It verifies exactly two alive human major
seats. It retains the physical `:1` display and does not restart X. Model,
provider, 4096 output-token limit, 16 tool rounds, 2000 requests per client,
120-second request timeout and retry limits are unchanged. The 060 YAML differs
in executable configuration only by match ID and sixty-round target.

The existing `declare_own_endpath_drift=true` allowance remains enabled;
each admitted movement row must be present in the event log. Foreign-unit
movement and other out-of-policy changes remain failures. Clocks are outside
deterministic simulation/replay state. Cleanup is bounded to twenty seconds.

Open <http://127.0.0.1:8788/> on this host to view the match room. It follows the
latest run unless the user has pinned a different match. The observer has no
tuner connection or game-control endpoint.

## Follow-up probes

During the fresh match, compare first accepted action, requests per seat turn,
completed-turn elapsed time, rejections and useful game actions with the
retained six-turn diagnostic. Report its small sample and failed-run status.
Check the first real movement, production completion, technology/civic
transition and informational popup against engine and event evidence. A
successful tool receipt alone does not establish a resulting engine change.

After completion, validate the event log with:

```bash
PYTHONPATH=src /home/alexk/documents/civ-arena/.venv/bin/python \
  -m civ_arena.game.civ6.validate_run runs/ACTUAL_RUN_ID --rounds 60
```

Structural replay checks are separate from live engine-state proof. One
sixty-round run does not establish two consecutive fresh thirty-round starts.

## Blocked checks and stop-and-preserve

Live sixty-round completion and a measured live speedup remain pending until
the fresh attempt finishes. Any stage failure stops the attempt. Preserve its
startup and dispatch logs, summaries, wire logs, screenshots and save backups.
For a manual controlled stop, verify the task-owned launch/driver PID against
its recorded command, send SIGTERM and allow the bounded cleanup to finish.
Do not kill Steam or X, reuse a run ID, resume an uncertain lease, or restore
a save automatically. A hard-killed or unwritable run remains incomplete.

## Evidence gaps

The dashboard exposes recorded plans and action order. It does not yet emit
forecast branches or calibrated confidence. Full-game victory, stronger
strategy, stricter mod mutation accounting and automatic save recovery remain
separate milestones. The next planning sequence is in
[live-turn-pacing.md](live-turn-pacing.md).
