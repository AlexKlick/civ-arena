# Productive growth and 100-round validation, 2026-09-07 UTC

## Verified findings

The fresh `minimax100-20260907T011202Z` run used commit
`538f84be69343afe06a171f3226ce284834df78d` and stopped naturally after **38 complete
rounds / 76 correctly ordered completed seats**. P0 turn 39 failed with
`no eligible production within unit targets for c0:65536`. It had zero watchdog
violations, zero recovery sweeps, five observed popup dismissals and completed
cleanup. Exactly one `MATCH_END` matches its summary. The active turn remains
incomplete; this is **FAIL**, not a 100-round pass or a fresh 30-round acceptance.

Authoritative artifacts are under
`/home/alexk/civ-arena-reliability-20260904/runs/minimax100-20260907T011202Z/`.
The event log contains 4163 rows with SHA-256
`6ef912e7d4c007b94e73bb45490623f9d3b5e7dbc05cf8c14b84798166170ec4`.
The independent audit and stopped-state evidence are under
`/home/alexk/civ-arena-reliability-20260904/runs/100-round-live-20260907T011202Z/`:

- `final-independent/review.md`: complete failure accounting, including the
  uncompleted turn's requests and lease. Its run verdict remains NO-GO.
- `native-save-backup.json`: unique 398743-byte save with SHA-256
  `b1aa765007b9b428c89ecc3c33f435f8a07b7c4e2ec795fcd9ba484a961a84b4`.
- `native-logs-at-stop/manifest.json`: 51 copied native logs; copies are not an
  atomic engine snapshot.
- `productive-read/result.json`, `query.lua`, `wire.jsonl`: a bounded read-only
  native query after save preservation. P0's queue was empty, no project was
  enabled, and a Campus had seven legal owned-visible targets. Five targets had
  no feature, resource, improvement or existing district. No action was issued.

The run had 33 logical model generations and 33 input-count requests: P0 48 POSTs
and P1 18 POSTs, including two POSTs in the uncompleted P0 turn. Two malformed
responses were repaired within the configured budget. Generation usage was
121518 input and 12759 output tokens; provider input-count measurements are a
separate quantity. A turn-39 briefing was 8568 characters and admitted normally;
the old 8000-character briefing cap did not cause this failure.

The configured allowance remains **`declare_own_endpath_drift=true`**. Across the
76 completed seats, 76 allowance audits record exactly three admitted own-unit
movement rows. Other mutation domains and foreign-unit changes are not excused.
The active turn has no completed-seat allowance audit. Human-seat evidence has
380 receipts for completed seats plus three opening receipts in the failed turn.

Both seats completed the turn-37 and turn-38 handoffs without watchdog violations.
That is live evidence for those transitions, not proof of 100-round reliability.
P1 never founded its capital in this run. The isolated correction `f768aa2`
subsequently passed 1633 tests with one optional Neo4j skip, full Ruff and 16
independent probes. Those checks do not retroactively repair the failed match.

## Follow-up probes

The next candidate combines persistent initial-capital founding, bounded
unstarted settlement replanning, at most one preparatory settler, and typed native
district/project production. These changes require their affected tests,
independent review and one full release gate on the settled source before launch.
The native read above establishes available APIs and options only. District
submission, plot readback, project admission and later completion need action
evidence from the new implementation.

Preserve the existing provider/model settings, request/output limits and movement
allowance. Fresh play targets 100 complete rounds / 200 released seats under
startup 2700s, match 7200s, agent turn 600s and recovery 180s/eight sweeps. The
physical display remains the requested destination. Do not add a model rehearsal
to either player's first turn.

Prepared support helpers are copied with hashes into
`runs/integration-checks-20260906/copied-helper-bindings.json`. Once the candidate
is clean, reviewed and qualified by local gates, the reproducible command is:

```bash
PYTHONPATH=src /home/alexk/documents/civ-arena/.venv/bin/python \
  runs/integration-checks-20260906/launch_fresh_100.py \
  configs/live-hotseat-adaptive-growth-minimax2-100.yaml 100
```

The helper durably records intent and unique identities before replacing the
startup slot, preserves run-specific backups, installs the bound mod and launches
Architecture-1 on `DISPLAY=:1`. Stage run-specific watchers only after the new
durable pointer exists. Their structural accounting is separate from native
engine state and browser-visible evidence.

## Blocked checks

No fresh launch of this candidate has occurred. Native productive actions remain
unqualified until implementation, review and action readback evidence exist. The
two consecutive fresh 30-round acceptance runs are separately unmet; prefixes of
failed longer runs do not satisfy that requirement.

## Evidence gaps

The existing mod recorder omits production queues, districts and tile ownership.
An unchanged watchdog hash cannot prove district placement, project progress or
absence of tile effects. Exact observed queue/plot receipts must state that
coverage separately; the movement allowance grants no authority for these effects.

District choice initially uses verified consequence-free native placements and
projected ownership with a deterministic distance tie-break. It does not optimize
adjacency or city yields. Research-to-building unlock context is a source-backed
follow-up design, not yet integrated. Four-player live proof, full-game victory,
strict mod accounting, automatic save recovery and strategy learning remain later
milestones.

## Stop and preserve

At the first unresolved failure, stop new model/UI actions and preserve the run,
summary, event log, wire log, screenshots, native logs and unique native save.
The driver owns bounded cleanup and honest terminal recording. Never restart into
the same run folder and never open a second tuner client while the driver runs.

For an operator stop, verify the PID, owner, command and cwd against the current
run-specific `launch.json`. Send SIGTERM **only to that launcher PID**; the launcher
forwards one termination to its driver and waits for the driver's bounded cleanup.
Allow at least 22 seconds for that path. Do not signal the whole process group,
which can cancel the driver once directly and again via the launcher. Preserve
any incomplete cleanup honestly. Do not kill Steam, X, Chrome or other sessions.
