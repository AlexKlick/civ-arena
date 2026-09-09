# Spectator capture — the operator's game as recorded data

Lane: `feat/spectator-capture-20260907` (worktree
`/home/alexk/civ-arena-spectator-capture-20260907`, based on `fdeaae2`).

## What this is

`--phase spectate`: the harness attaches to a live Civ VI game the
OPERATOR plays at the keyboard (seat 0) against the engine's own AI, and
ONLY watches — polls, observes, and the mod's lease-free ambient-window
recorder commands. Never: puppet arming (a lease would freeze the
human's units), local-player switch, blocker housekeeping, popup
dismissal, desktop input. The run dir becomes the first
human-vs-typical-AI corpus for the distillation initiative (post-hoc
reasoning reconstruction; graph memory; curated context).

## Verified findings (all game-free, `--fake` / FakeTunerServer)

- The lease-free observation surface is sufficient: ambient windows
  (`Begin/EndAmbientWindow` + `DumpAmbient`) give per-player per-turn
  deltas (unit pos/moves/damage/exists, city pop/exists, gold,
  researching); the omniscient observe reads (OVERVIEW/UNITS/CITIES)
  give the full board including the ENGINE AI's pieces; `Digest()`
  brackets every census for drift detection. No mod change was needed.
- Turn boundaries are readable from the unconditional hook ring
  (`T|HOOK_ENTER/HOOK_DEACT|pid`) corroborated by `TURN_ACTIVE`;
  attaching mid-human-turn records that partial turn as round 1
  (`window="attach"`); attaching between turns discards the stale ring
  history (`attach_history_discarded` audit) and waits for the next
  fresh HOOK_ENTER.
- The rehearsal caught one real gap before it could ship: the replay
  structural certificate did not cross-check the summary's round count
  against the log — a coordinated summary+MATCH_END edit could hide a
  dropped round. Fixed (rehearsal test pins it).
- The engine-AI hang risk discussed at planning: downgraded to
  "unproven, likely harness-artifact" — the one documented hang (M17)
  occurred in harness-DRIVEN games with a known forced-end-turn freeze
  mechanism; spectate mode issues zero turn-affecting commands, so that
  mechanism cannot fire. Residual risk = the game crashing on its own;
  heartbeat + per-round events mean data to that point is durable.

## Resulting implementation

- `config.py`: `SpectateSpec` + `spectate:` block (operator attribution
  key, human_seat, observed_players, snapshot_scope, AUDIT-ONLY
  budgets); a spectate config REQUIRES an empty agents roster and the
  firetuner adapter (fail loud). `LLMSpec.wire_log` opt-in.
- `arena/events.py`: `SPECTATOR_SNAPSHOT`, `HUMAN_TURN_START`,
  `HUMAN_TURN_END` kinds. Ambient rows ride INSIDE these payloads —
  never as top-level `AMBIENT` events (replay compares those against a
  sim replay; guaranteed divergence).
- `game/civ6/spectate_capture.py`: `TurnWatch` (wrap-safe ring cursor,
  gaps reported never fabricated), `SpectatorCensus` (digest-bracketed
  census, one drift retry, 256 KB payload cap preserving
  digests/counts), `SpectateLimits`.
- `game/civ6/live_driver.py`: `phase_spectate` — attach, per-round:
  START → snapshot → (human plays; heartbeat cadence) → END with the
  human's in-turn ambient delta; overrun audits never act; SIGTERM /
  match-timeout produce a clean aborted MATCH_END; cleanup is
  disconnect-only. The input-free proof is threefold: a monkeypatched
  Controller that fails on construction, a wire-level
  forbidden-substring scan over every received command, and a static
  source pin on the phase function (no `set_puppet` /
  `activate_human_seat` / `request_end_turn` / `_resolve_blockers` /
  `_dismiss_popups` / `write_raw` / `adapter.act`).
- `game/civ6/validate_run.py`: `validate_spectate` — zero action kinds
  REQUIRED, strict START/SNAPSHOT/END interleave, digest bracket on
  every snapshot, `command_census.game_writes == 0`, identity +
  mod_sha256; `require_live` gates the clean-commit + real-engine
  checks (fake rehearsals pass with `--allow-fake`).
- `replay.py`: spectate short-circuit BEFORE Arena construction (zero
  driven tool calls would fabricate a synthetic sim match); structural
  certificate incl. the summary round-count cross-check; CLI exit 0/4.
- `agents/llm/client.py` + `agents/llm/wire_log.py`: `on_attempt` hook
  (full request/response record per counted attempt; `on_post`
  semantics untouched); `CostLedger` (`llm_costs.jsonl`, always-on,
  tokens+latency per attempt) and `WireLog` (`llm-wire/<agent>.jsonl`,
  opt-in, 0600, redaction sweep with the live env key, headers never
  recorded). `_wire_client_sinks` composes spend + audit + ledgers and
  replaces the hotseat's `on_post` clobber; single-seat dispatch gets
  the same wiring (it never had any).
- `game/civ6/fake_tuner_server.py`: FakeMod `spectate=` poll-driven
  engine timeline (driver stays pure), real ambient windows behind
  `ambient_diffs=True` (default preserves every existing fixture),
  `mutate_during_census` drift knob, unit-movement refresh at the new
  turn.
- `configs/live-spectator-human-001.yaml`: the authored live config.

## Follow-up probes (first live game)

- Babysit the first two rounds: heartbeat cadence, snapshot payload
  sizes (256 KB cap behavior on a real duel board), `census_consistent`
  rate on a live human actively playing (expect occasional honest
  `consistent:false` while the human moves mid-census), trace-ring wrap
  on 4+ major games.
- `snapshot_scope: census` as the long-game default if `full` payloads
  grow past the cap on big boards.
- The human's in-turn reasoning channel is OUT of scope here (post-hoc
  reconstruction lane); a think-aloud capture can ride
  `HUMAN_TURN_START/END` payloads later without schema change.

## Blocked checks (until a live game runs)

- Real-game attach timing (young-X tuner binding, single-client wire —
  see the pre-launch checklist in the lane plan), real engine turn
  durations vs `poll_s` cadence, real popup behavior while the human
  plays (the harness deliberately never touches them), MATCH_END on a
  natural game end (victory/defeat) vs `--turns` cap vs SIGTERM.

## Out of scope (named follow-on lane)

"Human-seat hotseat" — `policy: human` roster seat for user-vs-LLM
games (hotseat seat-type split, no arming for the human seat,
lease-free local-switch wait, human-scale budgets, no key sweeps during
the human turn). This lane deliberately touches none of that machinery.

## M4: the spectator WORLD block and the spectator_world audit

M4 widens capture onto BOTH carrier phases and adds a spectator-omniscient
world doc consumed only by viewer routes (`game/civ6/world_capture.py`):

- hotseat (`match.spectator_capture: true`): `phase_dispatch_hotseat`
  emits a HEARTBEAT `audit="spectator_world"`
  `visibility_scope="spectator"` once after the initial-attach step
  (baseline, `after_seat=-1`) and once immediately after every
  `completed_seat_turn` audit, under `HotseatLimits.spectator` (20 s);
  every failure is audited as `spectator_world_failed` (redacted) and the
  match continues. NO new EVENT_KINDS; the world NEVER enters model
  packets (`arena/visibility.py` is the no-leak boundary).
- spectate: `SpectatorCensus.snapshot()` gains an ADDITIVE `world` key at
  `snapshot_scope: full` — roster + owned tiles through the READ
  transport ONLY (palette needs the write transport; the phase's
  never-acts pin forbids writes), once per round, with its own digest
  bracket and a `world.digest_consistent` flag. Under the 256 KB cap the
  world drops FIRST (`truncated.world: true`); digests/counts always
  survive.

### Accessor matrix (live probe 2026-09-08, parked game, mod 0.3.10)

Full evidence: `runs/m4-capture-evidence-20260908/probe/
accessor-matrix-20260908.md` in the observer worktree (raw logs beside
it). Summary:

| Accessor | GameCore (read) | InGame (write) |
|---|---|---|
| `Game.GetPlayers()` / `PlayerManager.GetAlive()` (all players incl. city-states, barbarian) | yes | yes |
| `IsMajor` / `IsAlive` / `IsBarbarian`, `PlayerConfigurations:GetLeaderTypeName` / `GetCivilizationTypeName` | yes | yes |
| `GetInfluence():GetSuzerain()` | yes | yes |
| `PlayersVisibility[pid]:IsVisible(plot)` (TABLE route) | yes | yes |
| `Map.GetPlotCount/GetGridSize/GetPlotByIndex/GetNeighborPlot` | yes | yes |
| Treasury `GetGoldBalance/GetGoldYield/GetTotalMaintenance`, `GetTechs():GetScienceYield()`, `GetCulture():GetCultureYield()`, `GetReligion():GetFaithYield()`, `player:GetEra()` | yes | yes |
| `GetTechs():HasTech()`, `GetCulture():GetProgressingCivic()` / `HasCivic()` | yes | yes |
| `GetCulture():GetCulturalProgress()` / `GetCostNextCivic()` | NO | yes |
| `UI.GetPlayerColors(pid)` (two ints) | NO | yes |
| `player:GetCivilizationLevelType()` | NO | NO |

Non-claims, explicitly: the 2026-08-31 "fog not exposed" finding applied
to the PLOT-object route — the PlayersVisibility TABLE route works on
GameCore (so `live.visible_map_context` stays `gamecore` by default and
is NOT required for fog); PLAYERROW `level` stays unread (`?` → absent —
the accessor exists nowhere probed) and `kind` is DERIVED from the
boolean flags, never an engine read; COLORROW carries RAW ints and the
ABGR/ARGB packing is UNVERIFIED (the viewer keeps the M1 owner-class
fallback until `scripts/live_capture_check.py` confirms it against an
attached game). The named-but-wrong getters (`GetGold`, `GetScience`,
`treasury:GetScienceYield`, …) appear nowhere in the Lua.

THE PARKED-GAME CITY TRAP (Amendment 1.5): `p:GetCities():GetCount()` /
`Members()` return ZERO on both contexts in a parked never-attached game
— the real `cities_read()` Lua verbatim also returned zero rows. Cities
enumerate only on a driver-attached game; every extended city accessor
(growth getters, HasBuilding, districts, production progress) keeps its
pcall `?` fallback until confirmed against the attached 3-round chain,
and texlua stub tests are the interim authority for those shapes.

### Wire and scope (contract §2)

| Read | Header | Context | Notes |
|---|---|---|---|
| visible map | `VMAP|4` (13-field TILEROW) | GameCore (default) / InGame opt-in | static keys (feature, river) on every row; dynamic keys (resource, improvement, district, appeal, engine_visible) visible-only; resources tech-gated per the seat |
| cities | `CITIES|2` (18-field CITYROW) | InGame | GetAlive() incl. city-states; majors fail-loud queue, minors pcall-guarded; placeholders REMOVED |
| overview | `OVX|2` (13-field OVROW + OVERA + OVCIVICS) | GameCore | yields + era on read transport; civic progress InGame-only ('?' → absent) |
| spectator roster | `SPECW|1\|roster` (PLAYERROW) | GameCore | level `?`; kind derived |
| spectator tiles | `SPECW\|1\|tiles` (GRID/OWNEDROW/TILES_END) | GameCore | owned plots only; raw resource (no seat tech gate); TILES_END must match |
| spectator palette | `SPECW\|1\|palette` (COLORROW) | InGame | raw ints, packing unverified |

Caps: roster ≤ 64 (`truncated.roster` count), cities ≤ 256, owned-tile
rows ≤ 4096 total (`TILES_TRUNCATED`), disagree_coords ≤ 64, world doc ≤
256 KiB (`truncated.world`). Blank = unsupplied; a missing key is never
a value. Fog counters ride the world doc's `fog_audit`
(requested/engine_visible/engine_not_visible/unavailable + ≤64
disagreeing coords) and the adapter's `fog_audit_for(pid)`.

Cross-link: CAP-R1 finding 8 (roster discovery can't see minors —
`docs/codex-r1-findings-20260907.md`) is delivered in SUBSTANCE by
`world_capture.roster_read`; the spectate census's OVX-based discovery is
deliberately NOT rewired.
