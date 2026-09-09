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
The turn journal is a horizontal swimlane — one lane per seat, one column per
engine turn, time running left to right. The selected turn is expanded into one
node per recorded call in recorded order; the turns around it stay as pills that
summarise the seat turn. All, Actions, Observations and Notes filter what the
lanes and the pills count. Click a node or use keyboard focus and Enter to
inspect the arguments, result and rejection. Rejected planning calls remain
visible in the journal but do not become accepted plan notes.

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

## Comparison data (M2)

`/api/run` carries two comparison blocks beside `turns`.

`research` holds one entry per configured seat: the seat's `latest` request
packet (turn, sequence, researching, researched set, gold, research options and
the recorded `options_source`), a delta-only `history` of packets whose
researched set changed, and a `diff` of shared and seat-only technologies taken
from each seat's own latest packet. Seats act asynchronously, so the two `as_of`
turns usually differ; that is displayed, not smoothed. A packet whose recorded
character count, digest, projection marker or projected seat does not match is
never displayed: it raises a warning and sets `research.incomplete`, which does
not mark the journal itself incomplete.

A packet that supplied no researched list is counted in `packets` but is not a
research observation: it never removes a technology, resets a count or replaces
`latest`. When the history hits its row limit the dropped rows are folded into
one synthetic first row marked `baseline: true`, carrying the last dropped
packet's turn and the set it left behind — so a truncated history never reads as
a fresh discovery. Every other row carries `baseline: false`. Absent threat
lists in `growth` stay `null` rather than becoming zero.

`timeline` holds per-seat rows for each recorded seat turn. Every value carries
a `sources` tag naming where it came from — `audit` (driver completed-seat-turn
row), `events` (recorded counters), `tool` (an accepted `get_overview`,
`get_units` or `get_cities` result) or `packet` (the seat's own request packet).
A missing value stays `null` with a `null` source; nothing is interpolated or
carried forward. `series` names the eight comparable per-turn measures. Provider
requests are recorded POST attempts, not decisions. Each turn additionally
carries `strategy_delta` (which directive fields changed against the seat's
previous recorded turn, with tactical overrides reported only as a count),
`economy` (recorded `set_research` / `set_city_production` attempts with their
status, rejection and candidate reason) and `growth` (the recorded assessment
allowlist). All lists are explicitly bounded and dropping items raises a
warning.

`GET /api/tech-tree` returns the committed technology-layout asset derived from
the base source catalog by `scripts/derive_tech_tree.py` (68 nodes, 90 edges,
per-era `[11, 8, 7, 9, 8, 7, 8, 10]`). Regenerate with that script and verify in
place with `--check`. The asset is validated on every request — version, era
order, node fields, bounded ids and no backward-era edge — and an unverifiable
asset is withheld as a 404 instead of drawn. The file itself is not a static
route: `/tech-tree.json` is 404. The tree describes base source layout with
unverified prerequisite group semantics; it is not the effective ruleset of the
displayed match.

## Comparison panels (M2)

Three panels sit between the seat cards and the turn journal, all fed by the
same retained `/api/run` payload the rest of the page uses. A match recorded
before these keys existed keeps working: each panel prints "Comparison data not
recorded for this match" and nothing else changes.

**Who knows what** compares the two seats' research. Each seat's set is folded
from its own retained request packets up to the selected turn, so the as-of line
names each seat's packet turn and sequence separately — the seats are
asynchronous and the two as-of turns routinely differ. The three columns are the
shared set and each seat's exclusive set, with the count in the column head.
Technologies that are not in the base source catalog are kept and grouped under
"Outside base catalog" rather than dropped. The "Researching" row is the
selected packet's own field; "Could pick next" prints the recorded options only
when the packet actually observed them, and otherwise names the source token it
was given; a packet that observed the options but recorded none says exactly
that. No option list is inferred. If the selected turn is older than the seat's
retained history — that is, before its `baseline` row — that seat's column
prints "Research history before turn N was not retained", its tree halves are
dimmed instead of filled, its era counts read `?/N`, and nothing is called
shared. The other seat is unaffected.

The tree below it is laid out from `/api/tech-tree`, which is derived from the
base source catalog: eras are columns, same-era prerequisite depth makes
sub-columns, and the catalog's `UITreeRow` sets the vertical position. Each node
carries two halves, left seat and right seat: a filled half means the seat had
that technology at the selected turn, an outlined half means the seat was
researching it. The era header counts both seats. Selecting a node (click or
Enter) fills the fact list with era, recorded cost, prerequisites, and each
seat's status. The tree is requested once per match, never per poll. If the
catalog asset cannot be served the tree is hidden and the panel says so; the
research sets above stay. The legend states what the connective is worth:
prerequisite → tech, left to right, connective unverified, base source catalog,
not the effective ruleset of this match.

**Pacing and progress** is eight small multiples over the same turn axis: seat
turn time, provider requests (recorded POST attempts, not decisions), tool
calls, allowed mutations, gold, techs researched, own units and own cities. The
x position is the turn number itself, not the row index, so a turn nobody
recorded stays an empty stretch; a segment joins two points only when they are
consecutive recorded turns for that seat. A gap in a line is an unsupplied value
and is never bridged — the line breaks, and a lone reading is drawn as a dot. Red
triangles under the axis are watchdog violations, with the recorded kind and
detail in their tooltip. The selected turn is a hairline on every chart; hover
or keyboard focus moves a crosshair and reads both seats' values for the nearest
turn into a live region, and clicking picks that turn for the whole page. Every
value is also in the "Table view" twin, so nothing is reachable only by hover.

**Decision strip** sits in the turn journal head: per seat, one pill for the
strategy directive, one for the recorded economy calls (accepted ✓, rejected ✕
with the rejection text, ◌ when no status was recorded), and one for the growth
assessment. The directive pill names the recorded source rather than assuming a
model wrote it: the first recorded directive, a model update with its reasons
and changed fields, an unchanged or changed autopilot turn, any other recorded
source token, or "source not recorded". "Not recorded" means the audit was
absent, not that nothing happened. Selecting a pill scrolls to the full strategy
panel, which states that listed values are seeded selection weights, not
probabilities of success; the journal legend reads "Recorded order, not
causality".

The pure half of these panels (folding, the diff, chart scales and segments,
decision sentences, tree depth) lives in `dashboard_static/compare-core.js` and
is served at `/compare-core.js`. It touches no page and makes no request, so
`tests/test_compare_core.py` evaluates it under Node exactly as the browser
runs it.

Colours carry seats only: seat 0 gold, seat 1 teal, in both the tree halves and
the chart lines; labels stay on text colours so no reading depends on hue.
Charts draw no animation and honour `prefers-reduced-motion`.

## Turn journal (M3)

The journal replaced the vertical two-column graph. It is one horizontal
swimlane per seat over a window of thirteen recorded turns centred on the
selected one, with time running left to right and one column per engine turn.
The selected turn's column is expanded — every recorded call is a node, in
recorded order, with an arrow to the next node in the same lane. Every other
turn in the window is a pill carrying that seat turn's recorded call count, the
per-kind glyph counts and, when there are any, the rejection count; its tooltip
prints accepted, rejected and pending totals. A seat that recorded no turn at
that number says "no seat turn recorded" rather than showing an empty lane, and
a lane whose calls the filter removed says "No matching calls recorded" — an
absence of records and an absence of matches are different statements.

Arrows join consecutive nodes in one lane. They are recorded order and nothing
else: not causality, not a dependency, not a plan. The head keeps the line
"→ Recorded order, not causality" for that reason.

Clicking a pill selects that turn for the whole page; Shift-clicking one opens
it beside the selected turn without changing the selection, and clicking the
turn label of an opened column closes it again. The jump pills in the header
("‹ N earlier turns" / "N later turns ›") move the window by six turns without
changing the selected turn, so the recorded turns outside the window are stated
rather than hidden, and the strip starts at the first column of the window they
open. A poll that records a new turn moves the window's edge, never the reader's
place in it. A turn the reader opened keeps the records behind its calls
even after the window has moved past it, and CLOSER LOOK keeps showing the
selected call; while the window sits away from the selected turn the journal
prints no empty state, because the pills on screen are recorded turns. Choosing
a different turn starts the journal over — including when Follow latest turn
moves the selection without anybody clicking, and re-checking Follow latest turn
starts it over even when the selection is already the latest turn, so the window
comes back to it. Arrow keys walk a lane by recorded
position, Up and Down cross between the lanes and the header, and Enter or Space
activates whatever is focused — the same reach as the pointer.

Consecutive calls with the same tool *and* the same status fold into one dashed
run node labelled `×n`; two are enough, and a rejection never folds into an
acceptance. Folding hides no record: the foot prints "N calls shown · M folded
into runs", the run's tooltip lists the recorded sequence numbers, and clicking
the run opens it into its individual calls. When the reader is inside a run,
CLOSER LOOK says which call of how many it is.

Kind is carried by a hue *and* a glyph (◉ observation, ▶ action, ✎ note) and
status by a glyph *and* a colour (✓ accepted, ✕ rejected, ◌ pending), both keyed
in the journal foot, so nothing here has to be read by hue. The lane gutter with
the seat labels stays pinned while the strip scrolls under it.

The pure half of the journal — filtering, run folding, the pill summary, the
turn window and the whole strip geometry — lives in
`dashboard_static/journal-core.js` and is served at `/journal-core.js`. It
touches no page and makes no request, so `tests/test_journal_core.py` evaluates
it under Node exactly as the browser runs it, and the same test parses the five
match-room scripts as one concatenation because the browser loads them into a
single global scope.

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
