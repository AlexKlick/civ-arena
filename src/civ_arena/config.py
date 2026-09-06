"""Match configuration: YAML -> typed spec.

``model:`` remains a display hint that parses and is ignored (a round-trip
test pins that); the LLM lane's live wiring lives in the ``llm:`` block —
the api key NEVER appears inline, only the NAME of the env var holding it.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class AdaptiveContextSpec:
    """Opt-in targets are soft; only provider-counted complete requests face a hard window."""
    provider_context_tokens: int
    strategy_target_chars: int | None = None
    economy_target_chars: int | None = None
    contact_target_chars: int | None = None

    def __post_init__(self) -> None:
        if type(self.provider_context_tokens) is not int or self.provider_context_tokens < 1:
            raise ConfigError('adaptive_context.provider_context_tokens must be positive integer')
        for task in ('strategy', 'economy', 'contact'):
            value = getattr(self, task + '_target_chars')
            if value is not None and (type(value) is not int or value < 1):
                raise ConfigError('adaptive context targets must be positive integers or null')


@dataclass(frozen=True)
class LLMSpec:
    """Provider wiring for a ``policy: llm`` agent. The wire label used by
    telemetry is ``model_id`` — what actually went on the wire, not the
    display-only top-level ``model:``."""

    base_url: str
    api_key_env: str
    model_id: str
    max_tokens: int = 4096
    max_tool_rounds: int = 16
    max_result_chars: int = 8000
    request_timeout_s: float = 120.0
    max_retries: int = 2
    max_requests_per_match: int = 2000
    adaptive_context: AdaptiveContextSpec | None = None


def _parse_llm(block: Any, where: str) -> LLMSpec:
    if not isinstance(block, dict):
        raise ConfigError(f"{where}: llm block must be a mapping")
    if "api_key" in block:
        # structural refusal: a literal key in YAML is a leak waiting to
        # happen. Only the env var NAME belongs in config.
        raise ConfigError(
            f"{where}: llm.api_key must not appear in config — "
            "use llm.api_key_env (name of the env var holding the key)"
        )
    base_url = str(_require(block, "base_url", f"{where}.llm"))
    api_key_env = str(_require(block, "api_key_env", f"{where}.llm"))
    model_id = str(_require(block, "model_id", f"{where}.llm"))
    if not base_url.startswith(("http://", "https://")):
        raise ConfigError(f"{where}.llm: base_url must be an http(s) URL")
    if not api_key_env:
        raise ConfigError(f"{where}.llm: api_key_env must be a non-empty name")
    ints = {
        "max_tokens": block.get("max_tokens", 4096),
        "max_tool_rounds": block.get("max_tool_rounds", 16),
        "max_result_chars": block.get("max_result_chars", 8000),
        "max_retries": block.get("max_retries", 2),
        "max_requests_per_match": block.get("max_requests_per_match", 2000),
    }
    for key, value in ints.items():
        if not isinstance(value, int) or isinstance(value, bool) or value < 1:
            raise ConfigError(f"{where}.llm: {key} must be a positive integer")
    if ints["max_tool_rounds"] < 1:
        raise ConfigError(f"{where}.llm: max_tool_rounds must be >= 1")
    if ints["max_retries"] > 10:
        # retry sleeps grow 0.5 * 2**attempt; bound the wall-clock exposure
        raise ConfigError(f"{where}.llm: max_retries must be <= 10")
    timeout = block.get("request_timeout_s", 120.0)
    if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) \
            or not math.isfinite(timeout) or not 0 < timeout <= 600:
        raise ConfigError(
            f"{where}.llm: request_timeout_s must be finite and in "
            "(0, 600] seconds"
        )
    adaptive = block.get('adaptive_context')
    if adaptive is not None:
        fields = {'provider_context_tokens', 'strategy_target_chars',
                  'economy_target_chars', 'contact_target_chars'}
        if not isinstance(adaptive, dict) or set(adaptive) - fields \
                or 'provider_context_tokens' not in adaptive:
            raise ConfigError('adaptive_context has unsupported or missing fields')
        adaptive = AdaptiveContextSpec(**adaptive)
    return LLMSpec(
        base_url=base_url, api_key_env=api_key_env, model_id=model_id,
        max_tokens=ints["max_tokens"], max_tool_rounds=ints["max_tool_rounds"],
        max_result_chars=ints["max_result_chars"],
        request_timeout_s=float(timeout), max_retries=ints["max_retries"],
        max_requests_per_match=ints["max_requests_per_match"], adaptive_context=adaptive,
    )


@dataclass(frozen=True)
class CaseBaseSpec:
    """M19b: retrieval-as-evidence case base for policy "planner". ``path``
    is a BARE filename under configs/ (the recall_runs safe-id charset: no
    '/', no leading dot) — the artifact ships with the config surface, an
    arbitrary host path never rides a match config."""
    path: str


def _parse_case_base(block: Any, where: str) -> CaseBaseSpec:
    if not isinstance(block, dict):
        raise ConfigError(f"{where}: case_base block must be a mapping")
    raw = _require(block, "path", where)
    if not isinstance(raw, str) \
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", raw):
        raise ConfigError(
            f"{where}.case_base.path must be a bare filename under configs/ "
            "([A-Za-z0-9][A-Za-z0-9._-]{0,63}), not a path")
    return CaseBaseSpec(path=raw)


@dataclass
class AgentSpec:
    agent_id: str
    player_id: int
    policy: str  # "expansionist" | "turtler" | "llm" | "planner"
    seed: int
    model: str | None = None  # display hint; parsed and ignored
    llm: LLMSpec | None = None
    # M16b: untrusted LLM strategy proposer for policy "planner" only.
    proposer: LLMSpec | None = None
    # M19b: case-base prior artifact for policy "planner" only.
    case_base: CaseBaseSpec | None = None
    decision_mode: str = "legacy"


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
    # M13 cross-match recall: prior match_ids whose logs form the corpus.
    # Absent/empty => the recall_lessons tool is unavailable (existing
    # configs behave identically).
    recall_runs: list[str] = field(default_factory=list)
    # Turn-completeness gate (RefereeConfig.completeness_gate): one
    # end_turn bounce per (player, turn) while own units have movement
    # and no standing order.
    completeness_gate: bool = False
    # Live-hotseat movement-drift tolerance (RefereeConfig.
    # declare_own_endpath_drift): sim/tests stay strict.
    declare_own_endpath_drift: bool = False

    def agent_for_player(self, player_id: int) -> AgentSpec:
        for agent in self.agents:
            if agent.player_id == player_id:
                return agent
        raise ConfigError(f"no agent for player {player_id}")


VALID_POLICIES = frozenset({"expansionist", "turtler", "llm", "planner"})
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
    completeness_gate = bool(match.get("completeness_gate", False))
    declare_own_endpath_drift = bool(
        match.get("declare_own_endpath_drift", False))

    if adapter not in VALID_ADAPTERS:
        raise ConfigError(f"unknown adapter {adapter!r}")
    if watchdog_mode not in VALID_WATCHDOG_MODES:
        raise ConfigError(f"unknown watchdog_mode {watchdog_mode!r}")
    if max_turns < 1 or checkpoint_every < 1:
        raise ConfigError("max_turns and checkpoint_every must be >= 1")

    agents: list[AgentSpec] = []
    seen_players: set[int] = set()
    seen_agent_ids: set[str] = set()
    for i, entry in enumerate(doc.get("agents", [])):
        where = f"agents[{i}]"
        agent = AgentSpec(
            agent_id=str(_require(entry, "agent_id", where)),
            player_id=int(_require(entry, "player_id", where)),
            policy=str(_require(entry, "policy", where)),
            seed=int(entry.get("seed", seed * 10 + i)),
            decision_mode=entry.get("decision_mode", "legacy"),
            model=entry.get("model"),  # display hint; parsed and ignored
            llm=_parse_llm(entry["llm"], where) if "llm" in entry else None,
            proposer=_parse_llm(entry["proposer"], f"{where}.proposer")
            if "proposer" in entry else None,
            case_base=_parse_case_base(entry["case_base"], f"{where}.case_base")
            if "case_base" in entry else None,
        )
        if agent.decision_mode not in ("legacy", "strategic_autopilot"):
            raise ConfigError(f"{where}: unknown decision_mode {agent.decision_mode!r}")
        if agent.decision_mode != "legacy" and agent.policy != "llm":
            raise ConfigError(f"{where}: strategic_autopilot requires policy llm")
        if agent.policy not in VALID_POLICIES:
            raise ConfigError(f"{where}: unknown policy {agent.policy!r}")
        if agent.policy == "llm" and agent.llm is None:
            raise ConfigError(f"{where}: policy 'llm' requires an llm: block")
        if agent.policy != "llm" and agent.llm is not None:
            raise ConfigError(
                f"{where}: llm: block on policy {agent.policy!r} — remove it "
                "or set policy: llm (fail loudly, never silently ignore)"
            )
        if agent.policy != "planner" and agent.proposer is not None:
            raise ConfigError(
                f"{where}: proposer: block on policy {agent.policy!r} — the "
                "proposer rides the planner's search (set policy: planner)"
            )
        if agent.policy != "planner" and agent.case_base is not None:
            raise ConfigError(
                f"{where}: case_base: block on policy {agent.policy!r} — the "
                "case prior rides the planner's search (set policy: planner)"
            )
        if agent.player_id in seen_players:
            raise ConfigError(f"{where}: duplicate player_id {agent.player_id}")
        if agent.agent_id in seen_agent_ids:
            # agent_id is the log's attribution key: a duplicate collapses
            # telemetry, llm_posts, and replay's call bucketing
            raise ConfigError(
                f"{where}: duplicate agent_id {agent.agent_id!r} — "
                "attribution (log, telemetry, replay) requires uniqueness"
            )
        seen_players.add(agent.player_id)
        seen_agent_ids.add(agent.agent_id)
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

    recall_runs_raw = match.get("recall_runs", [])
    if not isinstance(recall_runs_raw, list) or any(
            not isinstance(r, str) or not r for r in recall_runs_raw):
        raise ConfigError("match.recall_runs must be a list of match_id "
                          "strings when present")
    recall_runs: list[str] = []
    for prior in recall_runs_raw:
        if prior not in recall_runs:
            recall_runs.append(prior)
    if recall_runs:
        # ids resolve to directories under the runs root: the safe-id
        # charset (no '/', no leading dot) keeps path aliases from pointing
        # the corpus at itself or outside the root
        if any(not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", p)
               for p in recall_runs):
            raise ConfigError(
                "match.recall_runs entries must be bare match ids "
                "([A-Za-z0-9][A-Za-z0-9._-]{0,63}), not paths")
        # the referee bounds the recall digest's SERIALIZED size to 3900
        # chars (dropping whole lessons if needed), so a result cap at or
        # above this floor guarantees the model receives exactly what the
        # log recorded — character-count arithmetic alone cannot bound the
        # JSON-escaped form (backslashes double, non-ASCII sextuples)
        for agent in agents:
            if agent.policy == "llm" and agent.llm is not None \
                    and agent.llm.max_result_chars < 4000:
                raise ConfigError(
                    f"agents[{agent.agent_id}]: max_result_chars must be "
                    ">= 4000 when recall_runs is set — the recall digest "
                    "must reach the model untruncated")

    return MatchSpec(
        match_id=match_id, seed=seed, max_turns=max_turns, adapter=adapter,
        completeness_gate=completeness_gate,
        declare_own_endpath_drift=declare_own_endpath_drift,
        watchdog_mode=watchdog_mode, violation_limit=violation_limit,
        checkpoint_every=checkpoint_every, agents=agents, chaos=chaos,
        recall_runs=recall_runs,
    )


def load_config(path: Path | str) -> MatchSpec:
    with open(path, encoding="utf-8") as fh:
        doc = yaml.safe_load(fh)
    if not isinstance(doc, dict):
        raise ConfigError(f"{path}: config must be a mapping")
    return parse_config(doc)
