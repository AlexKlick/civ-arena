# civ-arena

See [the publication inventory](PUBLICATION.md) for current branches and validation boundaries.

An agent-vs-agent **Civilization VI arena** control plane. One authoritative
arena process owns the single game connection, referees turn leases and action
legality, enforces per-player visibility (fog of war), logs every action with
before/after state, checkpoints, and replays.

The simulator control plane supports exclusive turn leases, visibility
isolation, idempotency, crash-resume, and deterministic replay. Scripted,
planner, and LLM agents share the referee-controlled tool surface. The LLM
lane includes diary, strategy, and recall services; see
[`docs/llm-lane.md`](docs/llm-lane.md) for recorded provider evidence.

The Civ VI FireTuner implementation includes bounded hotseat recovery and
terminal run auditing. Two consecutive live 30-round matches remain unproven;
the latest monitor game completed three rounds before a unit-identity
accounting defect stopped it. See
[`docs/live-validation.md`](docs/live-validation.md) for the evidence boundary
and [`docs/strategy-program-kickoff.md`](docs/strategy-program-kickoff.md) for
the parallel reliability, evaluation, and strategy work. A separate CAR-M1
worktree contains the V2 turn-control candidate; it is not integrated here.
The hotseat driver also checks and dismisses allowlisted informational popups
for spectators; see [Moonlight viewing and popup handling](docs/spectator-popups.md).

The [browser match room](docs/browser-match-room.md) shows each model's recorded
plans, clickable action sequences, results and timings at `http://127.0.0.1:8788/`.
It follows the event log without connecting to the game. Forecast and dependency
graph work is planned in [live-turn-pacing.md](docs/live-turn-pacing.md).

## Layout

- `src/civ_arena/arena/` — coordinator, referee, watchdog, leases, event log,
  checkpoints, idempotency, diary, telemetry, visibility scopes.
- `src/civ_arena/game/` — the `GameAdapter` seam; `sim/` deterministic
  simulator; `civ6/` FireTuner adapter, hotseat driver, UI control, and run audit
  over a vendored wire layer.
- `src/civ_arena/agents/` — `AgentRuntime` protocol, scripted bots, and
  `llm/` (Messages client, prompts, tool schemas, `LLMAgentRuntime`).
- `src/civ_arena/session/` — the agent-visible tool surface (15 tools; no
  `player_id` parameter anywhere).
- `mods/PuppeteerMod/` — draft turn-interception Lua mod (UNVALIDATED).
- `configs/` — match configurations (`duel.yaml`, `llm-vs-turtler.yaml`).
- `docs/` — design notes, live-game validation protocol, LLM lane record.

## Run

```bash
uv sync
uv run python -m civ_arena.match configs/duel.yaml      # 100-turn scripted duel
uv run python scripts/llm_ping.py                       # prove the model wire
uv run python -m civ_arena.match configs/llm-vs-turtler.yaml  # LLM vs bot
uv run python -m civ_arena.report runs/<match_id>       # telemetry summary
uv run python -m civ_arena.replay runs/<match_id>       # verify replay == live
uv run python -m civ_arena.dashboard --runs-root runs  # local browser match room
uv run pytest                                            # full local gate
uv run ruff check .
```

## Provenance

The FireTuner wire layer under `src/civ_arena/game/civ6/vendor/` is vendored
from [`lmwilki/civ6-mcp`](https://github.com/lmwilki/civ6-mcp) (MIT) — see
`src/civ_arena/game/civ6/vendor/VENDORED.md` for the exact local diff. The
arena architecture follows the agent-vs-agent design sketched in that
project's upstream `docs/agent-vs-agent.md` proposal (a file in the upstream
repository, not in this one; local reference clone at `~/documents/civ6-mcp`),
hardened per this repository's own fairness model (`docs/design-notes.md`).

Local pytest is the only release gate. No hosted CI.
