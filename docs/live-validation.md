# Live-game validation protocol — FireTuner / Civ VI leg

This document contains the original protocol followed by dated observations
from simulator checks and live Civ VI work. Use the dated evidence record
for the scope and outcome of each attempt. The latest 2026-09-04 attempt
stopped during startup; repeatable 30-round operational reliability remains
unproven.

## 1. Platform reality (unverified claims, both routes documented)

Civ VI Steam appid is **289070**. FireTuner is a TCP debug interface on
**port 4318**, enabled per-install via `AppOptions.txt`.

Two mutually conflicting claims exist:

- The upstream `lmwilki/civ6-mcp` README states FireTuner **requires the
  native Aspyr Linux port** and that "Proton/Wine builds do not expose it."
  That claim traces only to the author's own README (circular corroboration);
  their Linux work (CHANGELOG) targeted the Aspyr port.
- Community consensus is the **Aspyr native port is broken/outdated on
  modern distros** while the Windows build runs fine under Proton — and
  under Proton, `AppOptions.txt` lives inside the compat prefix.

**Locate, don't assume.** Check both paths:

| Route | AppOptions.txt location |
|---|---|
| Aspyr native | `~/.local/share/aspyr-media/Sid Meier's Civilization VI/AppOptions.txt` |
| Proton (Windows build) | `<steam library>/steamapps/compatdata/289070/pfx/drive_c/users/steamuser/My Documents/My Games/Sid Meier's Civilization VI/AppOptions.txt` |

## 2. Enabling the tuner

1. In `AppOptions.txt`, under `[Debug]`: `EnableTuner 1` (was 0). Also set
   `FullScreen 0` (windowed) — the tuner connection is less flaky windowed.
2. Consequences: **achievements are disabled** while the tuner is on, and
   **only one tuner client may be connected at a time** (close FireTuner.exe
   on Windows / any other client).
3. Launch the game and **load fully into a map** (the main menu exposes no
   game Lua states — the vendored connection raises for exactly this case).
4. Verify the listener:

   ```bash
   ss -ltn | grep 4318          # expect a LISTEN entry
   nc -zv 127.0.0.1 4318        # expect: succeeded
   ```

5. Smoke the wire layer without any game logic:

   ```bash
   uv run python scripts/firetuner_smoke.py --live
   ```

   Expected: handshake succeeds, `GameCore_Tuner` and `InGame` states are
   discovered, and the turn-state poll parses. If `LSQ:` returns no game
   states, you are at the main menu.

## 3. Rerunning the acceptance harness against the live game

The arena's acceptance criteria (see README / design-notes) map 1:1 onto a
live run once the adapter gaps are closed. Ordered protocol:

1. **Handshake**: `FireTunerAdapter.setup` connects; `Handshake()` from the
   mod must report `supports_freeze` and `supports_ledger`. Python refuses
   to drive a live match otherwise.
2. **Exclusive control (criterion 2)**: enable the mod, puppet ONE player,
   load a save, and let the referee hold a lease for a full turn while
   issuing no commands. Expected: `DumpLedger` + digest show zero unit,
   production, or research changes beyond the declared ambient window.
   This is the make-or-break test of `PlayerTurnStartComplete` timing
   (upstream open question 1) and of AI suppression (open question 3).
3. **Visibility isolation (criterion 3)**: implement `visibility_for` from
   the engine's revealed-tiles query, then rerun the projection property
   tests against live observations.
4. **Sequential dispatch (criterion 1)**: drive both players through the
   referee for 10 turns before attempting 100.
5. **Restart/resume (criterion 6)**: live save/load replaces the sim
   checkpoint; autosave every turn and verify resume continuity.
6. **Replay (criterion 8)**: the event log replay is adapter-independent —
   it must reproduce every recorded `after_state_hash` from the mod's
   digests.

Stop-on-first-anomaly: any step that fails invalidates the steps after it.
Record the failure in this file (dated) so the next attempt starts from
knowledge, not hope.

## 4. Extending the adapter

`FireTunerAdapter` implements the `GameAdapter` seam; every unimplemented
method names this section. Adding live support for an arena tool is:

1. one entry in `game/civ6/lua_translator.py` (semantic → Lua; pure),
2. one entry in `game/civ6/response_parser.py` (lines → docs; pure),
3. one canned-response test against `FakeTunerServer`,

with **zero changes** in `arena/`, `session/`, or `agents/`. Operation
routing follows the GameCore-safe vs InGame-switch split; in particular:

- 1-tile `UnitManager.MoveUnit`, `FinishMoves`, `RestoreMovement`,
  research setters: GameCore, no local-player switch.
- multi-tile `RequestOperation(MOVE_TO)`, `RANGE_ATTACK`, `FOUND_CITY`,
  `BUILD_IMPROVEMENT`, `CityManager.RequestOperation/RequestCommand`,
  diplomacy sessions: inside a pcall-wrapped
  `PlayerManager.SetLocalPlayerAndObserver` switch (restore ASAP; visible
  frame freeze each switch is expected).
- `pCulture:SetCivic(civicID, true)` is FORBIDDEN — it permanently breaks
  AI civic research. Use `SetCulturalProgress`.

The turn-interception behavior contract (freeze at hook with per-unit
snapshots, per-unit restore only, ambient windows declared at phase
boundaries, ledgers dumped for the watchdog) lives in
`mods/PuppeteerMod/PuppeteerMod.lua` — UNVALIDATED draft.

## 5. Operational notes

- One tuner connection at a time; the arena process is the only client.
- The game must stay the foreground process during InGame-context writes.
- Expect per-turn latency of seconds-to-minutes per puppeted player
  (upstream open question 5); the duel match config exists precisely to
  bound this.

## 6. Live-leg execution record (M14)

Host fact (verified 2026-08-29, read-only probes): the **Aspyr native Linux
port** is installed — appid 289070, buildid 15296837, `StateFlags 4`,
`libGameCore_*.so` — at
`/media/alexk/RAID5_Storage/SteamLibrary/steamapps/common/Sid Meier's
Civilization VI`. The game has never been launched on this host (no
`~/.local/share/aspyr-media`, no `AppOptions.txt` anywhere — it is created
on first run), so §1's Aspyr-native row is the expected AppOptions location
and the Proton row is expected dead (no `compatdata/289070` in that
library). Both expectations get CONFIRMED (not assumed) at the first
launch, below.

Entries are appended dated, newest last; every failure records the
hypothesis it killed. The staged smoke
(`uv run python scripts/firetuner_smoke.py --live`) is run before each
phase; its exit code names the failing stage (S1=10 … S6=60).

### Operator runbook — environment preflight (M14a)

1. First launch, to materialize the config tree (quit at the main menu):

   ```bash
   steam -applaunch 289070
   ```

2. Locate AppOptions.txt (§1 "locate, don't assume"):

   ```bash
   find ~/.local/share/aspyr-media -maxdepth 3 -name AppOptions.txt
   ls /media/alexk/RAID5_Storage/SteamLibrary/steamapps/compatdata/ 2>/dev/null
   ```

3. Edit the located `AppOptions.txt`: under `[Debug]` set `EnableTuner 1`;
   set `FullScreen 0`.

4. Stage the mod:

   ```bash
   mkdir -p "$HOME/.local/share/aspyr-media/Sid Meier's Civilization VI/Mods"
   cp -r /home/alexk/documents/civ-arena/mods/PuppeteerMod \
      "$HOME/.local/share/aspyr-media/Sid Meier's Civilization VI/Mods/"
   ```

### 2026-08-29 — M14a preflight shipped (no game touched)

Code-side only: staged smoke S1–S6 (`--live --json`, stage-distinct exit
codes, transcript under `runs/live-preflight-<ts>/`), the mod-handshake
seam (`mod_handshake`/`mod_status`/`mod_digest` translators +
fail-closed `parse_handshake` + `FireTunerAdapter.require_mod()` phase-1
gate), and the FakeTunerServer rehearsal mode (per-state framing, a
scripted FakeMod with fake-only `Simulate.*` commands to fire wire-
invisible engine events). Gate: 18/18 on the wire+preflight files; full
suite count in the M14a commit. Nothing live yet — every live step above
still pending operator hands.

### 2026-08-30 — M14b code leg + Codex round 1 (11 findings: 5 P1, 6 P2 → all fixed and pinned; 363 passed + 1 skipped)

Mod v0.2 (Status polling, ambient recorder, canonical rows, whole-board
digest) + the adapter phase surface (begin/end phase, D7 experiment,
digest-cached hash, mutation journal) + the exclusive-control driver —
all rehearsed against the REAL referee machinery over the fake wire.
Zero changes under `arena/`, `session/`, `agents/` (verified by diff).

Codex (gpt-5.6-sol) findings and their fixes:

- **P1-1 LIVE-ONLY — the freeze booked itself as a violation**: the hook
  snapshotted BEFORE `FinishMoves`, so every frozen unit diffed 2→0 at
  release as an undeclared actual. Fixed: freeze FIRST, then snapshot
  (the snapshot is the release baseline). Pinned by
  `test_freeze_snapshots_the_frozen_state`.
- **P1-2 LIVE-ONLY — recorder/digest coverage gap**: recorder covered
  units/cities only; the digest additionally covered gold/research —
  engine effects could move state with no ledger row to flag. Partially
  fixed: the recorder now also books gold/research/production-name
  (digest parity). RESIDUAL — DECLARED: districts, build queues,
  promotions, tile ownership are outside BOTH the recorder and the
  digest; nothing may claim live watchdog authority over them (the
  limitation is written into the mod source and pinned by test).
- **P1-3 LIVE-ONLY — lease identity unchecked**: begin_phase accepted any
  `PUPPET_ACTIVE=true`. Fixed: the wait requires LEASE_PLAYER == player
  AND LEASE_TURN == turn; an engaged lease for anyone else refuses.
- **P1-4 LIVE-ONLY — phase-end hash raced the next player**: post-release
  digest polls could hash foreign activity into TURN_END and fake idle
  drift. Fixed STRUCTURALLY, not by timing: phase hashes are
  owner-scoped (only the phase owner's digest rows) and SEALED at
  end_phase — later polls cannot move them (`filter_digest_rows` +
  `_sealed_hash`).
- **P1-5 — partial-run rerun spliced attempts**: the guard only rejected
  finished logs. Fixed: ANY existing events.jsonl refuses the rerun.
- **P2-6 — live envelope lacked replay parity**: MATCH_END now carries
  `aborted`/`final_state_hash`/`scores` (scores empty until M14c).
- **P2-7 — tap IO could break the authority path**: TapConnection
  degrades to closed on the first OSError; game responses never orphan.
- **P2-8 — gate ignored digest**: require_mod now requires
  freeze+ledger+digest (state_hash depends on it).
- **P2-9 — float tripwire missed spellings**: `.5`, `1.`, `1e3`, `nan`,
  `inf`, `+7`, `1_0` all fail closed now; only plain integers are
  canonical numbers on the wire.
- **P2-10 (future M14d) — act must refresh the digest**: contract
  written into the act stub; lands with the action surface.
- **P2-11 — setup absorbed S5 failures into S1**: digest seeding catches
  LuaError too, so the smoke's stage codes name the failing layer.

### 2026-08-30 — operator preflight attempt 1 (launch blocked, recorded)

CLI game launch FAILED on this host: `/usr/games/steam -applaunch 289070`
and the `steam://rungameid/289070` URI both start a second steam.sh that
never hands the request to the week-old `-silent` client (pid 1245205,
DISPLAY=:1); no Civ6 process, no aspyr-media tree, no :4318 listener.
Runtime update downloads DID run. Conclusion: launch must be operator-
clicked in the graphical session (the protocol's R10 path). AppOptions
path still unconfirmed — §6 runbook step 2 pending the first successful
launch.

### 2026-08-30 — the gaming-session platform facts (learned the hard way)

The host's games run inside a **systemd-managed dedicated X session**:
`headless-gaming.service` → `xinit ~/.local/bin/headless-gaming-session`
→ Xorg **:1** (vt8) + openbox + Sunshine (local mode drives the real
monitor when plugged; headless mode is a virtual 2944x1840 for
Moonlight). It is NOT an OS-level sandbox — same filesystem, same
network namespace — so `:4318` and `~/.local/share/aspyr-media` are
directly reachable from any shell. But Steam must live INSIDE that
session's environment, and three traps bit:

1. **Second Steam instances are poison**: a `steam.sh` started outside
   the session (even with DISPLAY=:1) fights the resident client — the
   resident one lost its CM login and entered a `LogonFailure No
   Connection` loop (network was fine the whole time; the store was
   reachable by curl). Never spawn a second Steam; ask the operator to
   drive the resident one.
2. **The harness HOME trap**: `/usr/games/steam` resolves `.steam`
   through `$HOME`, and an automation shell whose HOME is a scratch dir
   silently launches Steam against a FAKE Steam root. Always export
   `HOME=/home/alexk` explicitly.
3. **Killing the session's Steam costs the saved login** (and a blind
   `pkill -f steam` can self-match the calling shell, exit 144). Kill
   by explicit PID list only; after a kill, expect
   `SetLoginState: WaitingForCredentials` — the operator re-enters
   credentials.

Current state at this entry: Steam restarted correctly inside the
session (HOME fixed), waiting at the login prompt; operator logging in,
then Play. The `compatdata/289070` directory that appeared is a 4KB
EMPTY stub — no Proton prefix exists; the Aspyr-native row of the §1
table remains the expected AppOptions route, to be confirmed the moment
`aspyr-media` materializes.

### 2026-08-30 — THE MAKE-OR-BREAK PASSED: exclusive control CLEAN on the real engine

`live-exclusive-005`: **`clean: True, violations: 0, drift: False,
final_turn: 8`** — `MATCH_START → LEASE_GRANT → AMBIENT → LEASE_RELEASE →
TURN_END → TOOL pair (end_turn) → MATCH_END`, the exact sim event shape,
driven against live Civ VI. The freeze engaged at the player's
PlayerTurnStartComplete (upstream open question 1: ANSWERED — catchable),
nothing undeclared mutated during the held lease (open question 3:
suppressed), the held state was digest-identical from engagement to the
pre-endturn seal, and the programmatic end-turn (H1) worked.

The trajectory that got here — five runs, four live-only defects, each
fixed at the seam and recorded:

| run | outcome | the finding |
|---|---|---|
| 001 | crash at end_phase | GameCore and InGame are SEPARATE Lua VMs: `UI` nil in GameCore, `Puppeteer` nil in InGame — cross-VM commands must be self-contained |
| 002 | engage timeout (120s) | human-paced turn ends need a longer window (`--engage-timeout`) |
| 003 | crash at end_phase | `SetLocalPlayerAndObserver` exists ONLY in GameCore; but the local player's ENDTURN needs no switch — bare `UI.RequestAction` in InGame |
| 004 | full cycle, drift=true | the engine applies turn-end effects AFTER the end-turn command (gold income +5, completed production, the next player's whole turn) — the digest seal now brackets the held lease (pre-endturn), not the engine's post-processing |
| 005 | **CLEAN** | — |

Also proven this session (the D9 architecture shift): the mod needs NO
modinfo/Additional Content at all — gameplay-script globals never reach
the tuner VM, but `GameEvents` subscriptions made FROM the tuner fire on
the engine's dispatch (live-proven: hooks fired for every AI player and
the local one). The adapter now INJECTS PuppeteerMod.lua into the
GameCore VM at attach, with re-injection hygiene (old hooks retired).
The mod file stays the source of truth; the .modinfo is a packaging
artifact.

GameCore API surface (live-verified 2026-08-30): `GetMovesRemaining` /
`GetDamage` (NOT GetMovementRemaining/GetHP; no fortified accessor);
`GetGoldBalance` (NOT GetGold); `PlayerManager.GetAliveMajors`,
`IsMajor`/`IsAlive`/`IsBarbarian` (with per-entry nil guards — some
Players entries lack methods entirely); `UnitManager.FinishMoves`/
`RestoreMovement`/`RestoreUnitAttacks`/`MoveUnit`, `FindID`,
`SetLocalPlayerAndObserver` (GameCore only), `UI.RequestAction`+
`ActionTypes.ACTION_ENDTURN` (InGame only). The tuner's Lua lexer
rejects backslash escapes in patterns; multi-line prints arrive as one
payload (parser flattens).

### 2026-08-30 — the action-surface routing table (live-probed, read-only)

| Operation | VM | API (all verified present) |
|---|---|---|
| state reads (players/units/cities/techs/treasury) | GameCore | `Players`, guarded accessors (see above) |
| 1-tile move | GameCore | `UnitManager.MoveUnit` |
| freeze / per-unit restore | GameCore | `UnitManager.FinishMoves` / `RestoreMovement`+`RestoreUnitAttacks`, `Units:FindID` |
| set research | GameCore | `Techs:SetResearchingTech`, `CanResearch` |
| local-player switch | GameCore ONLY | `PlayerManager.SetLocalPlayerAndObserver` |
| multi-tile MOVE_TO / attacks / found-city | InGame | `UnitManager.RequestOperation` (NOT MoveUnit there) |
| production BUILD | InGame | `CityManager.RequestOperation(pCity, CityOperationTypes.BUILD, tParams)` — upstream's full pattern at civ6-mcp lua/cities.py:411-501, readback via `CurrentlyBuilding()` (GameCore) |
| purchases | InGame | `CityManager.RequestCommand`, `CityCommandTypes` |
| turn-end, LOCAL player | InGame | bare `UI.RequestAction(ActionTypes.ACTION_ENDTURN)` |

**The M14d simplification:** the opponent does not need puppeteering at
all — the ENGINE'S OWN AI plays that seat naturally between our turns.
The first live 1v1 = the arena driving ONE player (the local seat, where
every command above is legal as the acting local player: turtler policy
first, glm-5.3 next) against the built-in AI, with the watchdog on our
lease and the event log recording everything observable. Arena-vs-arena
(both seats driven) remains available later via H2 for the non-local
turn-end.

Post-M14 backlog (decided with the operator, 2026-08-30): once the live
control plane is proven, **glm-5.3 (Z.AI) enters as the live-leg LLM
opponent** — config-only per the M14 lane exploration (own `llm:` block,
Z.AI base URL, key env var name; the arena's per-agent client wiring
already supports two LLM agents). Also discussed: a **human-policy
agent** so the operator can play against the LLMs live — the mod only
puppets configured players, so the human's turn flows naturally and the
driver just waits for turn end; a small driver extension post-M14d.

### 2026-08-30 — M14d code leg: action surface + dispatch (fake-rehearsed)

The seven action tools + the six sim-shaped observes + the driver's
`--phase dispatch`, all rehearsed game-free through the real referee
machinery over the FakeTunerServer mini-engine (29/29 on the live-driver
file; full-suite count in the M14d commit):

- **The reconciliation seam (mod v0.3 `DiffSinceLast`)**: commanded
  effects must satisfy the watchdog's EXACT-key multiset diff
  (`entity_type, entity_id, attr, canonical(before), canonical(after)`),
  so allowed records must be THE SAME ROWS the release re-diff books.
  The mod's lease snapshot is now a ROLLING baseline: each accepted act
  drains `DiffSinceLast` (rows since the previous call, or lease start)
  and advances the baseline; `Release` re-diffs from the same baseline,
  booking only what no command covered. The adapter journals the drained
  rows as both the command's mutations and actuals — identical by
  construction.
- **Freeze-then-act discipline**: the lease freezes every unit at
  engagement, so each unit-tool act first restores exactly that unit
  (`RestoreUnit`), and a REJECTED act re-freezes it (`FreezeUnit`, new in
  v0.3) — otherwise the restored-but-unused movement would book as an
  undeclared actual at release.
- **Injection guard**: agent-supplied ids are interpolated into Lua
  source, so the adapter re-validates strict spellings (`u\d+`, `c\d+`,
  `[A-Z0-9_]+`, `-?\d+,-?\d+`) before any builder runs — a hostile
  `tech_id` never reaches the wire. Rejection tokens map to the
  RejectionReason ENUM (unknown token = loud failure).
- **The M14d visibility declaration**: `visibility_for` returns EMPTY
  sets until M14c's revealed-tiles read — the projection hides every
  FOREIGN entity (the safe side of the no-leak contract); own entities
  are ownership-based. Consequence, declared: driven agents cannot see
  or attack the opponent's units in M14d games (the turtler fortifies
  instead of attacking; an LLM sees only its own empire).
- **Coordinate mapping HYPOTHESIS**: the sim speaks axial hex; the engine
  speaks odd-q offset. `q = x, r = y - floor(x/2)` (bijection pinned by
  test). If the stagger parity is wrong for this engine, the only effect
  is REJECTED moves (never corrupted state); the first live dispatch
  decides it.
- **Declared read limitations**: production-queue readback is a pcall'd
  `GetBuildQueue():GetCurrentProductionType()` chain — if the GameCore
  accessor is absent the queue reads empty and the turtler re-issues
  production each turn (EXCLUSIVE replace keeps that legal); city
  hp/buckets/buildings are placeholder constants that only own-city
  pass-through projection ever carries (foreign cities are hidden under
  the empty visibility sets).
- **The first live 1v1 shape** (`--phase dispatch`): the driver drives
  ONE seat — agents[0], ANY policy including `llm` — through the real
  PlayerSession/Referee/tool surface while the engine's own AI plays the
  other seat. Integrity per turn: the digest moves IFF mutations were
  authorized (`unexpected` flag), plus the standing watchdog sweep.
- **Tourney configs** (operator request, 2026-08-30): `live-tourney-
  glm53` (Z.AI), `live-tourney-minimax-m3`, `live-tourney-qwen38`
  (text-main :18000 — its Anthropic-compat `/messages` path gets a curl
  probe before that match; llama-server key env may be empty). Live
  LLM-vs-LLM (both seats driven) needs the hotseat/H2 lane — next.

Live validation pending: dispatch with the turtler first, then the
tourney games (each LLM vs the engine AI), observable on the gaming
session.

### 2026-08-30 — M14d LIVE: seven clean driven turns; the self-sustaining
### loop proven; one engine wedge under investigation

The dispatch ladder against the real engine (all with the turtler, zero
watchdog violations on every driven turn):

| run | turns driven | the finding |
|---|---|---|
| 001 | — | 300s engage window too short for human-paced clicks (deleted pre-evidence; the finding is this row) |
| 002 | 2 | FIRST fully driven turn live: 9 tools through the real referee. Then t3 stranded: TURN+1 targeting overshoots after our own end-turn |
| 003 | — | attach re-injection KILLS the live mod's engaged lease → inject_mod is verify-first (version-gated) |
| 004 | — | reconfirmed 003; the bootstrap end-turn script born |
| 005 | 6 | targeting fix v1; t7 stranded — poll races the AI's turn transition |
| 006 | 8 | TURN_ACTIVE discriminator added (mod Status now reports IsTurnActive); t9 stranded — activation-in-progress ambiguity → settle window |
| 007 | 10 | settle in; t11 stranded — the REAL mechanism found via Codex P1-8: end_phase's trailing Release was turn-UNBOUND and killed the NEXT turn's engaged lease |
| 008 | — | mod syntax error reached the wire (junk half-line; cost the run) → luatex parse gate added |
| 009 | — | UI nil in GameCore for the bootstrap (read_raw) → write_raw; backslash escapes rejected in COMMAND chunks too → %c class |
| 009 (relaunch) | **14, 15, 16** | **SELF-SUSTAINING: three turns back-to-back, zero operator clicks** — the P1-8 fix proven live. Rule-2 targeting flaw found post-16 (fresh-attach ambiguity) |
| 010/011 | — | trace-ring discriminator wired; **the ENGINE WEDGED mid-cycle**: after our t16 deactivation the AI's turn never started (trace ring shows the cycle stop). Tyre's empty production queue confirmed + resolved via `scripts/live_resolve_production.py` (BUILD accepted); cycle still halted — pending the operator's screen report (suspected modal: diplomacy/city panel) |

Mechanisms learned about the engine (all via the mod's diagnostic ring):
- The full turn cycle fires PlayerTurnStartComplete for players 0, 1, 62,
  63 (majors + free agents) in order, every turn.
- Our H1 end-turn deactivates player 0 (hook fires) and the cycle proceeds
  — unless a modal blocks it (the current wedge).
- The engine applies no turn-start hook for a turn whose predecessor is
  blocked; everything downstream freezes cleanly.

Tourney status: configs live (glm-5.3 / MiniMax-M3 / Qwen3.8-27B local);
the first LLM game follows the turtler validation. The wedge must resolve
first (or a fresh game is created for it — seven driven turtler turns
already validate the milestone's core).

### 2026-08-31 — M17: planner readied for the live leg; the launch ladder

**M17a+b (committed 06e4d28)**: the CivGraph planner rehearsed
end-to-end through the LIVE dispatch surface over the fake wire —
3 turns clean, 0 violations, 0 rejections, journal landed. Three
sim-to-real seams found by the rehearsal itself (border_radius
defaults, researching null-vs-empty normalization, and the
wire-vocabulary filter that substitutes the engine's offered ids).
`scripts/screen_triage.py`: deterministic pixel-stats classification
(full PNG adaptive-filter decoder) + frame-diff freeze watch.

**M17c launch ladder (live-learned, this session)**:
- URI launch through the RESIDENT client WORKS mechanically:
  `HOME=/home/alexk DISPLAY=:1 /usr/games/steam
  steam://rungameid/289070` had the day-old -silent client create
  the Civ6 process tree at 14:13 (scout-on-soldier runtime).
- The game stalled pre-window in the Steam IPC handshake: zero CPU,
  no X socket, polling the client forever. The client had lost its
  CM login (black 1706x932 window; no CM connections).
- RETRY LESSON: relaunching while the first stalled game lives
  bounces with AppError_16 — kill the Civ6 tree before retrying.
- Graceful `steam -shutdown` (login-preserving, vs a kill) +
  in-session relaunch of the exact session command works: client
  back with connections in 10 s.
- CURRENT BLOCK (operator gate): no saved credentials survive —
  the login must be entered once in the Steam UI on :1. After
  login: URI launch → :4318 up → smoke (`scripts/firetuner_smoke.py
  --live`) → a game loaded to a map → planner dispatch with
  configs/live-planner-001.yaml.
- Tooling lesson: the agent harness's image PREVIEWS are unreliable
  (cache-collided renders of a Steam UI never in the bytes); the
  4B VL lane was RIGHT about every real capture. Ground-truth
  pixels with local decoders (screen_triage / ffmpeg signalstats).

## M17c — the zero-touch ladder closed; the planner plays live (2026-08-31)

Four live games this session, each teaching one layer. The full chain
now runs with ONE human input total (the Steam login, once):

1. **Zero-touch game creation works.** At the main menu the tuner
   exposes 31 front-end Lua contexts (Main State has `_G`; the UI
   contexts are sandboxed — bare-name probes only). `StagingRoom`
   carries the full hosting API: `Network.HostGame` /
   `GameConfiguration` / `MapConfiguration` / `PlayerConfigurations`
   plus the engine's own `Automation` table. ORDER MATTERS:
   configure at the menu FIRST (values stick through hosting), then
   `Network.HostGame()` — in SINGLEPLAYER mode it AUTO-LAUNCHES; no
   LaunchGame phase exists. Config gates on read-back: duel map
   (hash 388991850), 2 majors, standard speed.
2. **Civ VI type hashes are `~crc32(s)`** (verified against live
   GameInfo.GameSpeeds rows AND the MapConfiguration read-back).
   Never hand-type an enum value again: compute it.
3. **The leader intro is the one click.** VL locates the BEGIN GAME
   button WRONG ("bottom center"; the teal banner is at x≈0.28) —
   locate UI by pixel color. `x_click.py` does synthetic X input
   via ctypes XTest (no xdotool on this host); the XWarpPointer
   trap: dest_w=None is a RELATIVE move — pass the root window.
4. **Game one (blind)**: `get_visible_map` was the M14d empty-set
   stub — zero tiles → every M15d sight-bounded compiler produced
   nothing → 2 commands in 15 turns → defeat by neglect. The map
   was the only missing piece (the act surface was already live).
5. **The engine's fog API is NOT exposed**: plot
   IsRevealed/IsVisible/IsExplored nil, Player:GetVisibility
   errors, nothing enumerable on Map/Game. Derived visibility
   instead: hex radius 2 around own units / 3 around own cities,
   targeted terrain read for exactly those coords (leak-safe by
   construction), remembered tiles accumulated adapter-side (the
   M11 no-expiry epistemics).
6. **The frame seam**: the engine's axial frame is arbitrary (the
   duel start sits at (10,4), outside the radius-5 sim map) — the
   belief RE-CENTERS on its first own anchor; the executor
   translates move dests back at the wire boundary; a frame
   already inside the map keeps (0,0) so every sim pin is
   byte-identical.
7. **Game four (seeing)**: the planner FOUNDED ITS CAPITAL (Delhi),
   ran production every turn (MONUMENT → WARRIOR), set research,
   drove 15 clean turns — 0 rejections, 0 violations — until the
   turn-17 transition stalled on the research-chooser blocker (the
   driver's one UNHANDLED case; policies/civics blockers DO
   auto-resolve). The bounded-abort discipline worked exactly as
   designed: timeout, clean exit, no hang.
8. **Replay**: the 274-event log replayed model-free without
   unknown-record errors; the aborted run never wrote a MATCH_END
   hash, so hash parity is pending a run that reaches its natural
   end (the M14 P2-6 envelope work).

Tools landed: `scripts/frontend_probe.py` (state enumeration),
`scripts/live_newgame.py` (host/config/launch + the hash identity),
`scripts/x_click.py` (synthetic input), `scripts/live_zero_touch.py`
(the whole ladder, one command). Follow-up owed: the
ENDTURN_BLOCKING_RESEARCH auto-resolution; MATCH_END hash parity on
a full-length live run.

## M17d — the research blocker, resolved live; the evening's boot pathology (2026-08-31/09-01)

**The blocker fix is live-proven.** Game seven (duel, 1024x768): the
planner founded Aachen, ran production for ten clean turns, and at
turn 11 — the exact turn game four froze — the log reads
`housekeep[11]: research empty -> STUDY MINING: accepted`. The engine
cycle continued. That run then stopped on a FALSE anomaly: the digest
moved with zero AUTHORIZED mutations because housekeeping mutations
are acknowledged, not allowed. Fixed (6cd820c): driver-commanded
housekeeping mutations explain their own digest movement; the Codex
P2-1 check stays strict for genuinely undeclared drift. Rehearsal
clean, tests green.

Design note pinned by the planner rehearsal: research housekeeping is
REACTIVE (blocker-listed), never proactive — the blocker notification
lists at lease start (unlike production), and pre-filling would starve
the driven policy of its own research choice.

**The evening's boot pathology (games five/six/eight/nine, unresolved
— host-level):** starting ~19:30 every boot degrades the same way —
the game binds its listener on 4319 only (4318 binds late or never),
handshakes return an EMPTY state list through 20-60 min of 60-150%
CPU, and map loads crawl (afternoon games 2-4: 4318, 2-4 min loads).
Correlations: the AppOptions rewrite at 19:36 (RenderWidth jumped to
2944x1840 — now pinned back to 1024x768), and my early synthetic
clicks during the XWarpPointer bug may have dragged the window
full-size, switching render paths. A graceful Steam restart did NOT
clear it. nvidia-smi cannot see the game (pressure-vessel hides its
PID), so software-render fallback is suspected but unproven. NEXT
STEP: a full host reboot, then `scripts/live_zero_touch.py
--kill-first --smoke` and a 30-turn dispatch — the M17d code needs
nothing further.

Also learned: the leader intro's BEGIN GAME sits at DIFFERENT
fractions per boot (0.284/0.912 games 2-4; ~0.27/0.95 game eight) —
locate it per-boot (VL on the window crop answers reliably), and 4318
binds only at the in-game transition, not at boot.

## M17e — the long-game killers: two fixed and live-proven, one is the engine's own (2026-09-01)

**No reboot was needed.** The evening pathology was the GAMING SESSION's
long-lived X server (GL/driver state). Killing the xinit tree made the
session supervisor respawn a fresh one in seconds — after which the
game bound 4318, the intro banner sat at its classic position, and the
map loaded in 25 s (vs 20-60 min). The ladder is healthy again.

**The full-length attempt surfaced three long-game killers:**

1. **Front-end modals** (advisor tips chain after the policy/civic
   resolutions) freeze the between-turn processing — the lease never
   engages. FIXED: `_dismiss_popups` (Escape x2 — the second undoes the
   menu a blind first Escape opens; only invoked from the already-stalled
   path) + a single begin_turn retry on the same lease with the turn
   mirror re-armed. **Live-proven: `modal-sweep[16]` rescued turn 16 of
   the final run.**
2. **The attach-case race** (the settle window misreads an imminent
   hook; the parked human turn is never ended). FIXED:
   `_recover_stall` ends the parked turn ourselves when the stall shape
   says attach. **Live-proven: `recover[2]` rescued turn 2.**
3. **The engine's own AI hangs at ~turn 15-16** — Player 1's turn stays
   active indefinitely (35+ min at 150% CPU) with NO notification, NO
   dialog, menus cleared, pathing log stale; map size did not help
   (duel AND tiny both brick). NOT reachable from the wire. This is the
   one remaining blocker for a full-length 1v1 vs the engine AI.

**Recommended path past #3: a hotseat 1v1** — two HUMAN seats, both
driven by arena agents (planner vs turtler/planner). The engine AI is
out of the game entirely, the puppet/lease machinery already exists
per-player, and both seats already speak the arena's tool surface.
That is the shape of a true full-length autonomous 1v1.

## A1 — the in-game re-flag probe: ARCHITECTURE 1 CONFIRMED (2026-09-03)

The 2026-09-02 catch-22 (hotseat session kills the tuner; the tuner-safe
NONE-load demotes non-local humans) is BROKEN. The probe sequence, all
observed live (artifacts: runs/a1-reflag-probe-*/probe-record.txt):

1. **Hotseat create with EMPTY passwords** — the `--full --empty` variant
   reads back `P0PW|`/`P1PW|` as EMPTY STRINGS (not nil), so the shipped
   playerchange.lua `OnKeyUp_Return` auto-OK applies; the launch panel
   indeed rendered with NO password field and one Return started Player 1's
   turn. (ReadyButton ORB hitbox at window 0.50/0.888, as 09-02.)
2. **UI Quick Save at turn 1** (the tuner is dead inside the hotseat-session
   game by design — the wire save cannot run there; ESC menu → Quick Save
   at 0.50/0.383), then file-swap the quicksave into
   `Saves/Single/auto/AutoSave_0001.Civ6Save` — the only save location the
   LoadGame params reliably resolve (09-02 learning; unchanged).
3. **LoadGame(SERVER_TYPE_NONE)** with the load-menu screen OPEN (Single
   Player → Load Game clicks first): loads OUR match, and 4318 rebinds at
   the map transition. A fall-through load ALSO tears the front-end down
   (4318 dies even when nothing loads) — every load attempt costs the
   process's tuner; get the file right before loading.
4. **The demote, precisely**: P0 human slot=3(SS_TAKEN); P1 human=FALSE
   slot=**1(SS_OPEN)** — the seat is VACATED, not set to COMPUTER; the map
   roster auto-fills AI majors (T_ROOSEVELT, TOMYRIS) + city-states.
5. **The re-flag takes**: `PlayerConfigurations[1]:SetSlotStatus(SS_TAKEN)`
   + pcall pause-clear + BroadcastPlayerInfo from the InGame context.
   Read-backs: REFLAG_SLOT|1|3, REFLAG_CFGHUMAN|1|true; the GameCore
   census then reads P1 human=TRUE slot=3 — and it HOLDS across turn
   boundaries. (GetWantsPause is front-end-only — nil in GameCore.)
6. **The engine WAITS on the re-flagged seat instead of running AI**: with
   both puppets armed and seat 0's turn ended, the screen shows "WAITING
   FOR GILGAMESH" (the human-wait state) — the engine-AI-turn class that
   hung every long game is OUT of the game on this path. HOOK_ENTER|1 and
   LEASE_SET|1|1 fire; city-states cycle HOOK_SKIP|not-puppet.
7. **The local player does NOT auto-switch on the NONE path** — the engine
   treats seat 1 like a remote human. `PlayerManager.SetLocalPlayerAndObserver(1)`
   from GameCore WORKS from the wire (LOCALP 0→1, mid-game) — the A2
   driver delta is exactly: switch the local player to the lease-holder at
   each lease engagement; every GetLocalPlayer()-bound act builder and the
   InGame ENDTURN then work for either seat unchanged.
8. **A full engine cycle ran**: turn 1 p0→p1, turn 2 p0→p1 with leases
   engaging each time and the re-flag intact at the final census.

No hand-off panel appears on this path (the engine goes straight to
waiting), so the planned Return-sweep is stall-path only. X-server aging
re-confirmed: ~35 min is already too old for the menu bind — bounce X
per session (the gaming-mode pin in /run/gaming-session-mode must say
headless; a stale "local" pin from a hot-plug leaves the session 640x480
with a dead render path — `gaming-mode` re-detects and restarts).

## 2026-09-04 — bounded hotseat reliability implementation

Target: two consecutive fresh MiniMax-M3 versus MiniMax-M3 matches, each
30 complete rounds / 60 completed seat turns. This is an operational
reliability claim under the existing movement allowance, not a victory or
strategy-strength claim. Local tests cannot establish that target.

The reviewed source is `3074f1c510e4d0297305b19df1b5ab8dd22f3ac0` plus
`scripts/live_zero_touch.py`, `game/civ6/firetuner.py`, and
`game/civ6/live_driver.py`. The exact working patch is preserved in local
commit `fa130633e8e81bf672315008fc1a98fdda873799` and
`/tmp/civ-reliability-20260904/reviewed.patch`. Work continues on isolated
branch `fix/hotseat-reliability-20260904` in
`/home/alexk/civ-arena-reliability-20260904`; the original checkout and run
folders remain untouched.

### Verified findings

- UI recovery uses `sys.executable -m civ_arena.game.civ6.ui_control`, with
  one visible `WM_CLASS=Civ6` identity including display, window ID, and
  geometry. Moving or resizing requires a new capture before input.
- Helper outcomes are `sent`, `no_target`, `failed`, or `skipped_fake`.
  `sent` proves input only. Engine status and successful phase engagement
  determine recovery. Each action has a supporting screenshot/hash and an
  event-log audit; fake controllers send no desktop input.
- The default clocks are startup 2700s, play 7200s, agent turn 600s,
  stalled transition 180s/eight sweeps, and cleanup 20s. A transition's
  budget spans release and next engagement, including polling and helpers.
  Only successful engagement resets it. Clocks stay outside simulator hashes.
- Forced LLM closure checks the actual result, repairs a completeness
  rejection with one bounded standing-order pass, and retries once.
  Replayed rejected `end_turn` calls stay within their original turn.
- Only released leases produce completed-seat rows. A round needs both
  seats in order at the same engine turn; missing, duplicate, and reordered
  pairs are failures. Fatal errors preserve the game and close connections.
- `declare_own_endpath_drift: true` remains enabled in the existing configs:
  only own-unit `moves`, `movement`, `pos`, `q`, and `r` rows from the
  end path are admitted. Each admitted row and its observed owner are
  recorded in `HEARTBEAT` audit events. Foreign units and other attributes
  remain subject to the watchdog. This is not strict mod mutation accounting.

### Follow-up probes

Run gates in order and stop at the first failure: host/provider preflight,
three-round planner/turtler rehearsal, three-round MiniMax smoke, then
30-round acceptance A and a separate fresh 30-round acceptance B. Capture
both the game panel and engine status at real handoffs. Any code change
requires affected checks and restarts the consecutive acceptance requirement.

Use the project interpreter with this worktree's source explicitly selected:

```bash
cd /home/alexk/civ-arena-reliability-20260904
export PYTHONPATH="$PWD/src"
/home/alexk/documents/civ-arena/.venv/bin/python scripts/live_zero_touch.py \
  --session arch1 --fresh-x \
  --config configs/live-hotseat-001.yaml --rounds 3 \
  --startup-timeout 2700 --match-timeout 7200 --agent-turn-timeout 600 \
  --recovery-timeout 180 --recovery-sweeps 8 \
  --run-id "rehearsal-$(date -u +%Y%m%dT%H%M%SZ)" \
  > /tmp/civ-rehearsal-launch.log 2>&1
```

For smoke and acceptance, use `configs/live-hotseat-llm-minimax2-001.yaml`
with `--rounds 3` or `--rounds 30` and a new run ID each time. Provider,
model, token cap, retries, and 2000-request cap per seat are unchanged.
Secrets remain in `ANTHROPIC_AUTH_TOKEN_MINIMAX2`; never print its value.
Every startup creates `<run-id>-startup`, copies the existing save inventory,
and records its own startup-only event log/summary. The match event log is
`runs/<run-id>/events.jsonl`; driver output is retained in the startup
folder's `dispatch.log`. Do not confuse a clean startup with a clean match.

Audit a completed match with:

```bash
/home/alexk/documents/civ-arena/.venv/bin/python \
  -m civ_arena.game.civ6.validate_run runs/RUN_ID --rounds 30
```

This validates event structure, identity, counts, closures, deadlines, and
allowance accounting. It does not replay the live engine or establish
state-hash equality with a simulator. Keep structural replay results separate
from live engine-state observations and screenshots.

Stop and preserve: send SIGTERM to the task-owned launcher or driver PID.
The launcher forwards termination to its driver; allow the driver's 20s
cleanup interval plus a small scheduling margin. No new UI/game actions are
sent during cleanup. Preserve both run folders, save backups, event logs,
wire log, screenshots, and process output. A hard kill or unwritable event
log leaves an incomplete run; it cannot pass acceptance. Never rerun into
an existing run ID, rotate previous evidence, or restore a save automatically.

### Blocked checks

The fresh attempt `rehearsal-20260904T214144Z` failed its first live startup
gate: `tuner-bind` timed out after 240 seconds and the launcher exited 21.
The overall startup lasted 264.913 seconds, within its 2700-second ceiling.
At 2026-09-04 21:47 UTC, read-only checks found no visible `WM_CLASS=Civ6`
window, no Civ6 process, no tuner listener, and no tuner client. Steam and
the restarted gaming X session were running. The cause of Steam failing to
produce a Civ6 process is not yet established.

Both-human-seat census, mod capability checks, live handoff capture, the
six-turn deterministic rehearsal, LLM smoke, and both acceptance matches
are blocked by this missing game/tuner. The sequence stopped at that first
failed gate; it was not retried.

### Evidence gaps

There is no live handoff screenshot, engine digest, or wire transcript from
this attempt because Civ6/tuner never became available. Zero live seat turns
completed and neither acceptance run started. The launch diagnostic file is
empty; its existence is not proof that Steam accepted or executed the URI.
The previously reported 58-test baseline is historical evidence only.

### Captured results and handoff

| Check | Result | Evidence and scope |
|---|---|---|
| Full pytest gate at `bd65913` | 572 passed, 0 failed, 1 skipped | [Complete log](../runs/reliability-evidence-20260904/pytest-release.log); 526.82s; no flaky/rerun plugin used |
| Final launcher budget correction at `a8e3e3e` | 6 passed, 0 failed, 0 skipped | [Affected tests](../runs/reliability-evidence-20260904/launcher-reviewed-tests.log); full suite was not repeated for this isolated review correction |
| Ruff at `a8e3e3e` | PASS | [Complete log](../runs/reliability-evidence-20260904/ruff.log) |
| Configured MiniMax-M3 tool round trip | PASS | [Provider log](../runs/reliability-evidence-20260904/provider-preflight.log); two requests, both responses `MiniMax-M3`, 351 input and 31 output tokens |
| Fresh Architecture-1 startup at `a8e3e3e` | FAIL, exit 21 | [Launch log](../runs/reliability-evidence-20260904/rehearsal-launch.log), [startup summary](../runs/rehearsal-20260904T214144Z-startup/summary.json) |
| Controlled termination | PASS for this startup failure | Exactly one `MATCH_START` and one `MATCH_END`; the terminal event's summary equals the summary file: [events](../runs/rehearsal-20260904T214144Z-startup/events.jsonl) |
| Save preservation | 22 files preserved | [Inventory and hashes](../runs/reliability-evidence-20260904/preserved-saves.json); every backup matched the source after the failed attempt |
| Repeatable 30-round live reliability | NOT PROVEN | 0/2 acceptance runs started; 0 live seat turns completed |

The earlier completed full gate had 571 passed, one failed, one skipped.
The sole failure was a stale parser test that rejected string content even
though the reviewed client already normalized strings. Both files were
byte-identical to `3074f1c` before the correction:
[baseline attribution](../runs/reliability-evidence-20260904/baseline-failure.json).
The corrected test retains rejection of malformed numeric/list/block content.
The first full-gate attempt was stopped for a launcher review fix and is
incomplete evidence; it is not counted as a pass or failure.

The final startup correction shares the 2700s budget with driver setup by
forwarding only the remainder after boot and reconnect cooldown. The
7200s play, 600s agent-turn, 180s/eight-sweep recovery, and 20s cleanup limits
remain as documented. Provider configuration, request caps, and mod bytes
are unchanged. All clocks remain outside simulator determinism.

[Custody and hashes](../runs/reliability-evidence-20260904/custody.json),
[read-only review](../runs/reliability-evidence-20260904/review.md), and
[final host/termination observations](../runs/reliability-evidence-20260904/live-outcome.json)
are retained locally. Evidence lives under this isolated worktree's `runs/`
folder and is not published. The original checkout remains at `3074f1c`
with its original three-file working patch.

Next host probes, before another fresh attempt: inspect Steam's app-289070
launch handling and readiness, then establish a visible Civ6 window and
`127.0.0.1:4318` listener. Use read-only `systemctl show headless-gaming.service`,
`pgrep -ax Civ6`, `DISPLAY=:1 xprop -root _NET_CLIENT_LIST`, and
`ss -ltn '( sport = :4318 )'` to bind that state. Keep one tuner client.
The launcher command and stop-and-preserve procedure above are reproducible;
use a fresh run ID for any future attempt. No automatic save restore or
further launch attempt was performed after the failed gate.

## 2026-09-04 — parallel strategy kickoff and display diagnosis

The [startup follow-up](live-startup-followup-20260904.md) identifies a current
host blocker: the gaming service selected local-display mode while XRandR and
Steam's bundled SDL reported no usable display. Steam logged a login-window
creation failure. This does not establish the sole cause of the earlier failed
launch or prove that display repair will restore authentication and gameplay.

Local commit `e232b4b` adds a bounded display guard before X/game actions and
after X restart. The follow-up retains 17 passing focused tests, host evidence,
and the unsuccessful but reverted virtual-monitor probe. No fresh game launch
or provider request was made. The 22 previous save backups remain preserved.

The installed `/home/alexk/.local/bin/gaming-mode` helper requires host sudo
privileges unavailable to this session. After it restores a usable display,
recheck Steam readiness and single-client custody before restarting the staged
validation sequence above. Acceptance remains 0/2 runs started.

The [parallel kickoff](strategy-program-kickoff.md) records independent simulator
evaluation work and the [capability/experiment inventory](strategy-next-experiment.md).
Those tracks cannot satisfy this live gate or the separate CAR-M1 V2 gates.

## 2026-09-04 — spectator popup control and restored headless display

The user restored headless mode. Current host observations show an active
2944×1840 output, authenticated Steam startup and a Sunshine streaming client.
The prior missing-display blocker is cleared at the host-observation level.

The [spectator popup implementation](spectator-popups.md) adds bounded checks
through the existing driver connection, normal callbacks for six informational
contexts, serialized UI actions, and terminal popup audit counters. It does not
provide live popup proof from source or Lua-fixture tests.

The fresh provider preflight failed its echo tool-call predicate. One bounded
diagnostic attempt also failed: the client reported a text-only response with
zero tool calls. Both attempts and the first probe's evidence gap are retained
in the spectator record. No fresh game was launched and no further provider
requests followed. The existing headless/Moonlight session remains preserved.
Acceptance remains 0/2 runs started; installed callback execution and real
handoff observations for this revision remain **BLOCKED** by the failed stage.
