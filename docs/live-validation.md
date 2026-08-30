# Live-game validation protocol — FireTuner / Civ VI leg

This repository's spike is **simulator-backed**; everything in it is proven by
the local test suite without a game. This document is the manual protocol for
the day a Civilization VI install exists and the live leg gets exercised.
Nothing here has been executed against a real game yet — treat every step as
a hypothesis to verify, and stop at the first anomaly.

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
