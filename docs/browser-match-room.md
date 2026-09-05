# Browser match room

Run from the linked worktree:

```bash
cd /home/alexk/civ-arena-reliability-20260904
PYTHONPATH=src /home/alexk/documents/civ-arena/.venv/bin/python \
  -m civ_arena.dashboard --runs-root runs --host 127.0.0.1 --port 8788
```

Open <http://127.0.0.1:8788/> on this host. The server reads existing run events
and summaries; it does not connect to Civ6, a tuner or a model provider. It
binds only to loopback and has no write or game-control endpoints.

Select a match and engine turn, or leave Follow latest turn enabled. Each
seat shows accepted planning notes, recorded request counts and action timing.
The graph displays tool calls in recorded order, with separate action and
observation filters. Click a node or use keyboard focus and Enter to inspect
the arguments, result and rejection. Rejected planning calls remain visible in
the graph but do not become accepted plan notes.

Graph arrows describe sequence, not causal dependencies. Notes are what the
model chose to report, and confidence is model-reported. Forecast branches
require explicit future plan records; the current screen states their absence.
The [pacing and forecast plan](live-turn-pacing.md) records that next phase.

The UI polls every two seconds after the preceding request finishes. A recent
event indicates log activity, not process liveness. Terminal results require
matching event-log and summary records. Incomplete logs and unavailable data
remain visible as warnings. Large logs, lists and response bodies have bounded
read/display sizes; truncation is labeled. Credentials in recognized sensitive
fields and current secret environment values are redacted. Raw wire logs and
provider configuration are not part of the API.

## Stop and preserve

Stopping this server does not stop the game. Use Ctrl-C on a foreground server,
or SIGTERM only to the PID verified against its exact dashboard command. The
current detached server's PID and command are recorded in
`runs/dashboard-evidence-20260905/server-process.json`; preserve its log.

For a match-controller failure, retain the unique run folder, event log,
summary, wire log and saves. Do not restart a controller against an uncertain
active lease or overwrite a save slot. The resumed match below has already
stopped with completed cleanup and remains diagnostic evidence.

## Verified findings

The current displayed run `minimax-resume-20260905T004340Z` completed three
rounds / six ordered seat turns and sent 50 MiniMax requests. It then aborted
on two watchdog findings. The matching terminal summary reports completed
cleanup. It is not a clean 30-round acceptance run.
[Terminal audit](../runs/dashboard-evidence-20260905/live-terminal-audit.json).

Implementation commit: `5e976b0`. Focused backend checks: 18 passed, zero
failed or skipped; Ruff passed. These cover actual HTTP access, malformed
records, redaction, bounded reads, call pairing, terminal consistency, ordered
turn completion, index refresh and traversal/symlink rejection.
[Focused log](../runs/dashboard-development-20260905/pytest-reviewed.log).

The settled implementation passed the full repository gate: **735 passed,
zero failed, one skipped**, in 528.44 seconds. No reruns or flaky retries were
used in this gate. Ruff passed for `src tests scripts`. Source identity and
file hashes are recorded separately from subsequent documentation changes.
[Full pytest log](../runs/dashboard-evidence-20260905/pytest-release.log),
[Ruff log](../runs/dashboard-evidence-20260905/ruff-release.log),
[source identity](../runs/dashboard-evidence-20260905/source-identity.json).

Real Chrome checks on <http://127.0.0.1:8788/> at 1440x1100 and 390x844
verified the retained actual run, two seat panels, notes, metrics, action and
observation filters, click/keyboard node selection, turn selection and
follow-latest behavior. Neither viewport had horizontal document overflow.
The tested page reported zero captured console errors or network failures.
This is page-level browser evidence; Chrome's separate host service log has
update/registration messages and is not a zero-error host claim.
[Browser result](../runs/dashboard-evidence-20260905/browser-result.json),
[desktop](../runs/dashboard-evidence-20260905/desktop.png),
[graph details](../runs/dashboard-evidence-20260905/graph-details.png),
[mobile](../runs/dashboard-evidence-20260905/mobile.png).

A separate browser fixture started with one pending call, then appended the
remaining retained match events and summary. The page automatically updated
to six completed turns, selected turn three and displayed the stopped status
without reload. This proves incremental display using a synthetic append;
it is not new engine-play evidence.
[Incremental result](../runs/dashboard-evidence-20260905/incremental-result.json).

The visible dashboard window was opened and captured on physical display
`:1` at 1150x1200. [Monitor capture](../runs/dashboard-evidence-20260905/monitor-browser.png).
The server and visible browser remain available. The separate automated
browser and fixture server were stopped after their checks; only the main
dashboard listener remains on port 8788.

Read-only review reproduced and resolved one malformed-record defect: an
`agents=null` configuration initially hid healthy runs from the inventory.
The same three HTTP probes now return 200, retaining the healthy run and
marking the malformed run incomplete. Initial browser CSP styling errors and
the missing favicon were also fixed and the browser check repeated. Initial
failure artifacts remain under `runs/dashboard-evidence-20260905/initial-*`.

## Follow-up probes

Add explicit forecast records and branch inspection under the planned event
contract. Validate concurrent game play and browser display during a future
repaired match; current browser evidence uses retained events and a separate
synthetic append fixture.

## Blocked checks

Both consecutive 30-round live acceptance runs remain blocked by the
[unit-identity defect](live-resume-diagnosis-20260905.md). This dashboard does
not change game behavior or resolve that defect.

## Evidence gaps

Recorded-order graphs do not prove causal planning, calibrated confidence or
future-state prediction. Browser playback of an existing live log is separate
from observing a new live engine run.
