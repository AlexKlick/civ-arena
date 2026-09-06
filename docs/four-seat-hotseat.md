# Four-seat hotseat extension

This branch prepares opt-in two-to-four-seat Architecture-1 matches. It does not establish a live four-seat startup or replace the two consecutive fresh 30-round acceptance contract. The parent is running its separate two-seat 100-round lane.

Starting custody: `/home/alexk/civ-arena-four-seat-20260906`, branch `feat/four-seat-hotseat-20260906`, clean base `1bc80911373b5a88b3cb7ebe0befb02c5716cb80`. Parent worktree, installed mod, provider, tuner, desktop, and existing runs are untouched.

Implementation plan:

1. Generalize ordered completed-turn accounting and next-seat selection to an explicit roster of two through four distinct seats; reject unsupported counts and identities. Keep engine turns and lease release authoritative.
2. Parameterize the existing shipped-API startup recipe for contiguous fresh seats 0 through N-1, including all human passwords/readiness, closure of every extra major slot, post-host/native-refresh guards, all nonlocal reflags, and an exact complete GameCore census. Fail before desktop input on unsupported configured rosters.
3. Extend the fake engine roster, validator, and dashboard to the same seat count. Exercise four ordered seat completions per round and refusal of skips, duplicates, wrong turns, or missing human seats; preserve one tuner client and no desktop input in fake runs.
4. Add an opt-in four-model 100-round configuration with unchanged per-agent provider/request limits and movement allowance. Capture focused checks and Ruff; request independent review before parent integration or any live run.

Live startup remains unverified until a separate fresh four-seat run proves the existing slot APIs survive hosting, save/load demotion, reflagging, and all four engine handoffs. No new host API is assumed and no live probe runs from this branch. Elimination or an unexpected major seat aborts rather than silently shrinking the required roster. Structural fake/replay checks are separate from live engine-state proof.


## Implemented behavior

`CompletedTurns` and the adapter handoff select the next sorted configured player from a fixed roster of two through four distinct integer IDs. A round increments only after every seat releases its lease on the same engine turn. A missing, repeated, or reordered completion cannot satisfy the ledger or event validator. Elimination is not a roster change: a missing next living major is a controlled failure.

The fresh launcher derives its seat count from the match configuration. Fresh IDs must be contiguous from zero; invalid counts, gaps, and non-FireTuner configurations fail before desktop input. The existing default remains seats 0 and 1. Three/four-seat startup requires the Architecture-1 `--full --ui-start` route; the older manual completion route remains two-seat-only and refuses additional seats before opening a connection.

The existing Tiny map recipe now configures all requested humans, closes every other major slot, verifies participation before/after hosting, then requires readiness after the final native setup refresh, and checks an exact empty-password receipt for every seat. Base leaders are Cleopatra, Gilgamesh, Trajan, and Pericles in seat order. The last two identifiers are present in installed base `leaders.xml`; the source path, hash, and exact rows are retained in `runs/four-seat-local/base-leader-source.json`. Minor/barbarian slots and native map bounds remain under the game's existing setup.

After the Architecture-1 save/load, each nonlocal seat is reflagged sequentially with the existing helper. Each requires a unique successful slot/human readback. The full GameCore census must then contain exactly the configured living human majors. Existing one-client cooldowns, deadlines, save backups, fake input prohibition, mod handoff guards, and movement allowance are retained. No installed mod was changed by this branch.

The dashboard counts configured rounds before its existing bounded display truncation (latest 120 seat turns). It marks partial/duplicated/out-of-order terminal evidence and unexpected agent-to-seat attribution incomplete. The validator requires N times the requested rounds in authoritative release, closure, and completion rows. The simulator replay path explicitly refuses three/four-seat configurations before altering derived artifacts because its map generator still creates only two players. Call bucketing supports all four identities; this is separate from simulator replay and live engine-state proof.

`configs/live-hotseat-strategic-minimax4-100.yaml` opts into four MiniMax-M3 strategic-autopilot runtimes for 100 full engine rounds (400 seat turns). It uses the existing provider configuration, each agent's 4096-token/16-tool-round/8000-character context limits, request timeout/retry limits, and 2000-request-per-agent match cap. The movement allowance remains explicitly `declare_own_endpath_drift: true`; every admitted row remains audited, with foreign and other drift outside that policy still failing.

## Verified findings

- Repository evidence before the final readiness/attribution hardening: `runs/four-seat-local/combined-second.log` records 123 passed in 87.31 seconds. Its executable real-Lua fixture handed off 0→1→2→3→0 while freezing outgoing units first. Its real driver/adapter/FakeTuner fixture completed two rounds/eight released seat turns and validated their event structure.
- Initial integration fixture failures are preserved: six old startup fixtures used nonexistent placeholder configs, and the first new runtime fixture incorrectly accessed an unavailable facade identity attribute. These were corrected to use actual configuration and the runtime's bound profile. Logs are `existing-startup-dashboard.log` (80 passed, 6 failed) and `new-four-seat-first.log` (33 passed, 1 failed); these are new test-fixture failures, not reviewed-baseline failures or live game findings.
- `runs/four-seat-local/focused-final.log`: 188 passed, zero failures/skips, in 273.30 seconds across the four-seat tests, existing roster/reliability/hotseat/launcher/dashboard tests, live replay tests, and guarded handoff tests. The only source delta during that run restricted readiness checking to the final UI launch boundary, preserving native interim unready behavior; `runs/four-seat-local/startup-final-boundary.log` then directly covered the final startup code with 105 passed, 1 deliberately deselected long driver integration test, zero failures/skips, in 1.60 seconds.
- `runs/four-seat-local/ruff-settled.log`: `python -m ruff check src scripts tests` reports `All checks passed!` for the settled implementation. `runs/four-seat-local/diff-check.log` is empty (no whitespace errors).
- Implementation commit `c104c5401c1631403f940a8a1cb1e271787735dd`, tree `c456a97199ac89c1e3fb923ec6f44cdf064a0288`; plan commit `95edcba`. `runs/four-seat-local/evidence-manifest.json` binds these identities, changed source hashes, and complete captured log hashes. A broad full release suite remains reserved for integration after independent review.
- QA normalization scanned 24 documents and reported zero warnings for this report (`runs/four-seat-local/qa-normalize.log`). Its 163 warnings concern inherited documents, principally earlier runs absent from this new worktree; those historical evidence paths are not new branch proof.

## Follow-up probes

- Read-only review of the final commit and changed source before parent integration. The parent current live lane must remain untouched while running.
- After integration and required release checks, a fresh four-seat Architecture-1 run must prove all four humans survive host/native refresh, save/load/reflag, and correctly ordered engine handoffs. Retain the fresh census, event log, summary, configured limits, exact movement rows, model identity, request usage, mod digest, and timings.
- A full successful four-seat 100-round run needs exactly 400 released and accepted closures. Neither a two-seat 100-round run nor the fake eight-seat-turn check proves that result.

## Blocked checks

- Three/four-seat simulator replay: the simulator's `duel_start` supports exactly two players. The explicit refusal is intentional. Use event-structure validation below; engine-state replay needs a separate simulator extension.
- No environment failure blocked local focused checks. Live four-seat startup is unexecuted in this isolated development lane, not a passed host gate.

## Evidence gaps

- No browser-visible four-seat interaction, live provider round trip, live four-seat startup, or live four-seat engine progression was performed here. Local configuration/schema/source evidence cannot establish these lanes.
- No completed 100-round four-seat live match or replacement for the original two consecutive fresh 30-round acceptance runs is claimed.

## Reproduction and stop/preserve

After review/integration, use a new run ID from the reviewed worktree; keep the configured physical display and one tuner client:

```bash
PYTHONPATH=src PYTHONUNBUFFERED=1 DISPLAY=:1 /home/alexk/documents/civ-arena/.venv/bin/python scripts/live_zero_touch.py --session arch1 --kill-first --config configs/live-hotseat-strategic-minimax4-100.yaml --rounds 100 --startup-timeout 2700 --match-timeout 7200 --agent-turn-timeout 600 --recovery-timeout 180 --recovery-sweeps 8 --run-id UNIQUE_FOUR_SEAT_RUN --runs-root runs > /tmp/UNIQUE_FOUR_SEAT_RUN-launch.log 2>&1
```

Startup backs up saves under the unique run's startup artifacts. Before installation, the parent launch wrapper must preserve any installed mod under a run-specific backup and bind the reviewed source hash as in its existing procedure. Stop on the first unresolved recovery, watchdog, provider, deadline, or roster failure. Do not resume an uncertain lease or automatically restore a save. If stopping externally, signal only the verified task-owned launcher/driver PID so bounded cleanup can write the terminal record. Preserve the game window, Steam/X session, logs, saves, summaries, and screenshots; a hard kill remains incomplete.

```bash
PYTHONPATH=src /home/alexk/documents/civ-arena/.venv/bin/python -m civ_arena.game.civ6.validate_run runs/UNIQUE_FOUR_SEAT_RUN --rounds 100 > /tmp/UNIQUE_FOUR_SEAT_RUN-structure.log 2>&1
```

This validates event structure and outcome consistency. It does not establish live engine-state replay or victory.

## Independent review corrections

Review of `1c78520` found two P2 defects despite its green focused checks: the browser still truncated the roster to two seats, and dashboard round counting accepted four observed seats without an authoritative configuration. That reviewed implementation was NO-GO. The review is preserved at `/home/alexk/civ-arena-reliability-20260904/runs/60-round-live-20260906T172847Z/four-seat-review/review-1c78520.md`.

- Frontend correction `7434c6d`: every supplied seat has a card, scouting panel, distinct color, action lane, and generated legend entry. Additional lanes use horizontal scrolling without overlap; the UI explains that navigation. Roster-only updates invalidate the graph/scouting render caches. No placeholder players or two-seat-only labels remain.
- Authority correction `a244bd1`: audited round counts require a complete valid roster from `MATCH_START.config.agents` or `run_identity.identity.config.agents`. Missing, partial, duplicate, malformed, conflicting, or expanded declarations cannot certify a round. Observed actions do not define or enlarge the configured roster. The historical two-seat `TURN_END` display remains separate with an explicit missing-driver-audit warning; it cannot invent four-seat rounds.
- Current focused regressions: `runs/four-seat-review-fixes/review-regressions-final.log` records **110 passed, 1 deliberately deselected long fake-driver integration test, zero failed/skipped, in 0.92 seconds**. This includes execution of the production JavaScript for two, three, and four seats and the missing/invalid/config-conflict backend cases. `runs/four-seat-review-fixes/ruff-settled.log` reports `All checks passed!` for `ruff check src scripts tests`.
- Browser-visible fixture proof: `runs/four-seat-review-fixes/browser-proof.json` and `browser-probe-final.log` bind the actual frontend source hashes. A separately launched headless Chrome rendered four cards, four scouting panels, four legend entries, and nonoverlapping action lanes at 1440 px and 390 px; each seat's call opened the matching details. A roster-only refresh also updated all four panels/lanes. No page overflow, console/page errors, or failed requests were observed. Screenshots are `four-seat-1440.png` and `four-seat-390.png` in the same directory. The browser used intercepted local fixture responses; it did not access the parent dashboard, live game, provider, tuner, or desktop.

Narrow independent re-review and the parent's integrated release gate remain pending. This correction adds browser fixture proof; native four-seat startup and four-seat live progression remain unexecuted. Earlier failed Ruff formatting checks and the corrected summaries are retained separately, without replacing failed logs.
