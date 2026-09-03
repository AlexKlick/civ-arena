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
from urllib.parse import urlsplit

import yaml


class ConfigError(ValueError):
    pass


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
    base_url = _require(block, "base_url", f"{where}.llm")
    api_key_env = _require(block, "api_key_env", f"{where}.llm")
    model_id = _require(block, "model_id", f"{where}.llm")
    if not isinstance(base_url, str):
        raise ConfigError(f"{where}.llm: base_url must be an http(s) URL")
    try:
        endpoint = urlsplit(base_url)
        port = endpoint.port
    except ValueError:
        raise ConfigError(f"{where}.llm: base_url is malformed") from None
    if (
        endpoint.scheme not in {"http", "https"}
        or endpoint.hostname is None
        or endpoint.username is not None
        or endpoint.password is not None
        or endpoint.query
        or endpoint.fragment
        or port is not None
        and not 1 <= port <= 65_535
    ):
        raise ConfigError(
            f"{where}.llm: base_url must be a credential-free http(s) origin/path"
        )
    if not isinstance(api_key_env, str) or not re.fullmatch(
        r"[A-Za-z_][A-Za-z0-9_]*", api_key_env
    ):
        raise ConfigError(f"{where}.llm: api_key_env must be an environment name")
    if not isinstance(model_id, str) or not model_id:
        raise ConfigError(f"{where}.llm: model_id must be a non-empty string")
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
    return LLMSpec(
        base_url=base_url, api_key_env=api_key_env, model_id=model_id,
        max_tokens=ints["max_tokens"], max_tool_rounds=ints["max_tool_rounds"],
        max_result_chars=ints["max_result_chars"],
        request_timeout_s=float(timeout), max_retries=ints["max_retries"],
        max_requests_per_match=ints["max_requests_per_match"],
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
    # Breaking V2 turn-core controls. Programmatic V1 research harnesses may
    # still construct MatchSpec directly, but every file-backed config must
    # state these values explicitly (load_config enforces that boundary).
    schema: int = 2
    execution_mode: str = "dag_tx"
    scored: bool = False
    max_graph_actions: int = 1024
    max_replans_per_turn: int = 2

    def agent_for_player(self, player_id: int) -> AgentSpec:
        for agent in self.agents:
            if agent.player_id == player_id:
                return agent
        raise ConfigError(f"no agent for player {player_id}")


VALID_POLICIES = frozenset({"expansionist", "turtler", "llm", "planner"})
VALID_ADAPTERS = frozenset({"simulator", "firetuner"})
VALID_WATCHDOG_MODES = frozenset({"flag_and_continue", "rollback"})
VALID_EXECUTION_MODES = frozenset({"dag_tx", "sequential", "legal_list", "dag"})
VALID_CHAOS_SPECS = frozenset(
    {
        "ambient_like_trap",
        "change_research",
        "flip_production",
        "move_uncommanded_unit",
        "spawn_free_unit",
        "steal_gold",
    }
)
VALID_CHAOS_HOOKS = frozenset({"begin_phase", "act", "end_phase"})

V2_TOP_KEYS = frozenset({"schema", "match", "agents", "chaos"})
V2_MATCH_KEYS = frozenset(
    {
        "adapter",
        "checkpoint_every",
        "execution_mode",
        "match_id",
        "max_graph_actions",
        "max_replans_per_turn",
        "max_turns",
        "recall_runs",
        "scored",
        "seed",
        "violation_limit",
        "watchdog_mode",
    }
)
V2_AGENT_KEYS = frozenset(
    {
        "agent_id",
        "case_base",
        "llm",
        "model",
        "player_id",
        "policy",
        "proposer",
        "seed",
    }
)
V2_LLM_KEYS = frozenset(
    {
        "api_key_env",
        "base_url",
        "max_requests_per_match",
        "max_result_chars",
        "max_retries",
        "max_tokens",
        "max_tool_rounds",
        "model_id",
        "request_timeout_s",
    }
)
V2_CHAOS_KEYS = frozenset({"hook", "offset", "spec"})


def _require(mapping: dict[str, Any], key: str, where: str) -> Any:
    if key not in mapping:
        raise ConfigError(f"{where}: missing required key {key!r}")
    return mapping[key]


def parse_config(doc: dict[str, Any], *, require_v2: bool = False) -> MatchSpec:
    schema = doc.get("schema")
    if schema is not None and schema != 2:
        raise ConfigError(f"config schema {schema!r} is not supported; expected 2")
    if require_v2 and schema != 2:
        raise ConfigError("file-backed match configs require top-level schema: 2")
    if schema == 2:
        unknown_top = set(doc) - V2_TOP_KEYS
        if unknown_top:
            raise ConfigError(f"config: unknown V2 keys {sorted(unknown_top)}")
    match = _require(doc, "match", "config")
    if not isinstance(match, dict):
        raise ConfigError("config.match must be a mapping")
    if schema == 2 and (unknown_match := set(match) - V2_MATCH_KEYS):
        raise ConfigError(f"match: unknown V2 keys {sorted(unknown_match)}")
    raw_match_id = _require(match, "match_id", "match")
    raw_seed = _require(match, "seed", "match")
    raw_max_turns = match.get("max_turns", 100)
    raw_adapter = match.get("adapter", "simulator")
    raw_watchdog_mode = match.get("watchdog_mode", "flag_and_continue")
    raw_violation_limit = match.get("violation_limit", 5)
    raw_checkpoint_every = match.get("checkpoint_every", 5)
    if schema == 2:
        if not isinstance(raw_match_id, str) or not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", raw_match_id
        ):
            raise ConfigError("match.match_id must be a bare V2 episode id")
        for name, value in (
            ("seed", raw_seed),
            ("max_turns", raw_max_turns),
            ("violation_limit", raw_violation_limit),
            ("checkpoint_every", raw_checkpoint_every),
        ):
            if not isinstance(value, int) or isinstance(value, bool):
                raise ConfigError(f"match.{name} must be an integer")
        if not isinstance(raw_adapter, str):
            raise ConfigError("match.adapter must be a string")
        if not isinstance(raw_watchdog_mode, str):
            raise ConfigError("match.watchdog_mode must be a string")
    match_id = str(raw_match_id)
    seed = int(raw_seed)
    max_turns = int(raw_max_turns)
    adapter = str(raw_adapter)
    watchdog_mode = str(raw_watchdog_mode)
    violation_limit = int(raw_violation_limit)
    checkpoint_every = int(raw_checkpoint_every)
    if schema == 2:
        for key in (
            "execution_mode",
            "scored",
            "max_graph_actions",
            "max_replans_per_turn",
        ):
            _require(match, key, "match")
    raw_execution_mode = match.get("execution_mode", "dag_tx")
    if schema == 2 and not isinstance(raw_execution_mode, str):
        raise ConfigError("match.execution_mode must be a string")
    execution_mode = str(raw_execution_mode)
    scored = match.get("scored", False)
    max_graph_actions = match.get("max_graph_actions", 1024)
    max_replans_per_turn = match.get("max_replans_per_turn", 2)

    if adapter not in VALID_ADAPTERS:
        raise ConfigError(f"unknown adapter {adapter!r}")
    if watchdog_mode not in VALID_WATCHDOG_MODES:
        raise ConfigError(f"unknown watchdog_mode {watchdog_mode!r}")
    if max_turns < 1 or checkpoint_every < 1 or violation_limit < 0:
        raise ConfigError(
            "max_turns and checkpoint_every must be >= 1; violation_limit must be >= 0"
        )
    if execution_mode not in VALID_EXECUTION_MODES:
        raise ConfigError(f"unknown execution_mode {execution_mode!r}")
    if schema == 2 and execution_mode != "dag_tx":
        raise ConfigError(
            "normal V2 match configs require execution_mode: dag_tx; "
            "control treatments are experiment-harness only"
        )
    if not isinstance(scored, bool):
        raise ConfigError("match.scored must be a boolean")
    if not isinstance(max_graph_actions, int) or isinstance(max_graph_actions, bool) \
            or not 1 <= max_graph_actions <= 65_536:
        raise ConfigError("match.max_graph_actions must be an integer in [1, 65536]")
    if not isinstance(max_replans_per_turn, int) \
            or isinstance(max_replans_per_turn, bool) \
            or not 0 <= max_replans_per_turn <= 100:
        raise ConfigError("match.max_replans_per_turn must be an integer in [0, 100]")

    agents_raw = doc.get("agents", [])
    if schema == 2 and not isinstance(agents_raw, list):
        raise ConfigError("config.agents must be a list")
    agents: list[AgentSpec] = []
    seen_players: set[int] = set()
    seen_agent_ids: set[str] = set()
    for i, entry in enumerate(agents_raw):
        where = f"agents[{i}]"
        if not isinstance(entry, dict):
            raise ConfigError(f"{where}: agent entry must be a mapping")
        if schema == 2 and (unknown_agent := set(entry) - V2_AGENT_KEYS):
            raise ConfigError(f"{where}: unknown V2 keys {sorted(unknown_agent)}")
        if schema == 2:
            for block_name in ("llm", "proposer"):
                block = entry.get(block_name)
                if isinstance(block, dict) and (
                    unknown_llm := set(block) - V2_LLM_KEYS
                ):
                    raise ConfigError(
                        f"{where}.{block_name}: unknown V2 keys {sorted(unknown_llm)}"
                    )
            case_base = entry.get("case_base")
            if isinstance(case_base, dict) and (unknown_case := set(case_base) - {"path"}):
                raise ConfigError(
                    f"{where}.case_base: unknown V2 keys {sorted(unknown_case)}"
                )
        raw_agent_id = _require(entry, "agent_id", where)
        raw_player_id = _require(entry, "player_id", where)
        raw_policy = _require(entry, "policy", where)
        raw_agent_seed = entry.get("seed", seed * 10 + i)
        raw_model = entry.get("model")
        if schema == 2:
            if not isinstance(raw_agent_id, str) or not re.fullmatch(
                r"[A-Za-z0-9][A-Za-z0-9._:@+,-]{0,159}", raw_agent_id
            ):
                raise ConfigError(f"{where}.agent_id is not a V2 identifier")
            if (
                not isinstance(raw_player_id, int)
                or isinstance(raw_player_id, bool)
                or raw_player_id < 0
            ):
                raise ConfigError(f"{where}.player_id must be a non-negative integer")
            if not isinstance(raw_policy, str):
                raise ConfigError(f"{where}.policy must be a string")
            if not isinstance(raw_agent_seed, int) or isinstance(raw_agent_seed, bool):
                raise ConfigError(f"{where}.seed must be an integer")
            if raw_model is not None and not isinstance(raw_model, str):
                raise ConfigError(f"{where}.model must be a string or null")
        agent = AgentSpec(
            agent_id=str(raw_agent_id),
            player_id=int(raw_player_id),
            policy=str(raw_policy),
            seed=int(raw_agent_seed),
            model=raw_model,  # display hint; parsed and ignored
            llm=_parse_llm(entry["llm"], where) if "llm" in entry else None,
            proposer=_parse_llm(entry["proposer"], f"{where}.proposer")
            if "proposer" in entry else None,
            case_base=_parse_case_base(entry["case_base"], f"{where}.case_base")
            if "case_base" in entry else None,
        )
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

    chaos_raw = doc.get("chaos", [])
    if schema == 2 and not isinstance(chaos_raw, list):
        raise ConfigError("config.chaos must be a list")
    if schema == 2 and len(chaos_raw) > 1024:
        raise ConfigError("config.chaos must contain at most 1024 events")
    chaos: list[ChaosSpec] = []
    for i, entry in enumerate(chaos_raw):
        if not isinstance(entry, dict):
            raise ConfigError(f"chaos[{i}]: entry must be a mapping")
        if schema == 2 and (unknown_chaos := set(entry) - V2_CHAOS_KEYS):
            raise ConfigError(f"chaos[{i}]: unknown V2 keys {sorted(unknown_chaos)}")
        raw_spec = _require(entry, "spec", f"chaos[{i}]")
        raw_hook = entry.get("hook", "act")
        raw_offset = entry.get("offset", 0)
        if schema == 2:
            if not isinstance(raw_spec, str) or raw_spec not in VALID_CHAOS_SPECS:
                raise ConfigError(f"chaos[{i}].spec is not a registered mutation")
            if not isinstance(raw_hook, str) or raw_hook not in VALID_CHAOS_HOOKS:
                raise ConfigError(f"chaos[{i}].hook is not a registered hook")
            if (
                not isinstance(raw_offset, int)
                or isinstance(raw_offset, bool)
                or raw_offset < 0
            ):
                raise ConfigError(f"chaos[{i}].offset must be a non-negative integer")
        chaos.append(
            ChaosSpec(
                spec=str(raw_spec),
                hook=str(raw_hook),
                offset=int(raw_offset),
            )
        )

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
        watchdog_mode=watchdog_mode, violation_limit=violation_limit,
        checkpoint_every=checkpoint_every, agents=agents, chaos=chaos,
        recall_runs=recall_runs, schema=2 if schema == 2 else 1,
        execution_mode=execution_mode, scored=scored,
        max_graph_actions=max_graph_actions,
        max_replans_per_turn=max_replans_per_turn,
    )


def load_config(path: Path | str) -> MatchSpec:
    with open(path, encoding="utf-8") as fh:
        doc = yaml.safe_load(fh)
    if not isinstance(doc, dict):
        raise ConfigError(f"{path}: config must be a mapping")
    return parse_config(doc, require_v2=True)
