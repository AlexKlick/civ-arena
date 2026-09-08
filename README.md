# civ-arena

See [the publication inventory](PUBLICATION.md) for current branches and validation boundaries.

An agent-vs-agent **Civilization VI arena** control plane. One authoritative
arena process owns the single game connection, referees turn leases and action
legality, enforces per-player visibility (fog of war), logs every action with
before/after state, checkpoints, and replays.

**The spike control plane is complete**: under adversarial injection it proves
exclusive control, visibility isolation, idempotency, crash-resume, and replay
with deterministic scripted policies. **The LLM lane is live**: a
model-driven agent (`policy: llm`) plays through the same 15-tool surface
against the scripted bots, with a bounded per-turn diary as its only
cross-turn memory — see `docs/llm-lane.md` for the proven wire facts and the
MiniMax setup. The live Civ VI (FireTuner) leg remains a skeleton plus a
manual validation protocol (`docs/live-validation.md`).

## Layout

- `src/civ_arena/arena/` — coordinator, referee, watchdog, leases, event log,
  checkpoints, idempotency, diary, telemetry, visibility scopes.
- `src/civ_arena/game/` — the `GameAdapter` seam; `sim/` deterministic
  simulator; `civ6/` FireTuner adapter skeleton over a vendored wire layer.
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
