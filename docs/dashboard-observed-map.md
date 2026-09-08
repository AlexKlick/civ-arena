# Observed atlas in the match dashboard

The dashboard now includes the reviewed zoomable observed atlas as a read-only,
same-origin document. It reads recorded model observation packets from the chosen
run's `events.jsonl`; there is no new engine, provider, desktop, or live-state
connection. The existing action journal and scouting panels remain available.
This candidate starts from `76f4dce`, imports the reviewed minimap source as
`8b15cdae` (same tree as `8cb8ab5`), and adds this dashboard bridge separately.

## User flow

Choose a match and engine turn using the existing dashboard controls. The
Observed atlas section offers Player 0, Player 1, or explicit spectator scope,
Refresh map snapshot, and Open large atlas. Selecting spectator opens the combined
view directly. The embedded atlas retains its hex/unit/city selection, observed
production queue and recorded action-choice inspector, pan/zoom controls, and
small overview navigator. At narrow widths its detail panel stacks below the map.

A changed match, selected turn, or map perspective loads a new snapshot. Regular
two-second action-journal polling does not reload the same map URL or reset zoom.
Follow latest turn still moves to a new map when the selected engine turn changes.
For new packets within the same turn, use Refresh map snapshot. A run/seat with
no valid recorded observation packets shows a visible unavailable-map response;
the UI does not synthesize actors or terrain from incomplete tool-call sequences.
The current dashboard seat picker exposes its two established seats; the direct
route accepts explicitly identified observed seats as well.

The request metric and seat cards now say **Provider requests**. Existing
`provider_request.posts_sent` totals are cumulative reported POST attempts and
may include generation, count-token requests, and retries. No missing breakdown
is inferred from legacy logs. These totals are not model-decision counts, and
provider-reported usage tokens are not interchangeable with count-endpoint tokens.

## Read and identity boundaries

`/map?id=RUN&player=N&turn=T` serves a player export; replace `player=N` with
`spectator=1` for an explicit multi-perspective export. Scope and turn are required,
mutually exclusive scope selectors and duplicate/unknown query fields are refused,
and no raw arbitrary-file endpoint is added. The existing run-name, direct-child,
regular-file, no-symlink, loopback binding/Host, and read-only HTTP controls apply.
A missing, oversized, malformed, or inconsistent source returns an unavailable
map, not an empty successful world model.

The bridge reads a bounded event snapshot (existing 32 MiB / 100,000-record limits).
Only complete newline-terminated records contribute. A trailing unfinished
record is excluded and visibly disclosed; malformed complete records, duplicate
JSON keys, non-finite numbers, invalid sequence order and missing MATCH_START fail.
A selected engine turn establishes an **event-sequence prefix** ending before the
first observed later-turn record. A stale old-turn label after that boundary cannot
pull future information back into the selected map. Each retained packet then
uses the existing receipt validation and same-seat graph cutoff: the graph must
be at or before that packet in both sequence and turn.

Packets are extracted only from actual `strategy_request` audit contexts using
the existing controller-context marker. The bridge verifies the original context
length/hash and projected-state equality against the bound request event before
rendering. It does not depend on a monitoring process or a `model-packets` support
folder, and it does not reverse-engineer missing complete state from other calls.

Player-only HTML contains no other seat's packet contents or graph. Changing
another seat's private packet leaves the selected player's derivative unchanged
when the public sequence/turn boundary is unchanged. Spectator HTML intentionally
contains multiple private perspectives, and its internal player selector is only
a display filter. It is an operator view, not player authorization. Seats may
have different latest packet turns; no simultaneous omniscient state is asserted.

## Source versus display custody

Raw packet/event binding is checked before display redaction. The generic
dashboard redactor's list/depth truncation is not applied to map records: all
actors and terrain rows survive. Strings and sensitive fields are still redacted;
if redaction corrupts a critical coordinate, the map refuses instead of dropping
or relocating that observation. Redacted derivative data receives its own digest.
`dashboard_source.raw_player_scoped_bundle_sha256` separately identifies the raw
validated, player-scoped bundle. Receipt hashes refer to original source records,
not a claim that redacted displayed text equals the raw record.

The generated map pins its one static script with a CSP hash and uses DOM
`textContent` for source labels. Its response permits framing only by the same
origin and permits no network fetch. The outer dashboard keeps its existing CSP;
only the map response permits same-origin framing. An aborted navigation may
cancel a large map response; the server stops writing to that disconnected client.
The existing servers on 8788 and 8791 were preserved throughout this work.

## Meaning and limits

The inherited atlas contract remains in [observed-minimap-preview.md](observed-minimap-preview.md).
Tiles accumulate from supplied, selected model-context observations, including
remembered terrain. Brightness indicates packet receipt age, not current native
visibility or when the tile was actually seen. Blank space is unsupplied; observed
extents are not map bounds. Wrapping, true world geometry, full explored coverage,
current ownership, legality, expansion value and calibrated outcomes remain
unknown when not supplied. Actors come from the selected packet only. Historical
audit paths do not move their observed markers or prove successful displacement.

For a continuous map independent of model calls, a later observation-feed change
must supply explicit revision/coverage/fog semantics at curated refresh time. This
candidate deliberately does not change game or model cadence. A larger journal
that exceeds the read/artifact limits is unavailable; that does not abort a game
or reduce a model briefing. The tested stopped run has 48 packets and 240 combined
accumulated coordinates, not a full-world or map-scale performance proof.

## Readability foundation (2026-09-07)

The atlas key, colours, elevation marks, unit glyphs and observed borders are
documented in [observed-minimap-preview.md](observed-minimap-preview.md) and
proven in [atlas-readability-m1.md](atlas-readability-m1.md). Dashboard-specific
points: each snapshot now carries the packet's public majors roster as
`public_players` (or `null` when unsupplied), which survives display redaction
row for row; the atlas seat colours are the match room's `--gold`/`--teal`
tokens, pinned by a drift-guard test against the shared palette; the atlas
iframe chrome uses the match-room tokens instead of literal colours. Foreign
units and cities in retained packets carry no `owner_id` or `name` because the
curator's foreign field lists omit them, so foreign actors are drawn "owner not
supplied" — adding those fields is a model-context change and is deliberately
not part of the viewer work. Real retained runs contain tile owners outside the
two-major roster (players 2, 3 and 6 in `minimax100-20260907T183425Z`); they
draw non-major borders and are never labelled as civilizations.

## Research block (M2)

Each snapshot also carries the packet's own `research` context — researching,
researched, the recorded options and the `option_sources.research` token, or
`null` when the packet supplied none of them. The atlas aside prints one block
per displayed seat; option chips appear only when that packet recorded the
options as `observed`, otherwise the block states that they were not requested.
Like `public_players`, research rows survive display redaction row for row: the
generic list/depth cap is not applied, so a 50-entry option list stays 50
entries with its strings redacted and its costs intact. This is retained packet
context, not live research state, and the two seats' blocks can be as-of
different turns. The block-level contract is in
[observed-minimap-preview.md](observed-minimap-preview.md).

## Verified findings

- Repository proof: **75 focused tests passed, zero failed/skipped**, in 1.22s;
  Ruff and both JavaScript syntax checks passed. Gate covers dashboard routes,
  new causal/privacy/redaction cases, reviewed minimap contracts and compact-row
  compatibility. Full logs: `/tmp/civ-dashboard-map-focused-final.log`,
  `/tmp/civ-dashboard-map-ruff-final.log`, and `*-js-final.log`.
- Browser proof: **9 groups passed** on an isolated ephemeral loopback server,
  using `/usr/bin/python3` Playwright and headless `/usr/bin/google-chrome`.
  Desktop 1440×1080 and mobile 390×844 checks exercised the actual dashboard
  iframe/CSP, perspective and turn selection, zoom across a real journal poll,
  explicit refresh/open-large link, partial-tail disclosure and unavailable-data
  UI. No external requests or unexpected console/page errors were observed.
  The intentional unavailable-player probe produced one expected HTTP422 console
  message, recorded separately. This is not physical-browser or native-game proof.
- Final browser evidence is `runs/dashboard-map-evidence-final2-20260906/`:
  `browser-summary.json`, `source-manifest.json`, `checks.json`, captured HTML,
  screenshots, full logs and browser script. Its copied event fixture was appended
  with a deliberate partial record for the active-read test; the manifest records
  that mutation separately. The unmodified initial copy remains under
  `runs/dashboard-map-evidence-20260906/runs/`.
- Earlier attempt logs are preserved. The second browser attempt asserted the
  spectator selector before the new iframe document loaded; the corrected probe
  waits for the new document's value. This test-harness failure also exposed a
  harmless disconnect traceback, now handled without a second HTTP response.

## Follow-up probes

Independent exact-commit review should check all scope and causal cutoffs, the
redacted-display/raw-source distinction, and map-only CSP framing. Broader combined
release gates belong to the parent integration checkout. Evaluate an explicit
observation feed before claiming continuously current map coverage.

## Blocked checks

No environment blocker affected this offline/browser slice. Existing live servers
were not replaced. Native engine, provider, physical display, public exposure,
full-world fog correctness and operational acceptance were outside this scope.

## Reproduce

Run from the isolated candidate, choosing a fresh ephemeral port so existing
services remain unchanged:

```bash
PYTHONPATH=src /home/alexk/documents/civ-arena/.venv/bin/python -m civ_arena.dashboard \
  --runs-root runs/dashboard-map-evidence-20260906/runs --port 0
```

Open the printed loopback URL. Stop that candidate process when finished; do not
stop the separately owned 8788 dashboard or 8791 prototype. The retained browser
script creates a copied run and its own ephemeral server, then shuts it down;
choose a fresh output directory in the script before repeating it so prior
evidence is not overwritten.
