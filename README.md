# civ-arena

An agent-vs-agent **Civilization VI arena** control plane. One authoritative
arena process owns the single game connection, referees turn leases and action
legality, enforces per-player visibility (fog of war), logs every action with
before/after state, checkpoints, and replays.

**This repository is the spike milestone**: no LLMs, no memory system, no
diplomacy. Two deterministic scripted policies play a 100-turn duel on a
built-in simulator, and the arena proves — under adversarial injection — that
exclusive control, visibility isolation, idempotency, crash-resume, and replay
all hold. The live Civ VI (FireTuner) leg is a skeleton plus a manual
validation protocol (`docs/live-validation.md`); models plug in later behind
the `AgentRuntime` protocol.

## Layout

- `src/civ_arena/arena/` — coordinator, referee, watchdog, leases, event log,
  checkpoints, idempotency, telemetry, visibility scopes.
- `src/civ_arena/game/` — the `GameAdapter` seam; `sim/` deterministic
  simulator; `civ6/` FireTuner adapter skeleton over a vendored wire layer.
- `src/civ_arena/agents/` — `AgentRuntime` protocol + scripted bots.
- `src/civ_arena/session/` — the agent-visible tool surface (14 tools; no
  `player_id` parameter anywhere).
- `mods/PuppeteerMod/` — draft turn-interception Lua mod (UNVALIDATED).
- `configs/` — match configurations.
- `docs/` — design notes + the live-game validation protocol.

## Run

```bash
uv sync
uv run python -m civ_arena.match configs/duel.yaml      # 100-turn scripted duel
uv run python -m civ_arena.report runs/<match_id>       # telemetry summary
uv run python -m civ_arena.replay runs/<match_id>       # verify replay == live
uv run pytest                                            # full local gate
uv run ruff check .
```

## Provenance

The FireTuner wire layer under `src/civ_arena/game/civ6/vendor/` is vendored
from [`lmwilki/civ6-mcp`](https://github.com/lmwilki/civ6-mcp) (MIT) — see
`vendor/VENDORED.md`. The arena architecture follows the agent-vs-agent design
sketched in that project's `docs/agent-vs-agent.md` proposal, hardened per
this repository's own fairness model (`docs/design-notes.md`).

Local pytest is the only release gate. No hosted CI.
