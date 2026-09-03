"""Observation-only policy runtimes for the V2 turn boundary.

The mature scripted, planner, and LLM policies still speak the small tool
facade they were built against.  V2 preserves that policy code, but replaces
the facade's authority: reads are reconstructed solely from ``ObservationV2``
and action calls append untrusted ``ActionIntentV2`` values.  No adapter,
referee, lease, or mutation callable is reachable through this module.

Diary and strategy operations remain policy-side services.  They mutate only
their typed in-memory stores and are retained as canonical operation records so
the V2 coordinator can custody/restore them without granting game authority.
"""

from __future__ import annotations

import contextlib
import copy
import json
import random
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable
from urllib.parse import urlparse

from civ_arena.agents.runtime import AgentProfile, build_runtime
from civ_arena.arena.diary import MAX_DIARY_CHARS, DiaryStore
from civ_arena.arena.telemetry import TelemetryRegistry
from civ_arena.canonical import canonical, sha256_hex
from civ_arena.game.sim.state import parse_key
from civ_arena.recall import RecallCorpus
from civ_arena.strategy.store import StrategyStore
from civ_arena.v2.contracts import (
    PLAYER_ACTION_KINDS_V2,
    SYSTEM_ACTION_KINDS_V2,
    ActionIntentV2,
    ActionKindV2,
    EntityRefV2,
    EntityTypeV2,
    EventTypeV2,
    FactSubjectScopeV2,
    LegalActionV2,
    PolicyDescriptorV2,
    PolicyKindV2,
    PolicyOperationV2,
    PolicyRuntimeKindV2,
    PolicySpendV2,
    PolicyStateV2,
    RandomStateV2,
    TurnContextV2,
    TurnProposalV2,
)
from civ_arena.v2.enumeration import ObservationIndexV2
from civ_arena.v2.schemas import ContractError

RECALL_DIGEST_BUDGET = 3900
ALL_ACTION_KINDS_V2 = tuple(ActionKindV2)


@runtime_checkable
class AgentRuntimeV2(Protocol):
    """The only policy execution seam used by the V2 coordinator."""

    descriptor: PolicyDescriptorV2

    async def propose_turn(self, context: TurnContextV2) -> TurnProposalV2: ...


class SystemHousekeepingRuntimeV2:
    """Observation-only proposer for player-visible live turn blockers.

    The runtime deliberately proposes at most one action.  Each accepted
    mutation must be re-observed and the entire graph recompiled before the
    next blocker can be considered, exactly like an untrusted player policy.
    """

    def __init__(self) -> None:
        self.descriptor = PolicyDescriptorV2.create(
            policy_kind=PolicyKindV2.SYSTEM,
            policy_version="live-housekeeping-v2.0",
            artifact_digest=sha256_hex(
                canonical(
                    {
                        "algorithm": "one-mandatory-system-action-per-proposal",
                        "action_kinds": [item.value for item in SYSTEM_ACTION_KINDS_V2],
                    }
                )
            ),
            registered_action_kinds=SYSTEM_ACTION_KINDS_V2,
        )

    async def propose_turn(self, context: TurnContextV2) -> TurnProposalV2:
        if context.policy != self.descriptor:
            raise ContractError("system turn context policy descriptor mismatch")
        candidates = sorted(
            (
                action
                for action in context.legal_actions.actions
                if action.mandatory
                and action.action_kind in SYSTEM_ACTION_KINDS_V2
            ),
            key=lambda action: action.action_id,
        )
        intents: list[ActionIntentV2] = []
        if candidates:
            action = candidates[0]
            intents.append(
                ActionIntentV2.create(
                    action.action_kind,
                    action.actor,
                    target=action.target,
                    parameters=dict(action.parameters),
                    proposal_index=0,
                )
            )
        return TurnProposalV2.create(
            policy_id=self.descriptor.descriptor_id,
            observation_id=context.observation.observation_id,
            intents=intents,
        )


def _safe_scalar(value: Any) -> bool:
    return isinstance(value, str | int | bool) and not isinstance(value, float)


def _safe_claim_args(args: Mapping[str, Any]) -> dict[str, Any]:
    """Mirror the V1 claim boundary without allowing arbitrary object graphs."""

    return {
        key: value if _safe_scalar(value) or value is None else None
        for key, value in args.items()
    }


def _thaw(value: Any) -> Any:
    if isinstance(value, tuple):
        return [_thaw(item) for item in value]
    return copy.deepcopy(value)


def _policy_json_value(value: Any, path: str = "$policy") -> Any:
    """Normalize mature policy state into canonical JSON without ambiguity."""

    if value is None or isinstance(value, str | int | bool):
        return value
    if isinstance(value, float):
        raise ContractError(f"policy state contains a float at {path}")
    if isinstance(value, list | tuple):
        return [
            _policy_json_value(child, f"{path}[{index}]")
            for index, child in enumerate(value)
        ]
    if isinstance(value, Mapping):
        normalized: dict[str, Any] = {}
        for raw_key, child in value.items():
            if isinstance(raw_key, str):
                key = raw_key
            elif isinstance(raw_key, int) and not isinstance(raw_key, bool):
                key = str(raw_key)
            else:
                raise ContractError(f"policy state has an invalid key at {path}")
            if key in normalized:
                raise ContractError(
                    f"policy state key normalization collides at {path}.{key}"
                )
            normalized[key] = _policy_json_value(child, f"{path}.{key}")
        return normalized
    raise ContractError(f"policy state contains a non-JSON value at {path}")


@dataclass
class PolicyServicesV2:
    """Policy-only memory, telemetry, and spend accounting.

    None of these objects contains an environment or execution facet.  The
    operation record is the durable-source input used by the coordinator; it
    intentionally contains no model transport body, credentials, or private
    game state.
    """

    agent_id: str
    player_id: int
    diary: DiaryStore = field(default_factory=DiaryStore)
    strategy: StrategyStore = field(default_factory=StrategyStore)
    recall: RecallCorpus | None = None
    telemetry: TelemetryRegistry | None = None
    claim_sequence: int = 0
    model_posts: int = 0
    operations: list[dict[str, Any]] = field(default_factory=list)
    _durable_cursor: int = field(default=0, init=False, repr=False)
    _policy_id: str | None = field(default=None, init=False, repr=False)
    _recorder: Any = field(default=None, init=False, repr=False)
    _current_turn: int = field(default=0, init=False, repr=False)

    def bind_custody(self, policy_id: str, recorder: Any) -> None:
        """Bind the append-only V2 recorder after it acquires the writer lock."""

        self._policy_id = policy_id
        self._recorder = recorder

    def unbind_custody(self) -> None:
        self._recorder = None

    def begin_policy_turn(self, turn: int) -> None:
        self._current_turn = turn

    def note_model_post(self) -> None:
        self.model_posts += 1
        if self._recorder is None or self._policy_id is None:
            return
        spend = PolicySpendV2.create(
            policy_id=self._policy_id,
            agent_id=self.agent_id,
            player_id=self.player_id,
            turn=self._current_turn,
            attempt=self.model_posts,
        )
        self._recorder.record_reference(
            EventTypeV2.POLICY_SPEND_RECORDED,
            spend,
            turn_id=self._current_turn,
            correlation_id=f"turn-{self._current_turn}-p{self.player_id}",
        )

    def _record(
        self,
        tool: str,
        args: Mapping[str, Any],
        result: Mapping[str, Any],
        *,
        turn: int,
    ) -> None:
        record = {
            "agent_id": self.agent_id,
            "player_id": self.player_id,
            "turn": turn,
            "sequence": len(self.operations),
            "tool": tool,
            "args": copy.deepcopy(dict(args)),
            "result": copy.deepcopy(dict(result)),
        }
        # Refuse policy records that cannot enter a canonical V2 artifact.
        canonical(record)
        self.operations.append(record)

    def operation_delta(self) -> tuple[PolicyOperationV2, ...]:
        return tuple(
            PolicyOperationV2.create(
                agent_id=record["agent_id"],
                player_id=record["player_id"],
                turn=record["turn"],
                sequence=record["sequence"],
                tool=record["tool"],
                args=record["args"],
                result=record["result"],
            )
            for record in self.operations[self._durable_cursor :]
        )

    def mark_operations_durable(self) -> None:
        self._durable_cursor = len(self.operations)

    def restore_operations(self, operations: tuple[PolicyOperationV2, ...]) -> None:
        """Rebuild policy stores from trusted operation deltas, idempotently."""

        claim_tools = {"set_goal", "record_prediction", "record_lesson"}
        for operation in operations:
            if operation.agent_id != self.agent_id or operation.player_id != self.player_id:
                raise ContractError("policy operation does not belong to this service")
            record = {
                "agent_id": operation.agent_id,
                "player_id": operation.player_id,
                "turn": operation.turn,
                "sequence": operation.sequence,
                "tool": operation.tool,
                "args": operation.args,
                "result": operation.result,
            }
            if operation.sequence < len(self.operations):
                if self.operations[operation.sequence] != record:
                    raise ContractError("policy operation sequence has conflicting bytes")
                continue
            if operation.sequence != len(self.operations):
                raise ContractError("policy operation sequence is not contiguous")
            if operation.tool == "write_diary" and operation.result.get("status") == "accepted":
                text = operation.args.get("text")
                if not isinstance(text, str):
                    raise ContractError("accepted diary operation has no text")
                self.diary.write(self.player_id, text)
            elif operation.tool in claim_tools:
                replayed = self.strategy.apply_claim(
                    operation.tool,
                    self.player_id,
                    operation.args,
                    operation.turn,
                    self.claim_sequence,
                )
                if replayed != operation.result:
                    raise ContractError("policy claim operation does not replay exactly")
                self.claim_sequence += 1
            self.operations.append(record)
        self._durable_cursor = len(self.operations)

    def _note_call(self, tool: str, ok: bool) -> None:
        if self.telemetry is not None:
            # Wall-clock timing is an envelope concern, not deterministic
            # semantic evidence. V2 policy-facade accounting uses zero here.
            self.telemetry.note_call(self.agent_id, tool, 0, ok)

    def write_diary(self, text: Any, *, turn: int) -> dict[str, Any]:
        if (
            not isinstance(text, str)
            or not 1 <= len(text.strip()) <= MAX_DIARY_CHARS
            or len(text) > MAX_DIARY_CHARS
        ):
            result = {
                "status": "rejected",
                "rejection": "args_invalid",
                "tool": "write_diary",
            }
            args = {"text": text if isinstance(text, str) else None}
        else:
            self.diary.write(self.player_id, text)
            result = {
                "status": "accepted",
                "tool": "write_diary",
                "chars": len(text),
            }
            args = {"text": text}
        self._record("write_diary", args, result, turn=turn)
        self._note_call("write_diary", result["status"] == "accepted")
        return result

    def apply_claim(
        self,
        tool: str,
        args: Mapping[str, Any],
        *,
        turn: int,
    ) -> dict[str, Any]:
        safe_args = _safe_claim_args(args)
        result = self.strategy.apply_claim(
            tool,
            self.player_id,
            safe_args,
            turn,
            self.claim_sequence,
        )
        self.claim_sequence += 1
        self._record(tool, safe_args, result, turn=turn)
        self._note_call(tool, result.get("status") == "accepted")
        return result

    def get_strategy(self, *, turn: int) -> dict[str, Any]:
        result = {
            "status": "accepted",
            "tool": "get_strategy",
            "strategy": self.strategy.view_for(self.player_id),
        }
        self._record("get_strategy", {}, result, turn=turn)
        self._note_call("get_strategy", True)
        return result

    def recall_lessons(self, query: Any, *, turn: int) -> dict[str, Any]:
        args = {"query": query if isinstance(query, str) else None}
        if self.recall is None:
            result: dict[str, Any] = {
                "status": "rejected",
                "rejection": "tool_unavailable",
                "reason": "recall is not configured for this match",
            }
        elif not isinstance(query, str):
            result = {
                "status": "rejected",
                "rejection": "args_invalid",
                "tool": "recall_lessons",
            }
        else:
            lessons = self.recall.query(self.agent_id, query)
            recalled = {"query": query, "lessons": lessons}
            while lessons and len(
                json.dumps(recalled, sort_keys=True, ensure_ascii=True)
            ) > RECALL_DIGEST_BUDGET:
                lessons.pop()
                recalled = {"query": query, "lessons": lessons}
            result = {
                "status": "accepted",
                "tool": "recall_lessons",
                "recalled": recalled,
            }
        self._record("recall_lessons", args, result, turn=turn)
        self._note_call("recall_lessons", result["status"] == "accepted")
        return result


def _facts(index: ObservationIndexV2, ref: EntityRefV2) -> dict[str, Any]:
    return {key: _thaw(value) for key, value in index.facts_for(ref).items()}


class ObservationProjectionV2:
    """Legacy-shaped read views derived from one V2 observation only."""

    def __init__(self, context: TurnContextV2) -> None:
        self.context = context
        self.observation = context.observation
        self.index = ObservationIndexV2(self.observation)
        entity_id = self.observation.observing_player.entity_id
        if not entity_id.startswith("p") or not entity_id[1:].isdigit():
            raise ContractError("policy observation has no canonical player identity")
        self.player_id = int(entity_id[1:])

    def overview(self) -> dict[str, Any]:
        player = self.observation.observing_player
        own = _facts(self.index, player)
        public_players: list[dict[str, Any]] = []
        for ref in self.index.entities(EntityTypeV2.PLAYER):
            values = _facts(self.index, ref)
            if not ref.entity_id.startswith("p") or not ref.entity_id[1:].isdigit():
                continue
            public_players.append(
                {
                    "player_id": int(ref.entity_id[1:]),
                    "civ_name": values.get("civ_name", "unknown"),
                    "alive": values.get("alive", True),
                }
            )
        public_players.sort(key=lambda item: item["player_id"])
        return {
            "turn": self.observation.turn,
            "you": {
                "player_id": self.player_id,
                "civ_name": own.get("civ_name", "unknown"),
                "gold": own.get("gold", 0),
                "researched": list(own.get("researched", ())),
                "researching": own.get("researching", ""),
            },
            "public": {
                "turn": self.observation.turn,
                "players": public_players,
            },
        }

    def units(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for ref in self.index.entities(EntityTypeV2.UNIT):
            values = _facts(self.index, ref)
            values.pop("available", None)
            row = {"unit_id": ref.entity_id, **values}
            rows.append(row)
        return sorted(rows, key=lambda row: row["unit_id"])

    def cities(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for ref in self.index.entities(EntityTypeV2.CITY):
            values = _facts(self.index, ref)
            scope = self.index.scope(ref)
            row: dict[str, Any] = {"city_id": ref.entity_id, **values}
            if scope is FactSubjectScopeV2.OWNED:
                owner = row.pop("owner_id", self.player_id)
                row["owner"] = owner
                coord = row.get("coord")
                if isinstance(coord, str):
                    row["q"], row["r"] = parse_key(coord)
            elif "hp_bucket" in row:
                # The V1 planner expects a foreign city's visible HP under
                # ``hp``. Reconstruct a deterministic bucket midpoint; never
                # recover or infer the hidden exact value.
                bucket = row.pop("hp_bucket")
                if type(bucket) is int:
                    row["hp"] = min(100, max(1, bucket * 25 + 12))
            rows.append(row)
        return sorted(rows, key=lambda row: row["city_id"])

    def visible_map(self) -> dict[str, Any]:
        tiles: dict[str, dict[str, Any]] = {}
        for ref in self.index.entities(EntityTypeV2.TILE):
            values = _facts(self.index, ref)
            coord = values.get("coord")
            if isinstance(coord, str):
                tiles[coord] = values
        return {"turn": self.observation.turn, "tiles": dict(sorted(tiles.items()))}

    def research(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for ref in self.index.entities(EntityTypeV2.TECHNOLOGY):
            values = _facts(self.index, ref)
            if values.get("available") is True:
                rows.append(
                    {
                        "tech_id": ref.entity_id,
                        "cost": values.get("cost", 0),
                        "prereq": list(values.get("prereq", ())),
                    }
                )
        return sorted(rows, key=lambda row: row["tech_id"])

    def production(self, city_id: str) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for ref in self.index.entities(EntityTypeV2.PRODUCTION_ITEM):
            values = _facts(self.index, ref)
            if values.get("available") is not True or values.get("city_id") != city_id:
                continue
            item_id = ref.entity_id.split(":", 1)[1]
            row = {
                "item_id": item_id,
                "cost": values.get("cost", 0),
                "kind": values.get("kind", "unknown"),
            }
            if "turns_remaining" in values:
                row["turns"] = values["turns_remaining"]
            if "purchase_cost" in values:
                row["purchase_cost"] = values["purchase_cost"]
            rows.append(row)
        return sorted(rows, key=lambda row: row["item_id"])


def _fallback_refs(
    observation_player: EntityRefV2,
    action_kind: ActionKindV2,
    parameters: Mapping[str, Any],
) -> tuple[EntityRefV2, EntityRefV2 | None]:
    if action_kind in {
        ActionKindV2.MOVE_UNIT,
        ActionKindV2.ATTACK,
        ActionKindV2.FORTIFY,
        ActionKindV2.FOUND_CITY,
    }:
        actor = EntityRefV2(EntityTypeV2.UNIT, str(parameters["unit_id"]))
    elif action_kind in {
        ActionKindV2.PURCHASE,
        ActionKindV2.SET_CITY_PRODUCTION,
    }:
        actor = EntityRefV2(EntityTypeV2.CITY, str(parameters["city_id"]))
    else:
        actor = observation_player

    target: EntityRefV2 | None = None
    if action_kind is ActionKindV2.ATTACK:
        target = EntityRefV2(EntityTypeV2.UNIT, str(parameters["target_id"]))
    elif action_kind is ActionKindV2.SET_RESEARCH:
        target = EntityRefV2(EntityTypeV2.TECHNOLOGY, str(parameters["tech_id"]))
    elif action_kind in {
        ActionKindV2.PURCHASE,
        ActionKindV2.SET_CITY_PRODUCTION,
    }:
        target = EntityRefV2(
            EntityTypeV2.PRODUCTION_ITEM,
            f"{parameters['city_id']}:{parameters['item_id']}",
        )
    return actor, target


class _ProposalCollector:
    def __init__(self, context: TurnContextV2, services: PolicyServicesV2) -> None:
        self.context = context
        self.services = services
        self.projection = ObservationProjectionV2(context)
        self.intents: list[ActionIntentV2] = []

    def _note(self, tool: str, ok: bool) -> None:
        self.services._note_call(tool, ok)  # noqa: SLF001 - same trust boundary

    async def observe(self, tool: str, value: Any) -> Any:
        self._note(tool, True)
        return copy.deepcopy(value)

    def _matching_action(
        self,
        action_kind: ActionKindV2,
        parameters: Mapping[str, Any],
    ) -> LegalActionV2 | None:
        for action in self.context.legal_actions.actions:
            if action.action_kind is not action_kind:
                continue
            actual = dict(action.parameters)
            if actual == dict(parameters):
                return action
            if (
                action_kind is ActionKindV2.FOUND_CITY
                and actual.get("unit_id") == parameters.get("unit_id")
            ):
                # The enumerator owns the canonical city name. A legacy
                # policy's cosmetic name cannot create an unregistered action.
                return action
        return None

    async def action(
        self,
        action_kind: ActionKindV2,
        parameters: Mapping[str, Any],
    ) -> dict[str, Any]:
        match = self._matching_action(action_kind, parameters)
        intent_parameters = dict(match.parameters) if match is not None else dict(parameters)
        try:
            actor, target = (
                (match.actor, match.target)
                if match is not None
                else _fallback_refs(
                    self.context.observation.observing_player,
                    action_kind,
                    intent_parameters,
                )
            )
            intent = ActionIntentV2.create(
                action_kind,
                actor,
                target=target,
                parameters=intent_parameters,
                proposal_index=len(self.intents),
            )
        except (ContractError, KeyError, TypeError, ValueError):
            self._note(action_kind.value, False)
            return {
                "status": "rejected",
                "rejection": "args_invalid",
                "tool": action_kind.value,
            }
        self.intents.append(intent)
        accepted = match is not None or action_kind is ActionKindV2.END_TURN
        self._note(action_kind.value, accepted)
        if accepted:
            return {
                "status": "accepted",
                "tool": action_kind.value,
                "proposal_index": intent.proposal_index,
            }
        return {
            "status": "rejected",
            "rejection": "action_absent",
            "tool": action_kind.value,
            "proposal_index": intent.proposal_index,
        }

    def ensure_mandatory_intents(self) -> None:
        """Add deterministic policy-side defaults the legacy policy omitted.

        A V2 turn cannot be closed while an observable mandatory decision is
        unresolved. Some mature policies relied on the V1 referee tolerating
        that omission. The compatibility runtime, which is itself untrusted
        policy code, chooses the smallest stable legal action per mandatory
        actor. The executor still independently maps and authorizes it.
        """

        proposed = {(item.action_kind, item.actor) for item in self.intents}
        choices: dict[tuple[ActionKindV2, EntityRefV2], list[LegalActionV2]] = {}
        for action in self.context.legal_actions.actions:
            if action.mandatory:
                choices.setdefault((action.action_kind, action.actor), []).append(action)
        for key in sorted(
            choices,
            key=lambda item: (item[0].value, item[1].entity_type.value, item[1].entity_id),
        ):
            if key in proposed:
                continue
            action = min(choices[key], key=lambda item: item.action_id)
            self.intents.append(
                ActionIntentV2.create(
                    action.action_kind,
                    action.actor,
                    target=action.target,
                    parameters=dict(action.parameters),
                    proposal_index=len(self.intents),
                )
            )


_TOOL_NAMES = (
    "attack",
    "end_turn",
    "fortify",
    "found_city",
    "get_available_production",
    "get_available_research",
    "get_cities",
    "get_overview",
    "get_strategy",
    "get_units",
    "get_visible_map",
    "move_unit",
    "purchase",
    "recall_lessons",
    "record_lesson",
    "record_prediction",
    "set_city_production",
    "set_goal",
    "set_research",
    "write_diary",
)


def _proposal_facade(collector: _ProposalCollector) -> Any:
    """Build a closure-backed facade with no context/service attributes."""

    projection = collector.projection
    services = collector.services
    turn = collector.context.observation.turn

    async def get_overview() -> dict[str, Any]:
        return await collector.observe("get_overview", projection.overview())

    async def get_units() -> list[dict[str, Any]]:
        return await collector.observe("get_units", projection.units())

    async def get_cities() -> list[dict[str, Any]]:
        return await collector.observe("get_cities", projection.cities())

    async def get_visible_map() -> dict[str, Any]:
        return await collector.observe("get_visible_map", projection.visible_map())

    async def get_available_research() -> list[dict[str, Any]]:
        return await collector.observe("get_available_research", projection.research())

    async def get_available_production(city_id: str) -> list[dict[str, Any]]:
        return await collector.observe(
            "get_available_production", projection.production(city_id)
        )

    async def get_strategy() -> dict[str, Any]:
        return services.get_strategy(turn=turn)

    async def move_unit(
        unit_id: str, dest: str, *, idempotency_key: str | None = None
    ) -> dict[str, Any]:
        _ = idempotency_key
        return await collector.action(
            ActionKindV2.MOVE_UNIT, {"unit_id": unit_id, "dest": dest}
        )

    async def attack(
        unit_id: str, target_id: str, *, idempotency_key: str | None = None
    ) -> dict[str, Any]:
        _ = idempotency_key
        return await collector.action(
            ActionKindV2.ATTACK, {"unit_id": unit_id, "target_id": target_id}
        )

    async def fortify(
        unit_id: str, *, idempotency_key: str | None = None
    ) -> dict[str, Any]:
        _ = idempotency_key
        return await collector.action(ActionKindV2.FORTIFY, {"unit_id": unit_id})

    async def found_city(
        unit_id: str,
        name: str | None = None,
        *,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        _ = idempotency_key
        return await collector.action(
            ActionKindV2.FOUND_CITY,
            {"unit_id": unit_id, "name": name or f"Arena-{unit_id}"},
        )

    async def set_research(
        tech_id: str, *, idempotency_key: str | None = None
    ) -> dict[str, Any]:
        _ = idempotency_key
        return await collector.action(ActionKindV2.SET_RESEARCH, {"tech_id": tech_id})

    async def set_city_production(
        city_id: str,
        item_id: str,
        *,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        _ = idempotency_key
        return await collector.action(
            ActionKindV2.SET_CITY_PRODUCTION,
            {"city_id": city_id, "item_id": item_id},
        )

    async def purchase(
        city_id: str,
        item_id: str,
        *,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        _ = idempotency_key
        return await collector.action(
            ActionKindV2.PURCHASE, {"city_id": city_id, "item_id": item_id}
        )

    async def end_turn() -> dict[str, Any]:
        return await collector.action(ActionKindV2.END_TURN, {})

    async def write_diary(text: str) -> dict[str, Any]:
        return services.write_diary(text, turn=turn)

    async def set_goal(
        text: str,
        goal_id: str = "",
        by_turn: int = 0,
        metric: str = "",
        target: int = 0,
        status: str = "active",
        confidence: int = 50,
    ) -> dict[str, Any]:
        return services.apply_claim(
            "set_goal",
            {
                "text": text,
                "goal_id": goal_id,
                "by_turn": by_turn,
                "metric": metric,
                "target": target,
                "status": status,
                "confidence": confidence,
            },
            turn=turn,
        )

    async def record_prediction(
        text: str,
        review_turn: int,
        prediction_id: str = "",
        subject_id: str = "",
        metric: str = "",
        target: int = 0,
        confidence: int = 50,
    ) -> dict[str, Any]:
        return services.apply_claim(
            "record_prediction",
            {
                "text": text,
                "review_turn": review_turn,
                "prediction_id": prediction_id,
                "subject_id": subject_id,
                "metric": metric,
                "target": target,
                "confidence": confidence,
            },
            turn=turn,
        )

    async def record_lesson(text: str, about: str = "") -> dict[str, Any]:
        return services.apply_claim(
            "record_lesson", {"text": text, "about": about}, turn=turn
        )

    async def recall_lessons(query: str) -> dict[str, Any]:
        return services.recall_lessons(query, turn=turn)

    bound: dict[str, Callable[..., Any]] = {
        name: value
        for name, value in locals().items()
        if name in _TOOL_NAMES and callable(value)
    }

    class _Facade:
        __slots__ = ("__bound",)

        def __init__(self, methods: Mapping[str, Callable[..., Any]]) -> None:
            object.__setattr__(self, "_Facade__bound", dict(methods))

        def names(self) -> list[str]:
            return sorted(object.__getattribute__(self, "_Facade__bound"))

        def __getattr__(self, name: str) -> Any:
            if name.startswith("_"):
                raise AttributeError(name)
            try:
                return object.__getattribute__(self, "_Facade__bound")[name]
            except KeyError:
                raise AttributeError(f"no such tool: {name}") from None

        def __dir__(self) -> list[str]:
            return sorted(set(_TOOL_NAMES) | {"names"})

    return _Facade(bound)


class InMemoryPlannerJournalV2:
    """Planner journal with V2 custody instead of a mutable side file."""

    def __init__(self) -> None:
        self._docs: list[dict[str, Any]] = []

    def append(self, turn: int, payload: Mapping[str, Any]) -> None:
        doc = _policy_json_value(dict(payload))
        assert isinstance(doc, dict)
        if doc.get("turn") != turn:
            raise ContractError("planner journal payload turn mismatch")
        # A same-turn re-proposal replaces the earlier policy snapshot just as
        # the frozen file journal does after a rewind.
        self._docs = [item for item in self._docs if item["turn"] < turn]
        self._docs.append(json.loads(canonical(doc)))

    def replay_upto(self, turn: int) -> list[dict[str, Any]]:
        return [copy.deepcopy(item) for item in self._docs if item["turn"] < turn]

    def to_doc(self) -> list[dict[str, Any]]:
        return copy.deepcopy(self._docs)

    def restore(self, docs: Any) -> None:
        if not isinstance(docs, list):
            raise ContractError("planner runtime state journal must be a list")
        restored: list[dict[str, Any]] = []
        prior = -1
        for raw in docs:
            if not isinstance(raw, dict):
                raise ContractError("planner runtime state journal entry must be an object")
            doc = json.loads(canonical(raw))
            turn = doc.get("turn")
            if not isinstance(turn, int) or isinstance(turn, bool) or turn <= prior:
                raise ContractError("planner runtime state journal turns must increase")
            prior = turn
            restored.append(doc)
        self._docs = restored


@dataclass
class LegacyPolicyRuntimeV2:
    """Run one mature V1 policy as an observation-only V2 proposer."""

    runtime: Any
    descriptor: PolicyDescriptorV2
    services: PolicyServicesV2
    journal: InMemoryPlannerJournalV2 | None = None

    async def propose_turn(self, context: TurnContextV2) -> TurnProposalV2:
        if context.policy != self.descriptor:
            raise ContractError("turn context policy descriptor mismatch")
        entity_id = context.observation.observing_player.entity_id
        if entity_id != f"p{self.services.player_id}":
            raise ContractError("policy service identity does not match observation")
        self.services.begin_policy_turn(context.observation.turn)
        begin = getattr(self.runtime, "begin_turn", None)
        if callable(begin):
            begin(context.observation.turn)
        collector = _ProposalCollector(context, self.services)
        await self.runtime.take_turn(_proposal_facade(collector))
        collector.ensure_mandatory_intents()
        return TurnProposalV2.create(
            policy_id=self.descriptor.descriptor_id,
            observation_id=context.observation.observation_id,
            intents=collector.intents,
        )

    def snapshot_state(self, *, turn: int, proposal_id: str | None) -> PolicyStateV2:
        runtime_kind = PolicyRuntimeKindV2(self.descriptor.policy_kind.value)
        runtime_state: dict[str, Any] = {}
        if runtime_kind is PolicyRuntimeKindV2.PLANNER:
            runtime_state = {
                "journal": self.journal.to_doc() if self.journal is not None else [],
                "trace": _policy_json_value(getattr(self.runtime, "trace", [])),
                "proposer_client_posts": int(
                    getattr(getattr(self.runtime, "proposer", None), "posts_sent", 0)
                ),
            }
        elif runtime_kind is PolicyRuntimeKindV2.LLM:
            runtime_state = {
                "client_posts": int(
                    getattr(getattr(self.runtime, "client", None), "posts_sent", 0)
                )
            }
        rng = getattr(self.runtime, "rng", None)
        telemetry = {}
        if self.services.telemetry is not None:
            snapshot = self.services.telemetry.snapshot()
            if self.services.agent_id in snapshot:
                telemetry[self.services.agent_id] = {
                    key: value
                    for key, value in snapshot[self.services.agent_id].items()
                    if key != "total_ms"
                }
        return PolicyStateV2.create(
            policy_id=self.descriptor.descriptor_id,
            agent_id=self.services.agent_id,
            player_id=self.services.player_id,
            turn=turn,
            proposal_id=proposal_id,
            runtime_kind=runtime_kind,
            rng_state=(RandomStateV2.from_random(rng) if isinstance(rng, random.Random) else None),
            runtime_state=runtime_state,
            operations=self.services.operation_delta(),
            telemetry=telemetry,
            model_posts=self.services.model_posts,
        )

    def restore_state(self, state: PolicyStateV2, *, spend_attempts: int) -> None:
        if (
            state.policy_id != self.descriptor.descriptor_id
            or state.agent_id != self.services.agent_id
            or state.player_id != self.services.player_id
        ):
            raise ContractError("policy state identity does not match configured runtime")
        expected_kind = PolicyRuntimeKindV2(self.descriptor.policy_kind.value)
        if state.runtime_kind is not expected_kind:
            raise ContractError("policy runtime state kind does not match descriptor")
        if state.rng_state is not None:
            self.runtime.rng = state.rng_state.to_random()
        runtime_state = state.runtime_state
        if expected_kind is PolicyRuntimeKindV2.PLANNER:
            if set(runtime_state) != {"journal", "trace", "proposer_client_posts"}:
                raise ContractError("planner runtime state has unknown or missing fields")
            if self.journal is None:
                raise ContractError("planner runtime has no V2 journal")
            self.journal.restore(runtime_state["journal"])
            trace = runtime_state["trace"]
            if not isinstance(trace, list):
                raise ContractError("planner runtime trace must be a list")
            self.runtime.trace = copy.deepcopy(trace)
            proposer = getattr(self.runtime, "proposer", None)
            if proposer is not None and hasattr(proposer, "posts_sent"):
                proposer.posts_sent = max(
                    int(runtime_state["proposer_client_posts"]), spend_attempts
                )
        elif expected_kind is PolicyRuntimeKindV2.LLM:
            if set(runtime_state) != {"client_posts"}:
                raise ContractError("LLM runtime state has unknown or missing fields")
            client = getattr(self.runtime, "client", None)
            if client is None or not hasattr(client, "posts_sent"):
                raise ContractError("LLM runtime has no restorable request counter")
            client.posts_sent = max(int(runtime_state["client_posts"]), spend_attempts)
        elif runtime_state:
            raise ContractError("scripted runtime state must be empty")
        self.services.model_posts = max(state.model_posts, spend_attempts)

    def restore_spend_attempts(self, attempts: int) -> None:
        """Restore the crash-window counter even when no state checkpoint exists."""

        self.services.model_posts = max(self.services.model_posts, attempts)
        if self.descriptor.policy_kind is PolicyKindV2.LLM:
            client = getattr(self.runtime, "client", None)
        elif self.descriptor.policy_kind is PolicyKindV2.PLANNER:
            client = getattr(self.runtime, "proposer", None)
        else:
            client = None
        if client is not None and hasattr(client, "posts_sent"):
            client.posts_sent = max(int(getattr(client, "posts_sent", 0)), attempts)

    async def aclose(self) -> None:
        close = getattr(self.runtime, "aclose", None)
        if callable(close):
            await close()


@dataclass(frozen=True)
class ReplayPolicyRuntimeV2:
    """Return exact normalized proposal bytes through the same policy seam."""

    proposal: TurnProposalV2
    descriptor: PolicyDescriptorV2

    async def propose_turn(self, context: TurnContextV2) -> TurnProposalV2:
        if context.policy != self.descriptor:
            raise ContractError("turn context policy descriptor mismatch")
        if self.proposal.policy_id != self.descriptor.descriptor_id:
            raise ContractError("replay proposal policy identity mismatch")
        if self.proposal.observation_id != context.observation.observation_id:
            raise ContractError("replay proposal observation identity mismatch")
        return self.proposal


def snapshot_policy_state_v2(
    runtime: AgentRuntimeV2,
    services: PolicyServicesV2,
    *,
    turn: int,
    proposal_id: str | None,
) -> PolicyStateV2:
    """Snapshot known runtimes; external proposal sources remain non-resumable."""

    if isinstance(runtime, LegacyPolicyRuntimeV2):
        return runtime.snapshot_state(turn=turn, proposal_id=proposal_id)
    runtime_kind = (
        PolicyRuntimeKindV2.REPLAY
        if isinstance(runtime, ReplayPolicyRuntimeV2)
        else PolicyRuntimeKindV2.EXTERNAL
    )
    telemetry: dict[str, Any] = {}
    if services.telemetry is not None:
        snapshot = services.telemetry.snapshot()
        if services.agent_id in snapshot:
            telemetry[services.agent_id] = {
                key: value
                for key, value in snapshot[services.agent_id].items()
                if key != "total_ms"
            }
    return PolicyStateV2.create(
        policy_id=runtime.descriptor.descriptor_id,
        agent_id=services.agent_id,
        player_id=services.player_id,
        turn=turn,
        proposal_id=proposal_id,
        runtime_kind=runtime_kind,
        rng_state=None,
        runtime_state={},
        operations=services.operation_delta(),
        telemetry=telemetry,
        model_posts=services.model_posts,
    )


def restore_policy_history_v2(
    runtime: AgentRuntimeV2,
    services: PolicyServicesV2,
    states: tuple[PolicyStateV2, ...],
    *,
    spend_attempts: int,
) -> None:
    """Restore one seat from ordered trust-root deltas plus durable spend."""

    for state in states:
        services.restore_operations(state.operations)
    if states:
        latest = states[-1]
        if latest.model_posts > spend_attempts:
            raise ContractError("policy state claims provider attempts absent from the ledger")
        if services.telemetry is not None:
            services.telemetry.merge_snapshot(latest.telemetry)
        if not isinstance(runtime, LegacyPolicyRuntimeV2):
            raise ContractError("external and replay policy runtimes cannot resume")
        runtime.restore_state(latest, spend_attempts=spend_attempts)
    elif isinstance(runtime, LegacyPolicyRuntimeV2):
        runtime.restore_spend_attempts(spend_attempts)
    elif spend_attempts:
        raise ContractError("external policy spend cannot resume without state")


def _qualified_type(value: Any) -> str | None:
    if value is None:
        return None
    kind = type(value)
    return f"{kind.__module__}.{kind.__qualname__}"


def _runtime_identity_v2(profile: AgentProfile, runtime: Any | None) -> dict[str, Any]:
    """Return only behavior-defining, secret-free construction controls."""

    identity: dict[str, Any] = {"runtime_class": _qualified_type(runtime)}
    rng = getattr(runtime, "rng", None)
    identity["initial_rng_state_sha256"] = (
        sha256_hex(canonical(RandomStateV2.from_random(rng).to_doc()))
        if isinstance(rng, random.Random)
        else None
    )
    if profile.policy == "planner":
        bandit = getattr(runtime, "bandit", None)
        bandit_doc = None
        if bandit is not None:
            to_doc = getattr(bandit, "to_doc", None)
            if not callable(to_doc):
                raise ContractError("planner bandit has no canonical identity document")
            bandit_doc = _policy_json_value(to_doc(), "$policy.bandit")
        weights = _policy_json_value(
            getattr(runtime, "weights", None),
            "$policy.weights",
        )
        identity["planner"] = {
            "method": getattr(runtime, "method", None),
            "budget": getattr(runtime, "budget", None),
            "weights": weights,
            "bandit_sha256": (
                sha256_hex(canonical(bandit_doc)) if bandit_doc is not None else None
            ),
        }
    return identity


def policy_descriptor_v2(
    profile: AgentProfile,
    *,
    runtime: Any | None = None,
) -> PolicyDescriptorV2:
    """Build a stable, secret-free descriptor for one configured policy seat."""

    kind = (
        PolicyKindV2.LLM
        if profile.policy == "llm"
        else PolicyKindV2.PLANNER
        if profile.policy == "planner"
        else PolicyKindV2.SCRIPTED
    )
    llm = profile.llm if profile.policy == "llm" else profile.proposer
    provider: str | None = None
    model: str | None = None
    llm_identity: dict[str, Any] | None = None
    if llm is not None:
        try:
            endpoint = urlparse(llm.base_url)
            port = endpoint.port
        except ValueError:
            raise ContractError("policy provider endpoint is malformed") from None
        host = endpoint.hostname
        provider = host or "configured-http"
        model = llm.model_id
        llm_identity = {
            "scheme": endpoint.scheme.casefold(),
            "host": endpoint.hostname,
            "port": port,
            "path": endpoint.path,
            "model_id": llm.model_id,
            "max_tokens": llm.max_tokens,
            "max_tool_rounds": llm.max_tool_rounds,
            "max_result_chars": llm.max_result_chars,
            "request_timeout_hex": float(llm.request_timeout_s).hex(),
            "max_retries": llm.max_retries,
            "max_requests_per_match": llm.max_requests_per_match,
        }
    loaded_case_base = getattr(runtime, "case_base", None)
    case_base_digest = getattr(loaded_case_base, "artifact_sha256", None)
    safe_identity = {
        "agent_id": profile.agent_id,
        "player_id": profile.player_id,
        "policy": profile.policy,
        "seed": profile.seed,
        "llm": llm_identity,
        "case_base_path": getattr(profile.case_base, "path", None),
        "case_base_sha256": case_base_digest,
        "runtime": _runtime_identity_v2(profile, runtime),
    }
    return PolicyDescriptorV2.create(
        policy_kind=kind,
        policy_version="2.0",
        artifact_digest=sha256_hex(canonical(safe_identity)),
        provider=provider,
        model=model,
        registered_action_kinds=PLAYER_ACTION_KINDS_V2,
    )


def build_policy_runtime_v2(
    profile: AgentProfile,
    *,
    services: PolicyServicesV2,
    runtime: Any | None = None,
) -> LegacyPolicyRuntimeV2:
    """Build or wrap a scripted/planner/LLM runtime at the V2 boundary."""

    legacy = runtime
    if legacy is None:
        legacy = build_runtime(
            profile,
            telemetry=services.telemetry,
            diary=services.diary,
            strategy=services.strategy,
            on_post=services.note_model_post,
        )
    # Programmatically injected mature runtimes are still subject to the V2
    # spend trust root. Rebind their provider-attempt hook before any turn can
    # issue a POST; the callback carries counts only, never request bytes.
    for client_name in ("client", "proposer"):
        client = getattr(legacy, client_name, None)
        if client is not None and hasattr(client, "on_post"):
            client.on_post = services.note_model_post
    journal = (
        InMemoryPlannerJournalV2()
        if profile.policy == "planner"
        else None
    )
    bind = getattr(legacy, "bind_services", None)
    if callable(bind):
        bind_kwargs = {"diary": services.diary, "strategy": services.strategy}
        if journal is not None:
            bind_kwargs["journal"] = journal
        with contextlib.suppress(TypeError):
            bind(**bind_kwargs)
    return LegacyPolicyRuntimeV2(
        runtime=legacy,
        descriptor=policy_descriptor_v2(profile, runtime=legacy),
        services=services,
        journal=journal,
    )


def proposal_from_doc_v2(
    doc: Mapping[str, Any], descriptor: PolicyDescriptorV2
) -> ReplayPolicyRuntimeV2:
    """Strict construction helper for replay/scripted proposal fixtures."""

    return ReplayPolicyRuntimeV2(TurnProposalV2.from_doc(doc), descriptor)
