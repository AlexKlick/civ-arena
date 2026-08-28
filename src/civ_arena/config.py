"""Match configuration: YAML -> typed spec. Model blocks parse but are ignored.

The spike has no LLM; ``model:`` exists in the schema so later stages plug in
without config churn (a round-trip test pins that).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


class ConfigError(ValueError):
    pass


@dataclass
class AgentSpec:
    agent_id: str
    player_id: int
    policy: str  # "expansionist" | "turtler"
    seed: int
    model: str | None = None  # parsed and ignored in the spike


@dataclass
class ChaosSpec:
    spec: str
    hook: str = "act"
    offset: int = 0


@dataclass
class MatchSpec:
    match_id: str
    seed: int
    max_turns: int
    adapter: str
    watchdog_mode: str
    violation_limit: int
    checkpoint_every: int
    agents: list[AgentSpec] = field(default_factory=list)
    chaos: list[ChaosSpec] = field(default_factory=list)

    def agent_for_player(self, player_id: int) -> AgentSpec:
        for agent in self.agents:
            if agent.player_id == player_id:
                return agent
        raise ConfigError(f"no agent for player {player_id}")


VALID_POLICIES = frozenset({"expansionist", "turtler"})
VALID_ADAPTERS = frozenset({"simulator", "firetuner"})
VALID_WATCHDOG_MODES = frozenset({"flag_and_continue", "rollback"})


def _require(mapping: dict[str, Any], key: str, where: str) -> Any:
    if key not in mapping:
        raise ConfigError(f"{where}: missing required key {key!r}")
    return mapping[key]


def parse_config(doc: dict[str, Any]) -> MatchSpec:
    match = _require(doc, "match", "config")
    match_id = str(_require(match, "match_id", "match"))
    seed = int(_require(match, "seed", "match"))
    max_turns = int(match.get("max_turns", 100))
    adapter = str(match.get("adapter", "simulator"))
    watchdog_mode = str(match.get("watchdog_mode", "flag_and_continue"))
    violation_limit = int(match.get("violation_limit", 5))
    checkpoint_every = int(match.get("checkpoint_every", 5))

    if adapter not in VALID_ADAPTERS:
        raise ConfigError(f"unknown adapter {adapter!r}")
    if watchdog_mode not in VALID_WATCHDOG_MODES:
        raise ConfigError(f"unknown watchdog_mode {watchdog_mode!r}")
    if max_turns < 1 or checkpoint_every < 1:
        raise ConfigError("max_turns and checkpoint_every must be >= 1")

    agents: list[AgentSpec] = []
    seen_players: set[int] = set()
    for i, entry in enumerate(doc.get("agents", [])):
        where = f"agents[{i}]"
        agent = AgentSpec(
            agent_id=str(_require(entry, "agent_id", where)),
            player_id=int(_require(entry, "player_id", where)),
            policy=str(_require(entry, "policy", where)),
            seed=int(entry.get("seed", seed * 10 + i)),
            model=entry.get("model"),  # parsed and ignored
        )
        if agent.policy not in VALID_POLICIES:
            raise ConfigError(f"{where}: unknown policy {agent.policy!r}")
        if agent.player_id in seen_players:
            raise ConfigError(f"{where}: duplicate player_id {agent.player_id}")
        seen_players.add(agent.player_id)
        agents.append(agent)
    if not agents:
        raise ConfigError("at least one agent is required")

    chaos: list[ChaosSpec] = []
    for i, entry in enumerate(doc.get("chaos", [])):
        chaos.append(ChaosSpec(
            spec=str(_require(entry, "spec", f"chaos[{i}]")),
            hook=str(entry.get("hook", "act")),
            offset=int(entry.get("offset", 0)),
        ))

    return MatchSpec(
        match_id=match_id, seed=seed, max_turns=max_turns, adapter=adapter,
        watchdog_mode=watchdog_mode, violation_limit=violation_limit,
        checkpoint_every=checkpoint_every, agents=agents, chaos=chaos,
    )


def load_config(path: Path | str) -> MatchSpec:
    with open(path, encoding="utf-8") as fh:
        doc = yaml.safe_load(fh)
    if not isinstance(doc, dict):
        raise ConfigError(f"{path}: config must be a mapping")
    return parse_config(doc)
