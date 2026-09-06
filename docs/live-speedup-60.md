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

## First live startup result and correction

The first fresh attempt, `minimax60-20260905T020306Z` at `a582c012`, stopped
before dispatch: its final engine census contained four majors, not two.
Startup took 507.39 seconds, sent zero model requests, and preserved 22 save
files plus the load-slot backup. Exactly one terminal event matches the
aborted startup summary. This attempt is a failure, not a completed match.
[Startup outcome](../runs/sixty-round-live-20260905T020306Z/startup-terminal-validation.log),
[Actual census and launcher output](../runs/sixty-round-live-20260905T020306Z/launcher.log).

The installed setup code compares all map capacity bounds with its database
row. Forcing Tiny's maximum major count to two caused a later UI refresh to
restore its default four-player roster. The correction preserves native map
capacity and closes extra seats separately. The actual Ready controls now
refresh native setup, enforce two seats, refresh again, verify stability and
launch synchronously. Callback registration is only setup evidence; the final
post-load engine census remains the authority. Sixty-one focused checks passed,
including deferred refresh, refused closure and repeated roster reset.
[Roster regression checks](../runs/sixty-round-development-20260905/roster-focused.log).

## Second live attempt and remaining blockers

`minimax60-20260905T022327Z` at `e57c3e1` passed fresh startup in667.60seconds:
the actual generated game had exactly two majors, both human after reflag,
and mod0.3.4 passed the live capability check. Both MiniMax-M3 agents performed
accepted actions. The run completed12 ordered seat turns (six rounds), then
stopped in turn7 recovery after eight sweeps. Cleanup completed without errors;
one MATCH_END matches summary, with the unreleased P0/turn7 lease explicit.
Zero watchdog violations were recorded. Eight exact movement rows were admitted
under the unchanged allowance. This is a failed60-round attempt.
[Startup census](../runs/minimax60-20260905T022327Z-startup/major-census.json),
[Event/summary analysis](../runs/sixty-round-live-20260905T022327Z/analysis.json).

Completed turns averaged56.28seconds (median55.70), using86 requests; total
usage including the unfinished turn was98 requests. First accepted actions
averaged18.02seconds. The historical aborted six-seat run averaged51.99seconds
per turn and21.91seconds to first action. Different game states and unequal
samples prevent a controlled speedup claim; overall turns were not faster in
this attempt. One boost popup was automatically observed hidden afterward.

The turn7 screenshot showed a blocking scout-advice popup. The native seat
switch was intentional; it did not establish a prematurely ended lease.
This release build has no TutorialContinue action and ignores Return for that
advisor, so the new handler targets the observed first OK button with pinned
window identity and geometry, then checks visibility. A one-click diagnostic after MATCH_END observed the panel become hidden and
the engine lease change fromP0/turn7inactive toP1/turn7active. The stopped run
remains failed; this is separate host proof of the blocker and dismissal.
[Postmortem dismissal and engine transition](../runs/sixty-round-live-20260905T022327Z/advisor-postmortem-close.json).
[Blocking panel](../runs/sixty-round-live-20260905T022327Z/turn-seven-recovery.png),
[Native advisor probes](../runs/sixty-round-live-20260905T022327Z/advisor-native-probe.log).

Production requests were also being labeled rejected before the engine applied
them, and GameCore queue reads falsely appeared empty. Subsequent InGame reads
now confirm the requested hash before acceptance and expose actual city queues.
A read-only host probe returned Egypt'sSLINGER and Sumeria'sSCOUT correctly.
The standalone resolver uses the same bounded verifier and disconnects on error.
[Actual queue read](../runs/sixty-round-live-20260905T022327Z/corrected-city-queue-read.log).

The unit-restore helper had waited for missing output on every call. The next
mod0.3.5 protocol emits checked owner/full-ID receipts plus a completion marker.
It preserves once-per-turn restoration and refuses older mod versions before
play. This removes a known wait path; its live speed effect remains unmeasured.
Movement action receipts can still show the pre-operation position; truthful
post-move projection remains a follow-up.
[Restore evidence](../runs/sixty-round-development-20260905/restore-speed-evidence/).

## Twenty-one-seat stop and silent native hooks

The next fresh run, `minimax60-20260905T031207Z`, used commit `311f18a`
and mod 0.3.5. It completed 21 correctly ordered seat turns (ten complete rounds
plus player zero's turn eleven), then stopped during player one's turn eleven.
The failure was `non-ledger row in ledger dump: PUPPET_ACTIVE|true`.
Its one `MATCH_END` matches `summary.json`; cleanup completed, with an unfinished
lease retained in the terminal evidence. This is a failed sixty-round attempt.

There were 150 provider requests, zero watchdog violations, five observed popup
dismissals, and two exact own-unit movement rows admitted in 21 allowance audits.
Completed turns averaged 53.105 seconds, median 40.453; the first accepted action
averaged 16.737 seconds. These are measurements from a failed run, not acceptance.
[Event accounting and exact allowance rows](../runs/sixty-round-live-20260905T031207Z/analysis.json)
and [launch custody](../runs/sixty-round-live-20260905T031207Z/launch.json) are preserved.

The native start hook printed a lease status onto the tuner response channel.
Review also found the native deactivation hook called the explicit release RPC,
which printed a status and generic terminator. Mod 0.3.6 keeps both native hooks
silent while explicit status/release requests retain their responses. A real-Lua
engine fixture reproduces deactivation, activation and duplicate start callbacks
inside the ledger response window and verifies only the requested ledger
terminator appears. The focused gate passed 54 tests:
[silent hook regression log](../runs/sixty-round-development-20260905/silent-hooks-reviewed-focused.log).
This proves the local regression; another fresh live run is still required.

## Strategic autopilot validation lane

The user's updated design moves routine decision work into the controller.
The opt-in configuration is `configs/live-hotseat-strategic-minimax2-060.yaml`:
both strategic models remain MiniMax-M3, with the same provider, request caps,
sixty-round target and `declare_own_endpath_drift=true` allowance.
The controller supplies projected state and accepts one validated JSON directive
on the initial turn, five-turn cadence, or meaningful state-change trigger.
Quiet turns use no provider request. Tactical overrides expire after one turn.

Scouting is a bounded heuristic over known adjacent terrain, with reproducible
selection seeds and component scores. It is not a legal-move oracle or a
calibrated outcome forecast. Actual moves go through the existing audited
facade; engine rejection and observed positions remain authoritative. The
browser distinguishes model strategy updates from autopilot execution and
shows alternatives alongside observed action results. Its pre-live Chrome
checks use synthetic records only:
[browser review](../runs/sixty-round-development-20260905/strategic-review/civ-strategy-browser-review-chrome-final.log).

This mode is explicitly fresh-only until directive and trigger history can be
restored through the checkpoint schema. It does not imply full-game victory,
stronger strategic play, or the future multi-turn technology/civic dependency
forecast. The launch command below retains the approved limits:

```bash
PYTHONPATH=src PYTHONUNBUFFERED=1 DISPLAY=:1 \
  /home/alexk/documents/civ-arena/.venv/bin/python scripts/live_zero_touch.py \
  --session arch1 --kill-first \
  --config configs/live-hotseat-strategic-minimax2-060.yaml --rounds 60 \
  --startup-timeout 2700 --match-timeout 7200 --agent-turn-timeout 600 \
  --recovery-timeout 180 --recovery-sweeps 8 \
  --run-id UNIQUE_FRESH_ID --runs-root /home/alexk/civ-arena-reliability-20260904/runs
```

The local launch helper records the commit, source tree, config/mod hashes and
unique ID, and preserves a run-specific installed-mod backup before copying the
reviewed mod. Startup separately preserves replaced save slots. Do not launch
with an active tuner client or reuse an existing run ID.

## Strategic release gate and provider preflight

The full local pytest gate at `f655412` collected 1,013 tests and completed each
exactly once: **1,012 passed, one skipped, zero failures/errors** in four
disjoint file groups with complete logs. Ruff then passed after wrapping one
new test signature; the affected dashboard suite passed 25 tests. The only
post-gate code-file change was that whitespace-only test signature; runtime
source was unchanged. [Full gate summary](../runs/sixty-round-development-20260905/strategic-release/summary.json),
[Ruff](../runs/sixty-round-development-20260905/strategic-release-ruff-final.log),
and [affected test check](../runs/sixty-round-development-20260905/strategy-dashboard-format-focused.log).

Provider-only probes made three total requests: the first exposed the tactical
schema/validator mismatch; after correction, one request returned a valid JSON
directive in 2.272 seconds. A separate request using explicit live opening-freeze
metadata returned a valid founding order and warrior move in 2.732 seconds.
No probe executed game actions. These requests are separate from match spend.
[Final provider probe](../runs/sixty-round-development-20260905/strategic-provider-probe-live-freeze.json).

The visible physical-monitor dashboard was reloaded at 04:12 UTC. It correctly
showed the old stopped run and no strategy records; this is actual dashboard
proof for retained data, not proof of live strategic execution. The current
reload reported no browser errors or failed requests.
[Browser evidence](../runs/sixty-round-development-20260905/visible-strategic-browser/).

## First strategic live attempt: outgoing-seat handoff race

Fresh run `minimax60-20260905T041339Z` used commit `8edc0d9`, mod 0.3.6,
and the strategic configuration. Startup passed in 667.44 seconds. Player zero
completed one seat turn in 23.118 seconds using one model request; player one
never received an agent turn. The run stopped with one watchdog violation:
a settler despawned and a city was founded without a founding command. The
single `MATCH_END` matches the failed summary and cleanup completed. This is
zero complete rounds, not a speed or operational-reliability acceptance.
[Exact event accounting](../runs/sixty-round-live-20260905T041339Z/analysis.json).

The actual directive contained two move overrides. The warrior's subsequent
position changed; the settler's accepted submission left its position unchanged.
Completeness repair then submitted sleep for the settler. Before handoff the
sealed digest still showed two remaining settler moves. The driver switched
local control in one RPC and called `FinishAllMoves` in the next, leaving the
outgoing nonlocal seat an execution window. A read-only engine probe confirmed
that after switching, outgoing player zero was nonhuman and player one human.
The shipped UI and actual enum/hash bindings confirm that the sleep fallback
uses the correct operation hash; no founding command was present.
[Wire and engine diagnostic](../runs/sixty-round-live-20260905T041339Z/poststop-operations.log)
and [primary UI source review](../runs/sixty-round-live-20260905T041339Z/unit-operation-source-review.log).

A separate postmortem probe tested freezing before switching in the same
GameCore chunk. Player one's untouched frozen settler could not found at zero
movement; after a once-only restore to two moves it could. The probe froze and
verified both outgoing units before switching, observed player zero's turn-two
lease, and confirmed the settler remained with no player-one city. It retained
an unrelated native research-change row in the ledger rather than hiding it.
This supports the prospective handoff change for this specific case, not a
repaired match or broad absence of native AI behavior.
[Full postmortem steps and observations](../runs/sixty-round-live-20260905T041339Z/poststop-freeze-handoff.json).

Actual browser proof shows the one-seat failed outcome and its two tactical
orders. Neither decision had scouting candidates, so actual live probability-
chart proof remains unavailable; synthetic chart proof remains separate.
[Browser records and screenshots](../runs/sixty-round-live-20260905T041339Z/browser-proof/).

## Guarded handoff release

Mod 0.3.7 and the adapter now execute one guarded GameCore handoff: validate
outgoing lease/turn/local identity and the next armed living major, freeze and
verify outgoing movement, then switch local control. The rolling snapshot is
preserved so unrelated drift remains visible. The explicit receipt is bound to
both seats and the turn; helper execution is bounded to five seconds, followed
by observed lease-release polling. Function-backed capability preflight rejects
an incomplete mod before play. Next-seat native `IsHuman()` is not required
before switching because that value follows local control in this build.

Full integrated pytest at `e7e6b3e` passed **1,043 tests**, with one skipped and
zero failures/errors. All 1,044 collected identities ran exactly once in four
disjoint groups, with complete logs. Ruff passed. The integrated code matches
the independently reviewed isolated commit `3f3a11e`.
[Release summary](../runs/sixty-round-development-20260905/guarded-handoff-release/summary.json),
[Ruff](../runs/sixty-round-development-20260905/guarded-handoff-release-ruff.log),
and [review and focused evidence](../runs/sixty-round-live-20260905T041339Z/guarded-handoff-evidence/).
The following live attempt exercises this change but does not complete the
sixty-round target.

## Second strategic live attempt: directive response rejection

Fresh run `minimax60-20260905T044417Z` used clean commit `30adf034` and mod
0.3.7. Startup passed in 667.912 seconds. It completed 31 correctly ordered
seat turns: fifteen complete rounds followed by player zero's turn sixteen.
Player one's turn sixteen stopped at the strategic response validator. There
were nine provider requests (four for player zero, five for player one), zero
watchdog violations, and six observed informational-popup dismissals. The
single `MATCH_END` matches the failed summary; cleanup completed with no errors.
The player-one turn-sixteen lease remains explicitly unreleased. The final
digest is cached, not a new shutdown observation.
[Retained event accounting](../runs/sixty-round-live-20260905T044417Z/final-analysis.json).

The error was `model must submit exactly one complete strategic directive`.
That validator rejected several possible response shapes, including valid
tool calls accompanied by prose. The failing response's block shape was not
retained, so this run cannot establish which shape caused the rejection.
No continuation or successful sixty-round claim is made for this run.

Completed seat turns had a median of 9.968 seconds and a mean of 11.394 seconds.
Twelve of those turns contained no accepted game action, so these timings are
not a uniform measure of productive play. Eight model requests belong to
completed turns; the ninth belongs to the failed active turn. The configured
own-unit movement allowance admitted ten exact rows, retained across all 31
completed-turn allowance audits. Foreign-unit and other drift remains outside
the allowance.

Actual physical-monitor browser evidence shows both model directives and a
quiet player-one turn with six scouting candidates, a selected action and an
observed coordinate change, using zero model requests for that turn. Selection
weights are not calibrated outcome confidence. The current browser reload had
zero console/page errors and zero failed requests.
[Browser records and screenshots](../runs/sixty-round-live-20260905T044417Z/browser-proof/).

The independently reviewed scouting follow-up also corrects a quiet-turn
stall: an explicitly assigned scouting role supersedes old fortification,
while a current tactical hold, unassigned standing orders and movement guards
still apply. Its two-turn controller regression passed with zero additional
model requests on the quiet turn.
[Standing-order contract](scouting-standing-intent.md).

## Strategic response release and current preflight

At clean commit `b54c9ec`, the full local gate collected 1,069 identities and ran
each exactly once: **1,068 passed, one skipped, zero failures/errors**. Ruff
passed. The integrated response/client files match independently reviewed
`96e32ca`; scouting matches independently reviewed `2ef5407`.
[Full gate](../runs/sixty-round-development-20260905/strategic-response-release/summary.json),
[Ruff](../runs/sixty-round-development-20260905/strategic-response-release-ruff.log),
[review binding](../runs/sixty-round-development-20260905/strategic-response-review-binding.log).

A provider-only preflight on the retained opening-state fixture used two
MiniMax-M3 requests in 5.260 seconds. Despite named-tool selection, the first
reply contained one text block and zero tools. The single bounded format retry
returned one valid directive tool call. Both exact curated inputs and response
shapes are retained. No game or facade action ran. This proves one actual repair
case; it does not establish universal provider format compliance or reconstruct
the earlier turn-sixteen response.
[Provider evidence](../runs/sixty-round-development-20260905/strategic-response-provider-probe.json).

The fresh thirty-round acceptance configuration is
`configs/live-hotseat-strategic-minimax2-030.yaml`. It preserves the sixty-round
provider, request, seed and allowance settings, changing only the match name and
round limit. After sixty-round validation, run two separate fresh launches using
that configuration and `--rounds 30`, with distinct run IDs and the same reviewed
implementation. Both must independently satisfy the structural and live gates;
one longer run cannot substitute for two fresh starts.

## Third strategic live attempt: unaccounted population change

Fresh run `minimax60-20260905T052156Z` used clean commit `503478c` and mod
0.3.7. It completed 42 correctly ordered, released seat turns: 21 rounds.
Fourteen MiniMax-M3 requests were recorded, seven per seat. The new response
boundary handled one valid directive with auxiliary prose and three text-only
responses that each succeeded on the single format retry. All fourteen saved
request contexts match their SHA256 and character counts; the largest was
5,471 characters. Nine informational popups were observed dismissed in 132
checks. There were no recovery input attempts.
[Independent final audit](../runs/60-round-live-20260905T052156Z/final-independent-audit.json).

The run failed with one watchdog violation: city `c1:65536` grew from population
three to four. Although reported during final turn accounting and labeled
`recovery`, the growth was already visible during the active player-one turn
21, immediately after scout `u1:327683` moved and before any handoff. The
single `MATCH_END` matches the failed summary, the coordinator lease is closed,
and cleanup completed without errors. A later read-only postmortem observed
the engine's next player-zero turn-22 lease; this does not change the summary's
explicitly cached final observation or make the failed match complete.
[Exact chronology](../runs/60-round-live-20260905T052156Z/growth-boundary-summary.log).

The installed rules include a tribal-village reward that adds one population to
the nearest city. The engine exposes a reward event identifying player, unit,
reward type and subtype. That is a plausible explanation for this move's growth,
but the failed run did not retain the causal event. An exact command-scoped
reward receipt is required before treating such growth as a commanded effect;
there is no new population or gold drift allowance.
[Installed source evidence](../runs/60-round-live-20260905T052156Z/goody-native-source.log),
[read-only event capability probe](../runs/60-round-live-20260905T052156Z/poststop-reward-capabilities.json).

The existing own-unit movement allowance admitted 90 exact `unit.moves` rows,
34 for player zero and 56 for player one, across all 42 completed-turn audits.
The unmatched population row remains a violation. Accepted gameplay submissions
were 51 and 104 respectively; these counts are not proof of movement or useful
strategy. Browser proof captured both directives and quiet-turn candidate graphs
at six completed seats, including an observed player-one movement and an
unchanged player-zero position. The scouting follow-up now distinguishes those
outcomes and temporarily avoids confirmed ineffective destinations.
[Scouting feedback contract](scouting-nonprogress-feedback.md),
[actual browser evidence](../runs/60-round-live-20260905T052156Z/browser-proof/).

## Native reward and scouting follow-up validation

At `2b59d2b`, the complete local pytest gate passed 1,124 tests with zero failures
or errors and one optional Neo4j integration test skipped. All 1,125 collected
test identities ran exactly once across four disjoint file groups. Ruff passed.
This gate includes completed-turn scouting feedback and command-bound population
receipts; it predates the Sumeria camp eligibility correction below and is not
live acceptance evidence.
[Captured gate](../runs/sixty-round-development-20260905/native-reward-final-release/summary.json),
[Ruff](../runs/sixty-round-development-20260905/native-reward-final-release-ruff.log).

A separate current MiniMax-M3 preflight returned one validated directive in one
request (4.48 seconds), using retained projected observations and performing no
game actions. This proves that request only.
[Provider probe](../runs/sixty-round-development-20260905/native-reward-provider-probe.json).

Installed rules and a read-only GameCore probe confirm that the configured
Sumerian seat receives tribal-village rewards when clearing barbarian camps.
A guard limited to an initial `IMPROVEMENT_GOODY_HUT` therefore omits an eligible
reward source. The correction must bind camp eligibility to the current
civilization's exact active trait/modifier/argument chain while preserving the
native event and population-delta checks. This finding does not identify the
historical turn-21 reward, whose event was not retained.
[Installed rule source](../runs/60-round-live-20260905T052156Z/civ-sumeria-camp-exact-source.log),
[GameCore API and active rules](../runs/60-round-live-20260905T052156Z/poststop-camp-api.log),
[Receipt contract](live-goody-reward-receipts.md).

## September 6 resumed live attempt and subtype evidence

The September 5 `minimax60-20260905T063949Z` launch stopped at display preflight,
with all physical outputs disconnected and zero seat turns. Its startup terminal
and save/mod backups were preserved. On September 6, DP-2 was verified connected
at 3440x1440, the old game was observed at turn 22, and no driver/tuner client was
active. The provider-only preflight returned a validated MiniMax-M3 directive in
two requests (3.55 seconds), without game actions.

Fresh `minimax60-20260906T172847Z` used clean `b1af708`, source tree `35bf8e1`,
mod 0.3.9, and the unchanged sixty-round strategic configuration. The bound full
local gate had 1,142 passing tests and one optional skip; Ruff passed. Startup
completed in 666.926 seconds within the 2,700-second budget. The game completed
50 correctly ordered, released seat turns (25 rounds), then stopped with one
population `3 -> 4` watchdog row for `c1:65536`. This run failed acceptance.

There is exactly one final `MATCH_END`, equal to the failed summary. Cleanup
completed with no errors; the coordinator lease is closed and final observations
are explicitly cached. No timeout or observed deadline excess was recorded.
The controller used 14 MiniMax-M3 requests, seven per seat. All 14 saved request
contexts match their hashes and lengths (maximum 5,281 characters). Both seats
submitted accepted game actions. The configured own-unit movement allowance
admitted 65 exact in-policy rows across all 50 turn audits, with no other drift
silently admitted. Thirteen informational popup dismissals were recorded.
[Independent final audit](../runs/60-round-live-20260906T172847Z/independent-audit-compact.json).

Unlike the prior failure, this run retained a native reward event at P1 turn 25:
`GoodyHutReward` type `1892398955`, subtype `1038837136`, classified
`unsupported_or_unmatched`. A subsequent read-only host probe established that
the subtype is `DB.MakeHash('GOODYHUT_ADD_POP')`, while direct numeric lookup of
that hash in `GameInfo.GoodyHutSubTypes` is absent. The row resolves by its name
or ordinal 16. The prospective 0.3.10 correction binds the event hash to the named
active row; all other causal and population checks remain. The historical failed
window lacks a complete causal receipt and is not reclassified.
[Host binding probe](../runs/60-round-live-20260906T172847Z/poststop-native-subtype.log),
[Native terminal audit](../runs/60-round-live-20260906T172847Z/native-reward-terminal.json),
[Receipt correction](live-goody-reward-receipts.md).

Actual browser evidence at three rounds shows both opening directives and quiet
turn graphs, zero additional requests on the four captured quiet seats, and both
observed displacement and honestly unconfirmed movement. Browser console, page,
and request failure counts were zero in that capture. A separate clean hex-map
prototype renders one retained player-scoped packet with source hashes and
explicit missing cells; its old normalized terrain labels cannot recover lost
native tundra/snow. This is a visual prototype, not model-vision benefit proof.
[Browser evidence](../runs/60-round-live-20260906T172847Z/browser-proof/verified-browser-checkpoint.md),
[Observation map prototype](../runs/60-round-live-20260906T172847Z/observation-map-prototype/index.html).

The opening audit confirms both models explicitly preferred Pottery, Mining, and
Animal Husbandry in that order, and Monument first, despite different terrain
packets. It establishes neither an optimal opening nor why the models shared
those preferences. Resources, yields, freshwater, and dependency/effect facts
were missing. The curator and native-terrain follow-ups address explicit packet
states and terrain identity; forward optimization and calibrated confidence
remain separate work.
[Exact opening inputs and choices](../runs/60-round-live-20260906T172847Z/opening-policy-audit.json),
[Context packet](context-decision-packet.md).

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
