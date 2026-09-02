"""Frozen Python mirror of the Civ Arena V2 JSON contracts.

Every public constructor either has no semantic identity or verifies the
identity supplied to it. ``create`` methods derive identities from canonical
semantic bytes; ``from_doc`` validates JSON Schema before constructing. The
models retain mappings as sorted tuples so frozen means deeply immutable, not
merely a frozen reference to a mutable ``dict``.
"""

from __future__ import annotations

import enum
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, ClassVar

from civ_arena.canonical import canonical, sha256_hex
from civ_arena.v2.schemas import ContractError, validate_doc

SCHEMA_V2 = 2
ZERO_DIGEST = "0" * 64

type CanonicalScalar = bool | int | str
type FrozenValue = CanonicalScalar | tuple[CanonicalScalar, ...]


def _semantic_id(doc: Mapping[str, Any], identity_field: str) -> str:
    body = {key: value for key, value in doc.items() if key != identity_field}
    return sha256_hex(canonical(body))


def _assert_identity(doc: Mapping[str, Any], identity_field: str) -> None:
    expected = _semantic_id(doc, identity_field)
    if doc.get(identity_field) != expected:
        raise ContractError(f"{identity_field} does not match canonical semantic bytes")


def _sorted_models(models: tuple[Any, ...]) -> bool:
    docs = [canonical(model.to_doc()) for model in models]
    return docs == sorted(docs) and len(docs) == len(set(docs))


def _freeze_value(value: Any) -> FrozenValue:
    if isinstance(value, list | tuple):
        return tuple(value)
    return value


def _value_doc(value: FrozenValue) -> CanonicalScalar | list[CanonicalScalar]:
    return list(value) if isinstance(value, tuple) else value


def freeze_parameters(
    parameters: Mapping[str, CanonicalScalar] | None,
) -> tuple[tuple[str, CanonicalScalar], ...]:
    if parameters is None:
        return ()
    return tuple(sorted(parameters.items()))


def parameters_doc(
    parameters: tuple[tuple[str, CanonicalScalar], ...],
) -> dict[str, CanonicalScalar]:
    return dict(parameters)


class KnowledgeStateV2(enum.StrEnum):
    KNOWN = "known"
    UNKNOWN = "unknown"
    NOT_OBSERVED = "not_observed"
    NOT_APPLICABLE = "not_applicable"


@dataclass(frozen=True)
class KnowledgeValueV2:
    state: KnowledgeStateV2
    value: FrozenValue | None
    reason: str | None
    observed_turn: int | None
    schema: int = SCHEMA_V2

    SCHEMA_REF: ClassVar[str] = "urn:civ-arena:common:2#/$defs/KnowledgeValueV2"

    def __post_init__(self) -> None:
        if not isinstance(self.state, KnowledgeStateV2):
            raise ContractError("knowledge state must be KnowledgeStateV2")
        validate_doc(self.SCHEMA_REF, self.to_doc())

    @classmethod
    def known(cls, value: FrozenValue, *, observed_turn: int) -> KnowledgeValueV2:
        return cls(KnowledgeStateV2.KNOWN, _freeze_value(value), None, observed_turn)

    @classmethod
    def unknown(
        cls, reason: str, *, observed_turn: int | None = None
    ) -> KnowledgeValueV2:
        return cls(KnowledgeStateV2.UNKNOWN, None, reason, observed_turn)

    @classmethod
    def not_observed(
        cls, reason: str, *, last_observed_turn: int | None = None
    ) -> KnowledgeValueV2:
        return cls(KnowledgeStateV2.NOT_OBSERVED, None, reason, last_observed_turn)

    @classmethod
    def not_applicable(cls, reason: str) -> KnowledgeValueV2:
        return cls(KnowledgeStateV2.NOT_APPLICABLE, None, reason, None)

    def to_doc(self) -> dict[str, Any]:
        doc: dict[str, Any] = {
            "schema": self.schema,
            "state": self.state.value,
            "observed_turn": self.observed_turn,
        }
        if self.state is KnowledgeStateV2.KNOWN:
            doc["value"] = _value_doc(self.value)  # type: ignore[arg-type]
        else:
            doc["reason"] = self.reason
        return doc

    @classmethod
    def from_doc(cls, doc: Mapping[str, Any]) -> KnowledgeValueV2:
        raw = dict(doc)
        validate_doc(cls.SCHEMA_REF, raw)
        state = KnowledgeStateV2(raw["state"])
        return cls(
            state=state,
            value=_freeze_value(raw["value"]) if state is KnowledgeStateV2.KNOWN else None,
            reason=raw.get("reason"),
            observed_turn=raw["observed_turn"],
            schema=raw["schema"],
        )


class EntityTypeV2(enum.StrEnum):
    PLAYER = "player"
    UNIT = "unit"
    CITY = "city"
    TILE = "tile"
    TECHNOLOGY = "technology"
    PRODUCTION_ITEM = "production_item"
    RESOURCE = "resource"
    PHASE = "phase"
    SYSTEM = "system"


class IdentityKindV2(enum.StrEnum):
    SEMANTIC = "semantic"
    EPHEMERAL = "ephemeral"


@dataclass(frozen=True)
class EntityRefV2:
    entity_type: EntityTypeV2
    entity_id: str
    identity_kind: IdentityKindV2 = IdentityKindV2.SEMANTIC
    identity_version: int = 1
    schema: int = SCHEMA_V2

    SCHEMA_REF: ClassVar[str] = "urn:civ-arena:common:2#/$defs/EntityRefV2"

    def __post_init__(self) -> None:
        if not isinstance(self.entity_type, EntityTypeV2):
            raise ContractError("entity_type must be EntityTypeV2")
        if not isinstance(self.identity_kind, IdentityKindV2):
            raise ContractError("identity_kind must be IdentityKindV2")
        validate_doc(self.SCHEMA_REF, self.to_doc())

    def to_doc(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "entity_type": self.entity_type.value,
            "entity_id": self.entity_id,
            "identity_kind": self.identity_kind.value,
            "identity_version": self.identity_version,
        }

    @classmethod
    def from_doc(cls, doc: Mapping[str, Any]) -> EntityRefV2:
        raw = dict(doc)
        validate_doc(cls.SCHEMA_REF, raw)
        return cls(
            entity_type=EntityTypeV2(raw["entity_type"]),
            entity_id=raw["entity_id"],
            identity_kind=IdentityKindV2(raw["identity_kind"]),
            identity_version=raw["identity_version"],
            schema=raw["schema"],
        )


class FactSourceV2(enum.StrEnum):
    DIRECT = "direct"
    REMEMBERED = "remembered"
    DERIVED_VISIBLE = "derived_visible"


class FactSubjectScopeV2(enum.StrEnum):
    SELF = "self"
    OWNED = "owned"
    PUBLIC = "public"
    VISIBLE_FOREIGN = "visible_foreign"
    REMEMBERED_FOREIGN = "remembered_foreign"


OBSERVABLE_PREDICATES = frozenset(
    {
        "available",
        "available_production",
        "available_research",
        "alive",
        "border_radius",
        "buildings",
        "city_id",
        "civ_name",
        "coord",
        "cost",
        "feature",
        "food_bucket",
        "fortified",
        "gold",
        "hp",
        "hp_bucket",
        "improvement",
        "kind",
        "max_movement",
        "movement",
        "name",
        "owner_id",
        "population",
        "prereq",
        "production_bucket",
        "production_queue",
        "purchase_cost",
        "ranged_strength",
        "researched",
        "researching",
        "resource",
        "science_bucket",
        "status",
        "strength",
        "terrain",
        "turns_remaining",
        "type",
        "yields",
    }
)


@dataclass(frozen=True)
class ObservableFactV2:
    subject: EntityRefV2
    subject_scope: FactSubjectScopeV2
    predicate: str
    value: KnowledgeValueV2
    source: FactSourceV2
    schema: int = SCHEMA_V2

    SCHEMA_REF: ClassVar[str] = "urn:civ-arena:common:2#/$defs/ObservableFactV2"

    def __post_init__(self) -> None:
        if self.predicate not in OBSERVABLE_PREDICATES:
            raise ContractError("predicate is not policy-observable in V2")
        if not isinstance(self.subject_scope, FactSubjectScopeV2):
            raise ContractError("fact subject scope must be FactSubjectScopeV2")
        if not isinstance(self.source, FactSourceV2):
            raise ContractError("fact source must be FactSourceV2")
        validate_doc(self.SCHEMA_REF, self.to_doc())

    def to_doc(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "subject": self.subject.to_doc(),
            "subject_scope": self.subject_scope.value,
            "predicate": self.predicate,
            "value": self.value.to_doc(),
            "source": self.source.value,
        }

    @classmethod
    def from_doc(cls, doc: Mapping[str, Any]) -> ObservableFactV2:
        raw = dict(doc)
        validate_doc(cls.SCHEMA_REF, raw)
        return cls(
            subject=EntityRefV2.from_doc(raw["subject"]),
            subject_scope=FactSubjectScopeV2(raw["subject_scope"]),
            predicate=raw["predicate"],
            value=KnowledgeValueV2.from_doc(raw["value"]),
            source=FactSourceV2(raw["source"]),
            schema=raw["schema"],
        )


class ObservationPhaseV2(enum.StrEnum):
    PRE_TURN = "pre_turn"
    TURN = "turn"
    POST_TURN = "post_turn"
    TERMINAL = "terminal"


class ActionKindV2(enum.StrEnum):
    ATTACK = "attack"
    END_TURN = "end_turn"
    FORTIFY = "fortify"
    FOUND_CITY = "found_city"
    MOVE_UNIT = "move_unit"
    PURCHASE = "purchase"
    SET_CITY_PRODUCTION = "set_city_production"
    SET_RESEARCH = "set_research"


ACTION_PARAMETER_KEYS: dict[ActionKindV2, frozenset[str]] = {
    ActionKindV2.ATTACK: frozenset({"unit_id", "target_id"}),
    ActionKindV2.END_TURN: frozenset(),
    ActionKindV2.FORTIFY: frozenset({"unit_id"}),
    ActionKindV2.FOUND_CITY: frozenset({"unit_id", "name"}),
    ActionKindV2.MOVE_UNIT: frozenset({"unit_id", "dest"}),
    ActionKindV2.PURCHASE: frozenset({"city_id", "item_id"}),
    ActionKindV2.SET_CITY_PRODUCTION: frozenset({"city_id", "item_id"}),
    ActionKindV2.SET_RESEARCH: frozenset({"tech_id"}),
}


def _validate_action_parameters(
    action_kind: ActionKindV2,
    parameters: tuple[tuple[str, CanonicalScalar], ...],
) -> None:
    if tuple(sorted(parameters)) != parameters or len(dict(parameters)) != len(parameters):
        raise ContractError("action parameters must be unique and canonically sorted")
    if frozenset(dict(parameters)) != ACTION_PARAMETER_KEYS[action_kind]:
        raise ContractError("action parameters do not match the registered action kind")


@dataclass(frozen=True)
class ObservationV2:
    observation_id: str
    environment_id: str
    game_version: str
    ruleset_digest: str
    turn: int
    active_player: EntityRefV2
    observing_player: EntityRefV2
    phase: ObservationPhaseV2
    observed_at_seq: int
    facts: tuple[ObservableFactV2, ...]
    mandatory_action_kinds: tuple[ActionKindV2, ...]
    schema: int = SCHEMA_V2

    SCHEMA_REF: ClassVar[str] = "urn:civ-arena:observation:2"

    def __post_init__(self) -> None:
        if not isinstance(self.phase, ObservationPhaseV2):
            raise ContractError("observation phase must be ObservationPhaseV2")
        if any(not isinstance(item, ActionKindV2) for item in self.mandatory_action_kinds):
            raise ContractError("mandatory action kinds must be registered ActionKindV2 values")
        if not _sorted_models(self.facts):
            raise ContractError("observation facts must be unique and canonically sorted")
        if tuple(sorted(self.mandatory_action_kinds, key=str)) != self.mandatory_action_kinds:
            raise ContractError("mandatory action kinds must be unique and sorted")
        if len(set(self.mandatory_action_kinds)) != len(self.mandatory_action_kinds):
            raise ContractError("mandatory action kinds must be unique and sorted")
        for fact in self.facts:
            if (
                fact.subject_scope is FactSubjectScopeV2.SELF
                and fact.subject != self.observing_player
            ):
                raise ContractError("self-scoped fact does not describe observing player")
            if fact.subject_scope in {
                FactSubjectScopeV2.VISIBLE_FOREIGN,
                FactSubjectScopeV2.REMEMBERED_FOREIGN,
            } and fact.subject == self.observing_player:
                raise ContractError("foreign-scoped fact describes observing player")
        doc = self.to_doc()
        validate_doc(self.SCHEMA_REF, doc)
        _assert_identity(doc, "observation_id")

    @classmethod
    def create(
        cls,
        *,
        environment_id: str,
        game_version: str,
        ruleset_digest: str,
        turn: int,
        active_player: EntityRefV2,
        observing_player: EntityRefV2,
        phase: ObservationPhaseV2,
        observed_at_seq: int,
        facts: tuple[ObservableFactV2, ...] | list[ObservableFactV2],
        mandatory_action_kinds: tuple[ActionKindV2, ...] | list[ActionKindV2] = (),
    ) -> ObservationV2:
        sorted_facts = tuple(sorted(facts, key=lambda item: canonical(item.to_doc())))
        mandatory = tuple(sorted(set(mandatory_action_kinds), key=str))
        stub = cls.__new__(cls)
        values = {
            "observation_id": ZERO_DIGEST,
            "environment_id": environment_id,
            "game_version": game_version,
            "ruleset_digest": ruleset_digest,
            "turn": turn,
            "active_player": active_player,
            "observing_player": observing_player,
            "phase": phase,
            "observed_at_seq": observed_at_seq,
            "facts": sorted_facts,
            "mandatory_action_kinds": mandatory,
            "schema": SCHEMA_V2,
        }
        for key, value in values.items():
            object.__setattr__(stub, key, value)
        doc = stub.to_doc()
        values["observation_id"] = _semantic_id(doc, "observation_id")
        return cls(**values)

    def to_doc(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "observation_id": self.observation_id,
            "environment_id": self.environment_id,
            "game_version": self.game_version,
            "ruleset_digest": self.ruleset_digest,
            "turn": self.turn,
            "active_player": self.active_player.to_doc(),
            "observing_player": self.observing_player.to_doc(),
            "phase": self.phase.value,
            "observed_at_seq": self.observed_at_seq,
            "facts": [fact.to_doc() for fact in self.facts],
            "mandatory_action_kinds": [kind.value for kind in self.mandatory_action_kinds],
        }

    @classmethod
    def from_doc(cls, doc: Mapping[str, Any]) -> ObservationV2:
        raw = dict(doc)
        validate_doc(cls.SCHEMA_REF, raw)
        return cls(
            observation_id=raw["observation_id"],
            environment_id=raw["environment_id"],
            game_version=raw["game_version"],
            ruleset_digest=raw["ruleset_digest"],
            turn=raw["turn"],
            active_player=EntityRefV2.from_doc(raw["active_player"]),
            observing_player=EntityRefV2.from_doc(raw["observing_player"]),
            phase=ObservationPhaseV2(raw["phase"]),
            observed_at_seq=raw["observed_at_seq"],
            facts=tuple(ObservableFactV2.from_doc(item) for item in raw["facts"]),
            mandatory_action_kinds=tuple(
                ActionKindV2(item) for item in raw["mandatory_action_kinds"]
            ),
            schema=raw["schema"],
        )


class PreconditionOperatorV2(enum.StrEnum):
    EQ = "eq"
    NE = "ne"
    GTE = "gte"
    LTE = "lte"
    PRESENT = "present"
    ABSENT = "absent"
    CONTAINS = "contains"


@dataclass(frozen=True)
class PreconditionClaimV2:
    subject: EntityRefV2
    predicate: str
    operator: PreconditionOperatorV2
    expected: KnowledgeValueV2
    schema: int = SCHEMA_V2

    SCHEMA_REF: ClassVar[str] = "urn:civ-arena:common:2#/$defs/PreconditionClaimV2"

    def __post_init__(self) -> None:
        if not isinstance(self.operator, PreconditionOperatorV2):
            raise ContractError("precondition operator must be PreconditionOperatorV2")
        validate_doc(self.SCHEMA_REF, self.to_doc())

    def to_doc(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "subject": self.subject.to_doc(),
            "predicate": self.predicate,
            "operator": self.operator.value,
            "expected": self.expected.to_doc(),
        }

    @classmethod
    def from_doc(cls, doc: Mapping[str, Any]) -> PreconditionClaimV2:
        raw = dict(doc)
        validate_doc(cls.SCHEMA_REF, raw)
        return cls(
            subject=EntityRefV2.from_doc(raw["subject"]),
            predicate=raw["predicate"],
            operator=PreconditionOperatorV2(raw["operator"]),
            expected=KnowledgeValueV2.from_doc(raw["expected"]),
            schema=raw["schema"],
        )


class ResourceKindV2(enum.StrEnum):
    GOLD = "gold"
    MOVEMENT = "movement"
    PRODUCTION_SLOT = "production_slot"
    RESEARCH_SLOT = "research_slot"
    UNIT = "unit"
    TURN = "turn"


class ResourceModeV2(enum.StrEnum):
    CONSUME = "consume"
    RESERVE = "reserve"
    READ = "read"


@dataclass(frozen=True)
class ResourceClaimV2:
    resource: ResourceKindV2
    owner: EntityRefV2
    amount: int
    mode: ResourceModeV2
    region: str
    schema: int = SCHEMA_V2

    SCHEMA_REF: ClassVar[str] = "urn:civ-arena:common:2#/$defs/ResourceClaimV2"

    def __post_init__(self) -> None:
        if not isinstance(self.resource, ResourceKindV2):
            raise ContractError("resource must be ResourceKindV2")
        if not isinstance(self.mode, ResourceModeV2):
            raise ContractError("resource mode must be ResourceModeV2")
        validate_doc(self.SCHEMA_REF, self.to_doc())

    def to_doc(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "resource": self.resource.value,
            "owner": self.owner.to_doc(),
            "amount": self.amount,
            "mode": self.mode.value,
            "region": self.region,
        }

    @classmethod
    def from_doc(cls, doc: Mapping[str, Any]) -> ResourceClaimV2:
        raw = dict(doc)
        validate_doc(cls.SCHEMA_REF, raw)
        return cls(
            resource=ResourceKindV2(raw["resource"]),
            owner=EntityRefV2.from_doc(raw["owner"]),
            amount=raw["amount"],
            mode=ResourceModeV2(raw["mode"]),
            region=raw["region"],
            schema=raw["schema"],
        )


class EffectKindV2(enum.StrEnum):
    CREATE = "create"
    DAMAGE = "damage"
    DECREMENT = "decrement"
    DELETE = "delete"
    INCREMENT = "increment"
    MOVE = "move"
    NO_CHANGE = "no_change"
    SET = "set"
    TERMINAL = "terminal"


@dataclass(frozen=True)
class EffectClaimV2:
    effect_kind: EffectKindV2
    subject: EntityRefV2
    attribute: str
    expected: KnowledgeValueV2
    region: str
    schema: int = SCHEMA_V2

    SCHEMA_REF: ClassVar[str] = "urn:civ-arena:common:2#/$defs/EffectClaimV2"

    def __post_init__(self) -> None:
        if not isinstance(self.effect_kind, EffectKindV2):
            raise ContractError("effect kind must be EffectKindV2")
        validate_doc(self.SCHEMA_REF, self.to_doc())

    def to_doc(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "effect_kind": self.effect_kind.value,
            "subject": self.subject.to_doc(),
            "attribute": self.attribute,
            "expected": self.expected.to_doc(),
            "region": self.region,
        }

    @classmethod
    def from_doc(cls, doc: Mapping[str, Any]) -> EffectClaimV2:
        raw = dict(doc)
        validate_doc(cls.SCHEMA_REF, raw)
        return cls(
            effect_kind=EffectKindV2(raw["effect_kind"]),
            subject=EntityRefV2.from_doc(raw["subject"]),
            attribute=raw["attribute"],
            expected=KnowledgeValueV2.from_doc(raw["expected"]),
            region=raw["region"],
            schema=raw["schema"],
        )


@dataclass(frozen=True)
class ActionIntentV2:
    intent_id: str
    action_kind: ActionKindV2
    actor: EntityRefV2
    target: EntityRefV2 | None
    parameters: tuple[tuple[str, CanonicalScalar], ...]
    proposal_index: int
    schema: int = SCHEMA_V2

    SCHEMA_REF: ClassVar[str] = "urn:civ-arena:action:2#/$defs/ActionIntentV2"

    def __post_init__(self) -> None:
        if not isinstance(self.action_kind, ActionKindV2):
            raise ContractError("action_kind must be ActionKindV2")
        _validate_action_parameters(self.action_kind, self.parameters)
        doc = self.to_doc()
        validate_doc(self.SCHEMA_REF, doc)
        _assert_identity(doc, "intent_id")

    @classmethod
    def create(
        cls,
        action_kind: ActionKindV2,
        actor: EntityRefV2,
        *,
        target: EntityRefV2 | None = None,
        parameters: Mapping[str, CanonicalScalar] | None = None,
        proposal_index: int,
    ) -> ActionIntentV2:
        frozen = freeze_parameters(parameters)
        body = {
            "schema": SCHEMA_V2,
            "intent_id": ZERO_DIGEST,
            "action_kind": action_kind.value,
            "actor": actor.to_doc(),
            "target": target.to_doc() if target is not None else None,
            "parameters": parameters_doc(frozen),
            "proposal_index": proposal_index,
        }
        return cls(
            intent_id=_semantic_id(body, "intent_id"),
            action_kind=action_kind,
            actor=actor,
            target=target,
            parameters=frozen,
            proposal_index=proposal_index,
        )

    def to_doc(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "intent_id": self.intent_id,
            "action_kind": self.action_kind.value,
            "actor": self.actor.to_doc(),
            "target": self.target.to_doc() if self.target is not None else None,
            "parameters": parameters_doc(self.parameters),
            "proposal_index": self.proposal_index,
        }

    @classmethod
    def from_doc(cls, doc: Mapping[str, Any]) -> ActionIntentV2:
        raw = dict(doc)
        validate_doc(cls.SCHEMA_REF, raw)
        return cls(
            intent_id=raw["intent_id"],
            action_kind=ActionKindV2(raw["action_kind"]),
            actor=EntityRefV2.from_doc(raw["actor"]),
            target=EntityRefV2.from_doc(raw["target"]) if raw["target"] else None,
            parameters=freeze_parameters(raw["parameters"]),
            proposal_index=raw["proposal_index"],
            schema=raw["schema"],
        )


@dataclass(frozen=True)
class LegalActionV2:
    action_id: str
    action_kind: ActionKindV2
    actor: EntityRefV2
    target: EntityRefV2 | None
    parameters: tuple[tuple[str, CanonicalScalar], ...]
    preconditions: tuple[PreconditionClaimV2, ...]
    resource_claims: tuple[ResourceClaimV2, ...]
    expected_effects: tuple[EffectClaimV2, ...]
    affected_regions: tuple[str, ...]
    observation_id: str
    mandatory: bool
    terminal: bool
    safe_to_retry: bool
    schema: int = SCHEMA_V2

    SCHEMA_REF: ClassVar[str] = "urn:civ-arena:action:2#/$defs/LegalActionV2"

    def __post_init__(self) -> None:
        if not isinstance(self.action_kind, ActionKindV2):
            raise ContractError("action_kind must be ActionKindV2")
        _validate_action_parameters(self.action_kind, self.parameters)
        for values, label in (
            (self.preconditions, "preconditions"),
            (self.resource_claims, "resource claims"),
            (self.expected_effects, "expected effects"),
        ):
            if not _sorted_models(values):
                raise ContractError(f"{label} must be unique and canonically sorted")
        if tuple(sorted(set(self.affected_regions))) != self.affected_regions:
            raise ContractError("affected regions must be unique and sorted")
        if self.action_kind is ActionKindV2.END_TURN and not self.terminal:
            raise ContractError("end_turn must be terminal")
        if self.action_kind is not ActionKindV2.END_TURN and self.terminal:
            raise ContractError("only end_turn may be terminal")
        doc = self.to_doc()
        validate_doc(self.SCHEMA_REF, doc)
        _assert_identity(doc, "action_id")

    @classmethod
    def create(
        cls,
        action_kind: ActionKindV2,
        actor: EntityRefV2,
        observation_id: str,
        *,
        target: EntityRefV2 | None = None,
        parameters: Mapping[str, CanonicalScalar] | None = None,
        preconditions: tuple[PreconditionClaimV2, ...] | list[PreconditionClaimV2] = (),
        resource_claims: tuple[ResourceClaimV2, ...] | list[ResourceClaimV2] = (),
        expected_effects: tuple[EffectClaimV2, ...] | list[EffectClaimV2] = (),
        affected_regions: tuple[str, ...] | list[str] = (),
        mandatory: bool = False,
        terminal: bool = False,
        safe_to_retry: bool = False,
    ) -> LegalActionV2:
        frozen = freeze_parameters(parameters)
        pres = tuple(sorted(preconditions, key=lambda item: canonical(item.to_doc())))
        resources = tuple(sorted(resource_claims, key=lambda item: canonical(item.to_doc())))
        effects = tuple(sorted(expected_effects, key=lambda item: canonical(item.to_doc())))
        regions = tuple(sorted(set(affected_regions)))
        body = {
            "schema": SCHEMA_V2,
            "action_id": ZERO_DIGEST,
            "action_kind": action_kind.value,
            "actor": actor.to_doc(),
            "target": target.to_doc() if target is not None else None,
            "parameters": parameters_doc(frozen),
            "preconditions": [item.to_doc() for item in pres],
            "resource_claims": [item.to_doc() for item in resources],
            "expected_effects": [item.to_doc() for item in effects],
            "affected_regions": list(regions),
            "observation_id": observation_id,
            "mandatory": mandatory,
            "terminal": terminal,
            "safe_to_retry": safe_to_retry,
        }
        return cls(
            action_id=_semantic_id(body, "action_id"),
            action_kind=action_kind,
            actor=actor,
            target=target,
            parameters=frozen,
            preconditions=pres,
            resource_claims=resources,
            expected_effects=effects,
            affected_regions=regions,
            observation_id=observation_id,
            mandatory=mandatory,
            terminal=terminal,
            safe_to_retry=safe_to_retry,
        )

    def to_doc(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "action_id": self.action_id,
            "action_kind": self.action_kind.value,
            "actor": self.actor.to_doc(),
            "target": self.target.to_doc() if self.target is not None else None,
            "parameters": parameters_doc(self.parameters),
            "preconditions": [item.to_doc() for item in self.preconditions],
            "resource_claims": [item.to_doc() for item in self.resource_claims],
            "expected_effects": [item.to_doc() for item in self.expected_effects],
            "affected_regions": list(self.affected_regions),
            "observation_id": self.observation_id,
            "mandatory": self.mandatory,
            "terminal": self.terminal,
            "safe_to_retry": self.safe_to_retry,
        }

    @classmethod
    def from_doc(cls, doc: Mapping[str, Any]) -> LegalActionV2:
        raw = dict(doc)
        validate_doc(cls.SCHEMA_REF, raw)
        return cls(
            action_id=raw["action_id"],
            action_kind=ActionKindV2(raw["action_kind"]),
            actor=EntityRefV2.from_doc(raw["actor"]),
            target=EntityRefV2.from_doc(raw["target"]) if raw["target"] else None,
            parameters=freeze_parameters(raw["parameters"]),
            preconditions=tuple(
                PreconditionClaimV2.from_doc(item) for item in raw["preconditions"]
            ),
            resource_claims=tuple(
                ResourceClaimV2.from_doc(item) for item in raw["resource_claims"]
            ),
            expected_effects=tuple(
                EffectClaimV2.from_doc(item) for item in raw["expected_effects"]
            ),
            affected_regions=tuple(raw["affected_regions"]),
            observation_id=raw["observation_id"],
            mandatory=raw["mandatory"],
            terminal=raw["terminal"],
            safe_to_retry=raw["safe_to_retry"],
            schema=raw["schema"],
        )


@dataclass(frozen=True)
class LegalActionSetV2:
    legal_action_set_id: str
    observation_id: str
    actions: tuple[LegalActionV2, ...]
    schema: int = SCHEMA_V2

    SCHEMA_REF: ClassVar[str] = "urn:civ-arena:action:2#/$defs/LegalActionSetV2"

    def __post_init__(self) -> None:
        if tuple(sorted(self.actions, key=lambda item: item.action_id)) != self.actions:
            raise ContractError("legal actions must be unique and sorted by stable action_id")
        if len({item.action_id for item in self.actions}) != len(self.actions):
            raise ContractError("legal action ids must be unique")
        if any(item.observation_id != self.observation_id for item in self.actions):
            raise ContractError("every legal action must bind to the set observation")
        doc = self.to_doc()
        validate_doc(self.SCHEMA_REF, doc)
        _assert_identity(doc, "legal_action_set_id")

    @classmethod
    def create(
        cls,
        observation_id: str,
        actions: tuple[LegalActionV2, ...] | list[LegalActionV2],
    ) -> LegalActionSetV2:
        ordered = tuple(sorted(actions, key=lambda item: item.action_id))
        body = {
            "schema": SCHEMA_V2,
            "legal_action_set_id": ZERO_DIGEST,
            "observation_id": observation_id,
            "actions": [item.to_doc() for item in ordered],
        }
        return cls(_semantic_id(body, "legal_action_set_id"), observation_id, ordered)

    def to_doc(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "legal_action_set_id": self.legal_action_set_id,
            "observation_id": self.observation_id,
            "actions": [item.to_doc() for item in self.actions],
        }

    @classmethod
    def from_doc(cls, doc: Mapping[str, Any]) -> LegalActionSetV2:
        raw = dict(doc)
        validate_doc(cls.SCHEMA_REF, raw)
        return cls(
            legal_action_set_id=raw["legal_action_set_id"],
            observation_id=raw["observation_id"],
            actions=tuple(LegalActionV2.from_doc(item) for item in raw["actions"]),
            schema=raw["schema"],
        )


class AuthorizationDecisionV2(enum.StrEnum):
    AUTHORIZED = "authorized"
    REFUSED = "refused"


class AuthorizationReasonV2(enum.StrEnum):
    AUTHORIZED = "authorized"
    ACTION_ABSENT = "action_absent"
    AMBIGUOUS_INTENT = "ambiguous_intent"
    CONFLICT = "conflict"
    MANDATORY_UNRESOLVED = "mandatory_unresolved"
    STALE_GRAPH = "stale_graph"
    STALE_OBSERVATION = "stale_observation"
    UNREGISTERED_ACTION = "unregistered_action"


@dataclass(frozen=True)
class AuthorizationV2:
    authorization_id: str
    decision: AuthorizationDecisionV2
    intent_id: str
    action_id: str | None
    observation_id: str
    graph_id: str
    reason_code: AuthorizationReasonV2
    schema: int = SCHEMA_V2

    SCHEMA_REF: ClassVar[str] = "urn:civ-arena:action:2#/$defs/AuthorizationV2"

    def __post_init__(self) -> None:
        if not isinstance(self.decision, AuthorizationDecisionV2):
            raise ContractError("authorization decision must be AuthorizationDecisionV2")
        if not isinstance(self.reason_code, AuthorizationReasonV2):
            raise ContractError("authorization reason must be AuthorizationReasonV2")
        if self.decision is AuthorizationDecisionV2.AUTHORIZED:
            if self.action_id is None or self.reason_code is not AuthorizationReasonV2.AUTHORIZED:
                raise ContractError("authorized decisions require an action and authorized reason")
        elif self.reason_code is AuthorizationReasonV2.AUTHORIZED:
            raise ContractError("refused decisions cannot carry the authorized reason")
        doc = self.to_doc()
        validate_doc(self.SCHEMA_REF, doc)
        _assert_identity(doc, "authorization_id")

    @classmethod
    def create(
        cls,
        *,
        decision: AuthorizationDecisionV2,
        intent_id: str,
        action_id: str | None,
        observation_id: str,
        graph_id: str,
        reason_code: AuthorizationReasonV2,
    ) -> AuthorizationV2:
        body = {
            "schema": SCHEMA_V2,
            "authorization_id": ZERO_DIGEST,
            "decision": decision.value,
            "intent_id": intent_id,
            "action_id": action_id,
            "observation_id": observation_id,
            "graph_id": graph_id,
            "reason_code": reason_code.value,
        }
        return cls(
            authorization_id=_semantic_id(body, "authorization_id"),
            decision=decision,
            intent_id=intent_id,
            action_id=action_id,
            observation_id=observation_id,
            graph_id=graph_id,
            reason_code=reason_code,
        )

    def to_doc(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "authorization_id": self.authorization_id,
            "decision": self.decision.value,
            "intent_id": self.intent_id,
            "action_id": self.action_id,
            "observation_id": self.observation_id,
            "graph_id": self.graph_id,
            "reason_code": self.reason_code.value,
        }

    @classmethod
    def from_doc(cls, doc: Mapping[str, Any]) -> AuthorizationV2:
        raw = dict(doc)
        validate_doc(cls.SCHEMA_REF, raw)
        return cls(
            authorization_id=raw["authorization_id"],
            decision=AuthorizationDecisionV2(raw["decision"]),
            intent_id=raw["intent_id"],
            action_id=raw["action_id"],
            observation_id=raw["observation_id"],
            graph_id=raw["graph_id"],
            reason_code=AuthorizationReasonV2(raw["reason_code"]),
            schema=raw["schema"],
        )


class ActionStatusV2(enum.StrEnum):
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    DUPLICATE = "duplicate"
    DIVERGED = "diverged"


class VerificationStatusV2(enum.StrEnum):
    MATCHED = "matched"
    MISMATCHED = "mismatched"
    NOT_EXECUTED = "not_executed"
    NOT_VERIFIABLE = "not_verifiable"


class RejectionCodeV2(enum.StrEnum):
    ADAPTER_REJECTED = "adapter_rejected"
    AUTHORIZATION_REFUSED = "authorization_refused"
    POSTCONDITION_DIVERGED = "postcondition_diverged"
    STALE_AUTHORIZATION = "stale_authorization"
    UNSAFE_RETRY_REFUSED = "unsafe_retry_refused"


@dataclass(frozen=True)
class ActionResultV2:
    result_id: str
    status: ActionStatusV2
    action_id: str
    authorization_id: str
    pre_observation_id: str
    post_observation_id: str | None
    verification: VerificationStatusV2
    observable_effects: tuple[EffectClaimV2, ...]
    rejection_code: RejectionCodeV2 | None
    safe_message: str | None
    schema: int = SCHEMA_V2

    SCHEMA_REF: ClassVar[str] = "urn:civ-arena:action:2#/$defs/ActionResultV2"

    def __post_init__(self) -> None:
        if not isinstance(self.status, ActionStatusV2):
            raise ContractError("action result status must be ActionStatusV2")
        if not isinstance(self.verification, VerificationStatusV2):
            raise ContractError("verification must be VerificationStatusV2")
        if self.rejection_code is not None and not isinstance(
            self.rejection_code, RejectionCodeV2
        ):
            raise ContractError("rejection code must be RejectionCodeV2")
        if not _sorted_models(self.observable_effects):
            raise ContractError("observable effects must be unique and canonically sorted")
        if self.status in {ActionStatusV2.ACCEPTED, ActionStatusV2.DUPLICATE}:
            if self.post_observation_id is None or self.rejection_code is not None:
                raise ContractError("successful action result requires a post observation")
        elif self.rejection_code is None:
            raise ContractError("unsuccessful action result requires a rejection code")
        doc = self.to_doc()
        validate_doc(self.SCHEMA_REF, doc)
        _assert_identity(doc, "result_id")

    @classmethod
    def create(
        cls,
        *,
        status: ActionStatusV2,
        action_id: str,
        authorization_id: str,
        pre_observation_id: str,
        post_observation_id: str | None,
        verification: VerificationStatusV2,
        observable_effects: tuple[EffectClaimV2, ...] | list[EffectClaimV2] = (),
        rejection_code: RejectionCodeV2 | None = None,
        safe_message: str | None = None,
    ) -> ActionResultV2:
        effects = tuple(sorted(observable_effects, key=lambda item: canonical(item.to_doc())))
        body = {
            "schema": SCHEMA_V2,
            "result_id": ZERO_DIGEST,
            "status": status.value,
            "action_id": action_id,
            "authorization_id": authorization_id,
            "pre_observation_id": pre_observation_id,
            "post_observation_id": post_observation_id,
            "verification": verification.value,
            "observable_effects": [item.to_doc() for item in effects],
            "rejection_code": rejection_code.value if rejection_code is not None else None,
            "safe_message": safe_message,
        }
        return cls(
            result_id=_semantic_id(body, "result_id"),
            status=status,
            action_id=action_id,
            authorization_id=authorization_id,
            pre_observation_id=pre_observation_id,
            post_observation_id=post_observation_id,
            verification=verification,
            observable_effects=effects,
            rejection_code=rejection_code,
            safe_message=safe_message,
        )

    def to_doc(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "result_id": self.result_id,
            "status": self.status.value,
            "action_id": self.action_id,
            "authorization_id": self.authorization_id,
            "pre_observation_id": self.pre_observation_id,
            "post_observation_id": self.post_observation_id,
            "verification": self.verification.value,
            "observable_effects": [item.to_doc() for item in self.observable_effects],
            "rejection_code": (
                self.rejection_code.value if self.rejection_code is not None else None
            ),
            "safe_message": self.safe_message,
        }

    @classmethod
    def from_doc(cls, doc: Mapping[str, Any]) -> ActionResultV2:
        raw = dict(doc)
        validate_doc(cls.SCHEMA_REF, raw)
        return cls(
            result_id=raw["result_id"],
            status=ActionStatusV2(raw["status"]),
            action_id=raw["action_id"],
            authorization_id=raw["authorization_id"],
            pre_observation_id=raw["pre_observation_id"],
            post_observation_id=raw["post_observation_id"],
            verification=VerificationStatusV2(raw["verification"]),
            observable_effects=tuple(
                EffectClaimV2.from_doc(item) for item in raw["observable_effects"]
            ),
            rejection_code=(
                RejectionCodeV2(raw["rejection_code"])
                if raw["rejection_code"] is not None
                else None
            ),
            safe_message=raw["safe_message"],
            schema=raw["schema"],
        )


@dataclass(frozen=True)
class TurnProposalV2:
    proposal_id: str
    policy_id: str
    observation_id: str
    intents: tuple[ActionIntentV2, ...]
    schema: int = SCHEMA_V2

    SCHEMA_REF: ClassVar[str] = "urn:civ-arena:action:2#/$defs/TurnProposalV2"

    def __post_init__(self) -> None:
        if tuple(item.proposal_index for item in self.intents) != tuple(range(len(self.intents))):
            raise ContractError("proposal intent indices must be contiguous author order")
        if len({item.intent_id for item in self.intents}) != len(self.intents):
            raise ContractError("proposal intent ids must be unique")
        doc = self.to_doc()
        validate_doc(self.SCHEMA_REF, doc)
        _assert_identity(doc, "proposal_id")

    @classmethod
    def create(
        cls,
        *,
        policy_id: str,
        observation_id: str,
        intents: tuple[ActionIntentV2, ...] | list[ActionIntentV2],
    ) -> TurnProposalV2:
        frozen = tuple(intents)
        body = {
            "schema": SCHEMA_V2,
            "proposal_id": ZERO_DIGEST,
            "policy_id": policy_id,
            "observation_id": observation_id,
            "intents": [item.to_doc() for item in frozen],
        }
        return cls(_semantic_id(body, "proposal_id"), policy_id, observation_id, frozen)

    def to_doc(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "proposal_id": self.proposal_id,
            "policy_id": self.policy_id,
            "observation_id": self.observation_id,
            "intents": [item.to_doc() for item in self.intents],
        }

    @classmethod
    def from_doc(cls, doc: Mapping[str, Any]) -> TurnProposalV2:
        raw = dict(doc)
        validate_doc(cls.SCHEMA_REF, raw)
        return cls(
            proposal_id=raw["proposal_id"],
            policy_id=raw["policy_id"],
            observation_id=raw["observation_id"],
            intents=tuple(ActionIntentV2.from_doc(item) for item in raw["intents"]),
            schema=raw["schema"],
        )


class EdgeKindV2(enum.StrEnum):
    MUST_PRECEDE = "MUST_PRECEDE"
    REQUIRES = "REQUIRES"
    ENABLES = "ENABLES"
    CONFLICTS_WITH = "CONFLICTS_WITH"
    MUTEX = "MUTEX"
    CONSUMES_SHARED_RESOURCE = "CONSUMES_SHARED_RESOURCE"
    INVALIDATES = "INVALIDATES"
    COMMUTES_WITH = "COMMUTES_WITH"


class EdgeAuthorityV2(enum.StrEnum):
    AUTHORITATIVE = "authoritative"
    DERIVED = "derived"


class EdgeReasonV2(enum.StrEnum):
    EFFECT_ENABLES = "effect_enables"
    EXCLUSIVE_CHOICE = "exclusive_choice"
    INSUFFICIENT_SHARED_RESOURCE = "insufficient_shared_resource"
    MUTATION_INVALIDATES = "mutation_invalidates"
    OVERLAPPING_EFFECT = "overlapping_effect"
    PRECONDITION_DEPENDENCY = "precondition_dependency"
    PROVEN_DISJOINT = "proven_disjoint"
    SAME_ACTOR_SEQUENCE = "same_actor_sequence"
    UNKNOWN_RELATIONSHIP = "unknown_relationship"


SYMMETRIC_EDGE_KINDS = frozenset(
    {
        EdgeKindV2.CONFLICTS_WITH,
        EdgeKindV2.MUTEX,
        EdgeKindV2.CONSUMES_SHARED_RESOURCE,
        EdgeKindV2.COMMUTES_WITH,
    }
)
ORDERING_EDGE_KINDS = frozenset({EdgeKindV2.MUST_PRECEDE, EdgeKindV2.REQUIRES})


@dataclass(frozen=True)
class ActionGraphNodeV2:
    node_id: str
    action: LegalActionV2
    schema: int = SCHEMA_V2

    SCHEMA_REF: ClassVar[str] = "urn:civ-arena:graph:2#/$defs/ActionGraphNodeV2"

    def __post_init__(self) -> None:
        if self.node_id != self.action.action_id:
            raise ContractError("graph node identity must equal its stable action identity")
        validate_doc(self.SCHEMA_REF, self.to_doc())

    @classmethod
    def from_action(cls, action: LegalActionV2) -> ActionGraphNodeV2:
        return cls(node_id=action.action_id, action=action)

    def to_doc(self) -> dict[str, Any]:
        return {"schema": self.schema, "node_id": self.node_id, "action": self.action.to_doc()}

    @classmethod
    def from_doc(cls, doc: Mapping[str, Any]) -> ActionGraphNodeV2:
        raw = dict(doc)
        validate_doc(cls.SCHEMA_REF, raw)
        return cls(
            node_id=raw["node_id"],
            action=LegalActionV2.from_doc(raw["action"]),
            schema=raw["schema"],
        )


@dataclass(frozen=True)
class ActionGraphEdgeV2:
    edge_id: str
    source_action_id: str
    target_action_id: str
    kind: EdgeKindV2
    authority: EdgeAuthorityV2
    reason_code: EdgeReasonV2
    region: str | None
    schema: int = SCHEMA_V2

    SCHEMA_REF: ClassVar[str] = "urn:civ-arena:graph:2#/$defs/ActionGraphEdgeV2"

    def __post_init__(self) -> None:
        if not isinstance(self.kind, EdgeKindV2):
            raise ContractError("graph edge kind must be EdgeKindV2")
        if not isinstance(self.authority, EdgeAuthorityV2):
            raise ContractError("graph edge authority must be EdgeAuthorityV2")
        if not isinstance(self.reason_code, EdgeReasonV2):
            raise ContractError("graph edge reason must be EdgeReasonV2")
        if self.source_action_id == self.target_action_id:
            raise ContractError("self edges are forbidden")
        if self.kind in SYMMETRIC_EDGE_KINDS and not (
            self.source_action_id < self.target_action_id
        ):
            raise ContractError("symmetric graph edges require canonical endpoint order")
        if self.kind is EdgeKindV2.COMMUTES_WITH:
            if self.authority is not EdgeAuthorityV2.DERIVED:
                raise ContractError("COMMUTES_WITH is always derived, never authoritative")
            if self.reason_code is not EdgeReasonV2.PROVEN_DISJOINT:
                raise ContractError("COMMUTES_WITH requires a proven_disjoint witness")
        doc = self.to_doc()
        validate_doc(self.SCHEMA_REF, doc)
        _assert_identity(doc, "edge_id")

    @classmethod
    def create(
        cls,
        source_action_id: str,
        target_action_id: str,
        kind: EdgeKindV2,
        authority: EdgeAuthorityV2,
        reason_code: EdgeReasonV2,
        *,
        region: str | None = None,
    ) -> ActionGraphEdgeV2:
        if kind in SYMMETRIC_EDGE_KINDS and target_action_id < source_action_id:
            source_action_id, target_action_id = target_action_id, source_action_id
        body = {
            "schema": SCHEMA_V2,
            "edge_id": ZERO_DIGEST,
            "source_action_id": source_action_id,
            "target_action_id": target_action_id,
            "kind": kind.value,
            "authority": authority.value,
            "reason_code": reason_code.value,
            "region": region,
        }
        return cls(
            edge_id=_semantic_id(body, "edge_id"),
            source_action_id=source_action_id,
            target_action_id=target_action_id,
            kind=kind,
            authority=authority,
            reason_code=reason_code,
            region=region,
        )

    def to_doc(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "edge_id": self.edge_id,
            "source_action_id": self.source_action_id,
            "target_action_id": self.target_action_id,
            "kind": self.kind.value,
            "authority": self.authority.value,
            "reason_code": self.reason_code.value,
            "region": self.region,
        }

    @classmethod
    def from_doc(cls, doc: Mapping[str, Any]) -> ActionGraphEdgeV2:
        raw = dict(doc)
        validate_doc(cls.SCHEMA_REF, raw)
        return cls(
            edge_id=raw["edge_id"],
            source_action_id=raw["source_action_id"],
            target_action_id=raw["target_action_id"],
            kind=EdgeKindV2(raw["kind"]),
            authority=EdgeAuthorityV2(raw["authority"]),
            reason_code=EdgeReasonV2(raw["reason_code"]),
            region=raw["region"],
            schema=raw["schema"],
        )


EDGE_KINDS_IN_ORDER = tuple(EdgeKindV2)


@dataclass(frozen=True)
class ActionGraphMetricsV2:
    legal_action_count: int
    edge_count_by_kind: tuple[tuple[EdgeKindV2, int], ...]
    independent_action_groups: int
    raw_permutation_estimate: int
    canonical_plan_count: int
    reduction_ratio_fixed: int
    compile_duration_ms: int
    revalidation_regions: tuple[str, ...]
    rejected_reasons: tuple[str, ...]

    SCHEMA_REF: ClassVar[str] = "urn:civ-arena:graph:2#/$defs/ActionGraphMetricsV2"

    def __post_init__(self) -> None:
        if tuple(kind for kind, _ in self.edge_count_by_kind) != EDGE_KINDS_IN_ORDER:
            raise ContractError("edge metrics must contain every edge kind in normative order")
        if tuple(sorted(set(self.revalidation_regions))) != self.revalidation_regions:
            raise ContractError("revalidation regions must be unique and sorted")
        validate_doc(self.SCHEMA_REF, self.to_doc())

    @classmethod
    def empty(cls, legal_action_count: int) -> ActionGraphMetricsV2:
        return cls(
            legal_action_count=legal_action_count,
            edge_count_by_kind=tuple((kind, 0) for kind in EDGE_KINDS_IN_ORDER),
            independent_action_groups=legal_action_count,
            raw_permutation_estimate=1,
            canonical_plan_count=1 if legal_action_count else 0,
            reduction_ratio_fixed=10_000,
            compile_duration_ms=0,
            revalidation_regions=(),
            rejected_reasons=(),
        )

    def to_doc(self) -> dict[str, Any]:
        return {
            "legal_action_count": self.legal_action_count,
            "edge_count_by_kind": {kind.value: count for kind, count in self.edge_count_by_kind},
            "independent_action_groups": self.independent_action_groups,
            "raw_permutation_estimate": self.raw_permutation_estimate,
            "canonical_plan_count": self.canonical_plan_count,
            "reduction_ratio_fixed": self.reduction_ratio_fixed,
            "compile_duration_ms": self.compile_duration_ms,
            "revalidation_regions": list(self.revalidation_regions),
            "rejected_reasons": list(self.rejected_reasons),
        }

    @classmethod
    def from_doc(cls, doc: Mapping[str, Any]) -> ActionGraphMetricsV2:
        raw = dict(doc)
        validate_doc(cls.SCHEMA_REF, raw)
        counts = raw["edge_count_by_kind"]
        return cls(
            legal_action_count=raw["legal_action_count"],
            edge_count_by_kind=tuple((kind, counts[kind.value]) for kind in EDGE_KINDS_IN_ORDER),
            independent_action_groups=raw["independent_action_groups"],
            raw_permutation_estimate=raw["raw_permutation_estimate"],
            canonical_plan_count=raw["canonical_plan_count"],
            reduction_ratio_fixed=raw["reduction_ratio_fixed"],
            compile_duration_ms=raw["compile_duration_ms"],
            revalidation_regions=tuple(raw["revalidation_regions"]),
            rejected_reasons=tuple(raw["rejected_reasons"]),
        )


def _graph_identity_doc(doc: Mapping[str, Any]) -> dict[str, Any]:
    body = {key: value for key, value in doc.items() if key != "graph_id"}
    metrics = dict(body["metrics"])
    metrics["compile_duration_ms"] = 0
    body["metrics"] = metrics
    return body


def _graph_id(doc: Mapping[str, Any]) -> str:
    return sha256_hex(canonical(_graph_identity_doc(doc)))


def _cycle_witness(nodes: tuple[str, ...], edges: tuple[ActionGraphEdgeV2, ...]) -> tuple[str, ...]:
    successors: dict[str, list[str]] = {node: [] for node in nodes}
    for edge in edges:
        if edge.kind in ORDERING_EDGE_KINDS:
            successors[edge.source_action_id].append(edge.target_action_id)
    for values in successors.values():
        values.sort()
    visiting: set[str] = set()
    visited: set[str] = set()
    stack: list[str] = []

    def visit(node: str) -> tuple[str, ...]:
        if node in visiting:
            start = stack.index(node)
            return tuple([*stack[start:], node])
        if node in visited:
            return ()
        visiting.add(node)
        stack.append(node)
        for successor in successors[node]:
            witness = visit(successor)
            if witness:
                return witness
        stack.pop()
        visiting.remove(node)
        visited.add(node)
        return ()

    for node in sorted(nodes):
        witness = visit(node)
        if witness:
            return witness
    return ()


@dataclass(frozen=True)
class ActionGraphV2:
    graph_id: str
    observation_id: str
    legal_action_set_id: str
    nodes: tuple[ActionGraphNodeV2, ...]
    edges: tuple[ActionGraphEdgeV2, ...]
    metrics: ActionGraphMetricsV2
    schema: int = SCHEMA_V2

    SCHEMA_REF: ClassVar[str] = "urn:civ-arena:graph:2#/$defs/ActionGraphV2"

    def __post_init__(self) -> None:
        if tuple(sorted(self.nodes, key=lambda item: item.node_id)) != self.nodes:
            raise ContractError("graph nodes must be unique and sorted by stable action_id")
        node_ids = tuple(node.node_id for node in self.nodes)
        if len(set(node_ids)) != len(node_ids):
            raise ContractError("graph node ids must be unique")
        if any(node.action.observation_id != self.observation_id for node in self.nodes):
            raise ContractError("every graph action must bind to the graph observation")
        if tuple(sorted(self.edges, key=lambda item: item.edge_id)) != self.edges:
            raise ContractError("graph edges must be unique and sorted by stable edge_id")
        if len({edge.edge_id for edge in self.edges}) != len(self.edges):
            raise ContractError("graph edge ids must be unique")
        if any(
            edge.source_action_id not in node_ids or edge.target_action_id not in node_ids
            for edge in self.edges
        ):
            raise ContractError("graph edge endpoint is absent from nodes")
        by_pair: dict[frozenset[str], set[EdgeKindV2]] = {}
        for edge in self.edges:
            pair = frozenset({edge.source_action_id, edge.target_action_id})
            by_pair.setdefault(pair, set()).add(edge.kind)
        for kinds in by_pair.values():
            if EdgeKindV2.COMMUTES_WITH in kinds and len(kinds) > 1:
                raise ContractError("COMMUTES_WITH cannot override another relationship")
        witness = _cycle_witness(node_ids, self.edges)
        if witness:
            raise ContractError("directed action-graph cycle: " + " -> ".join(witness))
        if self.metrics.legal_action_count != len(self.nodes):
            raise ContractError("graph metrics legal_action_count does not match nodes")
        actual_counts = {kind: 0 for kind in EDGE_KINDS_IN_ORDER}
        for edge in self.edges:
            actual_counts[edge.kind] += 1
        if dict(self.metrics.edge_count_by_kind) != actual_counts:
            raise ContractError("graph metrics edge counts do not match edges")
        doc = self.to_doc()
        validate_doc(self.SCHEMA_REF, doc)
        if self.graph_id != _graph_id(doc):
            raise ContractError("graph_id does not match canonical semantic graph bytes")

    @classmethod
    def create(
        cls,
        *,
        observation_id: str,
        legal_action_set_id: str,
        nodes: tuple[ActionGraphNodeV2, ...] | list[ActionGraphNodeV2],
        edges: tuple[ActionGraphEdgeV2, ...] | list[ActionGraphEdgeV2],
        metrics: ActionGraphMetricsV2,
    ) -> ActionGraphV2:
        ordered_nodes = tuple(sorted(nodes, key=lambda item: item.node_id))
        ordered_edges = tuple(sorted(edges, key=lambda item: item.edge_id))
        body = {
            "schema": SCHEMA_V2,
            "graph_id": ZERO_DIGEST,
            "observation_id": observation_id,
            "legal_action_set_id": legal_action_set_id,
            "nodes": [item.to_doc() for item in ordered_nodes],
            "edges": [item.to_doc() for item in ordered_edges],
            "metrics": metrics.to_doc(),
        }
        return cls(
            graph_id=_graph_id(body),
            observation_id=observation_id,
            legal_action_set_id=legal_action_set_id,
            nodes=ordered_nodes,
            edges=ordered_edges,
            metrics=metrics,
        )

    def to_doc(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "graph_id": self.graph_id,
            "observation_id": self.observation_id,
            "legal_action_set_id": self.legal_action_set_id,
            "nodes": [item.to_doc() for item in self.nodes],
            "edges": [item.to_doc() for item in self.edges],
            "metrics": self.metrics.to_doc(),
        }

    @classmethod
    def from_doc(cls, doc: Mapping[str, Any]) -> ActionGraphV2:
        raw = dict(doc)
        validate_doc(cls.SCHEMA_REF, raw)
        return cls(
            graph_id=raw["graph_id"],
            observation_id=raw["observation_id"],
            legal_action_set_id=raw["legal_action_set_id"],
            nodes=tuple(ActionGraphNodeV2.from_doc(item) for item in raw["nodes"]),
            edges=tuple(ActionGraphEdgeV2.from_doc(item) for item in raw["edges"]),
            metrics=ActionGraphMetricsV2.from_doc(raw["metrics"]),
            schema=raw["schema"],
        )


class AdapterKindV2(enum.StrEnum):
    FAKE = "fake"
    FIRETUNER = "firetuner"


class EnvironmentCapabilityV2(enum.StrEnum):
    ACTION_EXECUTION = "action_execution"
    DETERMINISTIC_FAKE_REPLAY = "deterministic_fake_replay"
    LIVE_OBSERVATIONAL_REPLAY = "live_observational_replay"
    PLAYER_OBSERVATION = "player_observation"
    PRIVATE_REFEREE_MONITOR = "private_referee_monitor"
    SAVE_LOAD_RESEARCH_ONLY = "save_load_research_only"
    TURN_EVENTS = "turn_events"


@dataclass(frozen=True)
class EnvironmentDescriptorV2:
    descriptor_id: str
    adapter_kind: AdapterKindV2
    adapter_version: str
    game_version: str
    ruleset_digest: str
    mod_digest: str
    capabilities: tuple[EnvironmentCapabilityV2, ...]
    deterministic: bool
    schema: int = SCHEMA_V2

    SCHEMA_REF: ClassVar[str] = (
        "urn:civ-arena:descriptor:2#/$defs/EnvironmentDescriptorV2"
    )

    def __post_init__(self) -> None:
        if not isinstance(self.adapter_kind, AdapterKindV2):
            raise ContractError("adapter_kind must be AdapterKindV2")
        if any(not isinstance(item, EnvironmentCapabilityV2) for item in self.capabilities):
            raise ContractError("environment capabilities must be typed")
        if tuple(sorted(set(self.capabilities), key=str)) != self.capabilities:
            raise ContractError("environment capabilities must be unique and sorted")
        if self.deterministic and (
            EnvironmentCapabilityV2.DETERMINISTIC_FAKE_REPLAY not in self.capabilities
        ):
            raise ContractError("deterministic environments must advertise fake replay")
        doc = self.to_doc()
        validate_doc(self.SCHEMA_REF, doc)
        _assert_identity(doc, "descriptor_id")

    @classmethod
    def create(
        cls,
        *,
        adapter_kind: AdapterKindV2,
        adapter_version: str,
        game_version: str,
        ruleset_digest: str,
        mod_digest: str,
        capabilities: tuple[EnvironmentCapabilityV2, ...]
        | list[EnvironmentCapabilityV2],
        deterministic: bool,
    ) -> EnvironmentDescriptorV2:
        caps = tuple(sorted(set(capabilities), key=str))
        body = {
            "schema": SCHEMA_V2,
            "descriptor_id": ZERO_DIGEST,
            "adapter_kind": adapter_kind.value,
            "adapter_version": adapter_version,
            "game_version": game_version,
            "ruleset_digest": ruleset_digest,
            "mod_digest": mod_digest,
            "capabilities": [item.value for item in caps],
            "deterministic": deterministic,
        }
        return cls(
            descriptor_id=_semantic_id(body, "descriptor_id"),
            adapter_kind=adapter_kind,
            adapter_version=adapter_version,
            game_version=game_version,
            ruleset_digest=ruleset_digest,
            mod_digest=mod_digest,
            capabilities=caps,
            deterministic=deterministic,
        )

    def to_doc(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "descriptor_id": self.descriptor_id,
            "adapter_kind": self.adapter_kind.value,
            "adapter_version": self.adapter_version,
            "game_version": self.game_version,
            "ruleset_digest": self.ruleset_digest,
            "mod_digest": self.mod_digest,
            "capabilities": [item.value for item in self.capabilities],
            "deterministic": self.deterministic,
        }

    @classmethod
    def from_doc(cls, doc: Mapping[str, Any]) -> EnvironmentDescriptorV2:
        raw = dict(doc)
        validate_doc(cls.SCHEMA_REF, raw)
        return cls(
            descriptor_id=raw["descriptor_id"],
            adapter_kind=AdapterKindV2(raw["adapter_kind"]),
            adapter_version=raw["adapter_version"],
            game_version=raw["game_version"],
            ruleset_digest=raw["ruleset_digest"],
            mod_digest=raw["mod_digest"],
            capabilities=tuple(EnvironmentCapabilityV2(item) for item in raw["capabilities"]),
            deterministic=raw["deterministic"],
            schema=raw["schema"],
        )


class PolicyKindV2(enum.StrEnum):
    SCRIPTED = "scripted"
    PLANNER = "planner"
    LLM = "llm"
    REPLAY = "replay"
    SYSTEM = "system"


@dataclass(frozen=True)
class PolicyDescriptorV2:
    descriptor_id: str
    policy_kind: PolicyKindV2
    policy_version: str
    artifact_digest: str | None
    provider: str | None
    model: str | None
    observation_only: bool
    registered_action_kinds: tuple[ActionKindV2, ...]
    schema: int = SCHEMA_V2

    SCHEMA_REF: ClassVar[str] = "urn:civ-arena:descriptor:2#/$defs/PolicyDescriptorV2"

    def __post_init__(self) -> None:
        if not isinstance(self.policy_kind, PolicyKindV2):
            raise ContractError("policy_kind must be PolicyKindV2")
        if self.observation_only is not True:
            raise ContractError("V2 policies must be observation-only")
        if any(not isinstance(item, ActionKindV2) for item in self.registered_action_kinds):
            raise ContractError("registered action kinds must be typed")
        if tuple(sorted(set(self.registered_action_kinds), key=str)) != (
            self.registered_action_kinds
        ):
            raise ContractError("registered action kinds must be unique and sorted")
        if self.policy_kind is PolicyKindV2.LLM and (self.provider is None or self.model is None):
            raise ContractError("LLM policy descriptors require provider and model identity")
        doc = self.to_doc()
        validate_doc(self.SCHEMA_REF, doc)
        _assert_identity(doc, "descriptor_id")

    @classmethod
    def create(
        cls,
        *,
        policy_kind: PolicyKindV2,
        policy_version: str,
        registered_action_kinds: tuple[ActionKindV2, ...] | list[ActionKindV2],
        artifact_digest: str | None = None,
        provider: str | None = None,
        model: str | None = None,
    ) -> PolicyDescriptorV2:
        kinds = tuple(sorted(set(registered_action_kinds), key=str))
        body = {
            "schema": SCHEMA_V2,
            "descriptor_id": ZERO_DIGEST,
            "policy_kind": policy_kind.value,
            "policy_version": policy_version,
            "artifact_digest": artifact_digest,
            "provider": provider,
            "model": model,
            "observation_only": True,
            "registered_action_kinds": [item.value for item in kinds],
        }
        return cls(
            descriptor_id=_semantic_id(body, "descriptor_id"),
            policy_kind=policy_kind,
            policy_version=policy_version,
            artifact_digest=artifact_digest,
            provider=provider,
            model=model,
            observation_only=True,
            registered_action_kinds=kinds,
        )

    def to_doc(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "descriptor_id": self.descriptor_id,
            "policy_kind": self.policy_kind.value,
            "policy_version": self.policy_version,
            "artifact_digest": self.artifact_digest,
            "provider": self.provider,
            "model": self.model,
            "observation_only": self.observation_only,
            "registered_action_kinds": [item.value for item in self.registered_action_kinds],
        }

    @classmethod
    def from_doc(cls, doc: Mapping[str, Any]) -> PolicyDescriptorV2:
        raw = dict(doc)
        validate_doc(cls.SCHEMA_REF, raw)
        return cls(
            descriptor_id=raw["descriptor_id"],
            policy_kind=PolicyKindV2(raw["policy_kind"]),
            policy_version=raw["policy_version"],
            artifact_digest=raw["artifact_digest"],
            provider=raw["provider"],
            model=raw["model"],
            observation_only=raw["observation_only"],
            registered_action_kinds=tuple(
                ActionKindV2(item) for item in raw["registered_action_kinds"]
            ),
            schema=raw["schema"],
        )


@dataclass(frozen=True)
class ArtifactRefV2:
    digest: str
    schema_ref: str
    byte_length: int
    media_type: str = "application/json"
    schema: int = SCHEMA_V2

    SCHEMA_REF: ClassVar[str] = "urn:civ-arena:artifact:2"

    def __post_init__(self) -> None:
        validate_doc(self.SCHEMA_REF, self.to_doc())

    def to_doc(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "digest": self.digest,
            "schema_ref": self.schema_ref,
            "byte_length": self.byte_length,
            "media_type": self.media_type,
        }

    @classmethod
    def from_doc(cls, doc: Mapping[str, Any]) -> ArtifactRefV2:
        raw = dict(doc)
        validate_doc(cls.SCHEMA_REF, raw)
        return cls(**raw)


@dataclass(frozen=True)
class ValidationCommandV2:
    command: str
    exit_code: int
    log_digest: str
    passed: int
    failed: int
    skipped: int

    SCHEMA_REF: ClassVar[str] = "urn:civ-arena:receipt:2#/$defs/ValidationCommandV2"

    def __post_init__(self) -> None:
        validate_doc(self.SCHEMA_REF, self.to_doc())

    def to_doc(self) -> dict[str, Any]:
        return {
            "command": self.command,
            "exit_code": self.exit_code,
            "log_digest": self.log_digest,
            "passed": self.passed,
            "failed": self.failed,
            "skipped": self.skipped,
        }

    @classmethod
    def from_doc(cls, doc: Mapping[str, Any]) -> ValidationCommandV2:
        raw = dict(doc)
        validate_doc(cls.SCHEMA_REF, raw)
        return cls(**raw)


@dataclass(frozen=True)
class ValidationReceiptV2:
    receipt_id: str
    commit_sha: str
    tree_sha: str
    source_diff_digest: str
    validator: str
    commands: tuple[ValidationCommandV2, ...]
    schema: int = SCHEMA_V2

    SCHEMA_REF: ClassVar[str] = "urn:civ-arena:receipt:2#/$defs/ValidationReceiptV2"

    def __post_init__(self) -> None:
        if not self.commands:
            raise ContractError("validation receipt requires at least one command")
        doc = self.to_doc()
        validate_doc(self.SCHEMA_REF, doc)
        _assert_identity(doc, "receipt_id")

    @classmethod
    def create(
        cls,
        *,
        commit_sha: str,
        tree_sha: str,
        source_diff_digest: str,
        validator: str,
        commands: tuple[ValidationCommandV2, ...] | list[ValidationCommandV2],
    ) -> ValidationReceiptV2:
        frozen = tuple(commands)
        body = {
            "schema": SCHEMA_V2,
            "receipt_id": ZERO_DIGEST,
            "commit_sha": commit_sha,
            "tree_sha": tree_sha,
            "source_diff_digest": source_diff_digest,
            "validator": validator,
            "commands": [item.to_doc() for item in frozen],
        }
        return cls(
            receipt_id=_semantic_id(body, "receipt_id"),
            commit_sha=commit_sha,
            tree_sha=tree_sha,
            source_diff_digest=source_diff_digest,
            validator=validator,
            commands=frozen,
        )

    def to_doc(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "receipt_id": self.receipt_id,
            "commit_sha": self.commit_sha,
            "tree_sha": self.tree_sha,
            "source_diff_digest": self.source_diff_digest,
            "validator": self.validator,
            "commands": [item.to_doc() for item in self.commands],
        }

    @classmethod
    def from_doc(cls, doc: Mapping[str, Any]) -> ValidationReceiptV2:
        raw = dict(doc)
        validate_doc(cls.SCHEMA_REF, raw)
        return cls(
            receipt_id=raw["receipt_id"],
            commit_sha=raw["commit_sha"],
            tree_sha=raw["tree_sha"],
            source_diff_digest=raw["source_diff_digest"],
            validator=raw["validator"],
            commands=tuple(ValidationCommandV2.from_doc(item) for item in raw["commands"]),
            schema=raw["schema"],
        )


class TurnTerminationV2(enum.StrEnum):
    COMPLETED = "completed"
    FAILED = "failed"
    MANDATORY_UNRESOLVED = "mandatory_unresolved"
    REPLANS_EXHAUSTED = "replans_exhausted"


@dataclass(frozen=True)
class TurnReceiptV2:
    receipt_id: str
    episode_id: str
    turn: int
    player_id: int
    pre_observation_id: str
    post_observation_id: str
    proposal_id: str
    graph_ids: tuple[str, ...]
    authorizations: tuple[AuthorizationV2, ...]
    results: tuple[ActionResultV2, ...]
    replan_count: int
    termination: TurnTerminationV2
    safe_error: str | None
    schema: int = SCHEMA_V2

    SCHEMA_REF: ClassVar[str] = "urn:civ-arena:receipt:2#/$defs/TurnReceiptV2"

    def __post_init__(self) -> None:
        if not isinstance(self.termination, TurnTerminationV2):
            raise ContractError("turn termination must be TurnTerminationV2")
        if not self.graph_ids:
            raise ContractError("turn receipt must bind at least one graph")
        auth_ids = {item.authorization_id for item in self.authorizations}
        if any(result.authorization_id not in auth_ids for result in self.results):
            raise ContractError("action result lacks its authorization in the turn receipt")
        if self.termination is TurnTerminationV2.COMPLETED and self.safe_error is not None:
            raise ContractError("completed turn receipt cannot carry an error")
        doc = self.to_doc()
        validate_doc(self.SCHEMA_REF, doc)
        _assert_identity(doc, "receipt_id")

    @classmethod
    def create(
        cls,
        *,
        episode_id: str,
        turn: int,
        player_id: int,
        pre_observation_id: str,
        post_observation_id: str,
        proposal_id: str,
        graph_ids: tuple[str, ...] | list[str],
        authorizations: tuple[AuthorizationV2, ...] | list[AuthorizationV2],
        results: tuple[ActionResultV2, ...] | list[ActionResultV2],
        replan_count: int,
        termination: TurnTerminationV2,
        safe_error: str | None = None,
    ) -> TurnReceiptV2:
        frozen_graphs = tuple(graph_ids)
        frozen_auth = tuple(authorizations)
        frozen_results = tuple(results)
        body = {
            "schema": SCHEMA_V2,
            "receipt_id": ZERO_DIGEST,
            "episode_id": episode_id,
            "turn": turn,
            "player_id": player_id,
            "pre_observation_id": pre_observation_id,
            "post_observation_id": post_observation_id,
            "proposal_id": proposal_id,
            "graph_ids": list(frozen_graphs),
            "authorizations": [item.to_doc() for item in frozen_auth],
            "results": [item.to_doc() for item in frozen_results],
            "replan_count": replan_count,
            "termination": termination.value,
            "safe_error": safe_error,
        }
        return cls(
            receipt_id=_semantic_id(body, "receipt_id"),
            episode_id=episode_id,
            turn=turn,
            player_id=player_id,
            pre_observation_id=pre_observation_id,
            post_observation_id=post_observation_id,
            proposal_id=proposal_id,
            graph_ids=frozen_graphs,
            authorizations=frozen_auth,
            results=frozen_results,
            replan_count=replan_count,
            termination=termination,
            safe_error=safe_error,
        )

    def to_doc(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "receipt_id": self.receipt_id,
            "episode_id": self.episode_id,
            "turn": self.turn,
            "player_id": self.player_id,
            "pre_observation_id": self.pre_observation_id,
            "post_observation_id": self.post_observation_id,
            "proposal_id": self.proposal_id,
            "graph_ids": list(self.graph_ids),
            "authorizations": [item.to_doc() for item in self.authorizations],
            "results": [item.to_doc() for item in self.results],
            "replan_count": self.replan_count,
            "termination": self.termination.value,
            "safe_error": self.safe_error,
        }

    @classmethod
    def from_doc(cls, doc: Mapping[str, Any]) -> TurnReceiptV2:
        raw = dict(doc)
        validate_doc(cls.SCHEMA_REF, raw)
        return cls(
            receipt_id=raw["receipt_id"],
            episode_id=raw["episode_id"],
            turn=raw["turn"],
            player_id=raw["player_id"],
            pre_observation_id=raw["pre_observation_id"],
            post_observation_id=raw["post_observation_id"],
            proposal_id=raw["proposal_id"],
            graph_ids=tuple(raw["graph_ids"]),
            authorizations=tuple(
                AuthorizationV2.from_doc(item) for item in raw["authorizations"]
            ),
            results=tuple(ActionResultV2.from_doc(item) for item in raw["results"]),
            replan_count=raw["replan_count"],
            termination=TurnTerminationV2(raw["termination"]),
            safe_error=raw["safe_error"],
            schema=raw["schema"],
        )


class ExecutionModeV2(enum.StrEnum):
    DAG_TX = "dag_tx"
    SEQUENTIAL = "sequential"
    LEGAL_LIST = "legal_list"
    DAG = "dag"


@dataclass(frozen=True)
class ComputeConfigV2:
    execution_mode: ExecutionModeV2
    max_graph_actions: int
    max_replans_per_turn: int

    SCHEMA_REF: ClassVar[str] = "urn:civ-arena:receipt:2#/$defs/ComputeConfigV2"

    def __post_init__(self) -> None:
        if not isinstance(self.execution_mode, ExecutionModeV2):
            raise ContractError("execution_mode must be ExecutionModeV2")
        validate_doc(self.SCHEMA_REF, self.to_doc())

    def to_doc(self) -> dict[str, Any]:
        return {
            "execution_mode": self.execution_mode.value,
            "max_graph_actions": self.max_graph_actions,
            "max_replans_per_turn": self.max_replans_per_turn,
        }

    @classmethod
    def from_doc(cls, doc: Mapping[str, Any]) -> ComputeConfigV2:
        raw = dict(doc)
        validate_doc(cls.SCHEMA_REF, raw)
        return cls(
            execution_mode=ExecutionModeV2(raw["execution_mode"]),
            max_graph_actions=raw["max_graph_actions"],
            max_replans_per_turn=raw["max_replans_per_turn"],
        )


class EpisodeTerminationV2(enum.StrEnum):
    SUCCESS = "success"
    FAILURE = "failure"
    CANCELLED = "cancelled"
    TIMEOUT = "timeout"
    RECOVERED_CRASH = "recovered_crash"


def _episode_receipt_identity_doc(doc: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: (ZERO_DIGEST if key == "terminal_event_hash" else value)
        for key, value in doc.items()
        if key != "receipt_id"
    }


def _episode_receipt_id(doc: Mapping[str, Any]) -> str:
    return sha256_hex(canonical(_episode_receipt_identity_doc(doc)))


@dataclass(frozen=True)
class EpisodeReceiptV2:
    receipt_id: str
    episode_id: str
    parent_episode_id: str | None
    parent_terminal_event_hash: str | None
    environment: EnvironmentDescriptorV2
    policies: tuple[PolicyDescriptorV2, ...]
    seed: int | None
    compute: ComputeConfigV2
    scored: bool
    termination_reason: EpisodeTerminationV2
    turns_completed: int
    terminal_event_hash: str
    artifacts: tuple[ArtifactRefV2, ...]
    schema: int = SCHEMA_V2

    SCHEMA_REF: ClassVar[str] = "urn:civ-arena:receipt:2#/$defs/EpisodeReceiptV2"

    def __post_init__(self) -> None:
        if not isinstance(self.termination_reason, EpisodeTerminationV2):
            raise ContractError("episode termination must be EpisodeTerminationV2")
        if not self.policies:
            raise ContractError("episode receipt requires at least one policy")
        if tuple(sorted(self.policies, key=lambda item: item.descriptor_id)) != self.policies:
            raise ContractError("episode policies must be unique and sorted")
        if len({item.descriptor_id for item in self.policies}) != len(self.policies):
            raise ContractError("episode policy descriptors must be unique")
        if tuple(sorted(self.artifacts, key=lambda item: item.digest)) != self.artifacts:
            raise ContractError("episode artifacts must be sorted by digest")
        if len({item.digest for item in self.artifacts}) != len(self.artifacts):
            raise ContractError("episode artifact digests must be unique")
        has_parent = self.parent_episode_id is not None
        if has_parent != (self.parent_terminal_event_hash is not None):
            raise ContractError("resume parent id and terminal hash must appear together")
        if self.scored and has_parent:
            raise ContractError("scored V2 episodes cannot resume")
        if self.scored and self.compute.execution_mode is not ExecutionModeV2.DAG_TX:
            raise ContractError("scored V2 episodes require dag_tx")
        doc = self.to_doc()
        validate_doc(self.SCHEMA_REF, doc)
        if self.receipt_id != _episode_receipt_id(doc):
            raise ContractError("receipt_id does not match canonical episode bytes")

    @classmethod
    def create(
        cls,
        *,
        episode_id: str,
        environment: EnvironmentDescriptorV2,
        policies: tuple[PolicyDescriptorV2, ...] | list[PolicyDescriptorV2],
        seed: int | None,
        compute: ComputeConfigV2,
        scored: bool,
        termination_reason: EpisodeTerminationV2,
        turns_completed: int,
        terminal_event_hash: str = ZERO_DIGEST,
        artifacts: tuple[ArtifactRefV2, ...] | list[ArtifactRefV2] = (),
        parent_episode_id: str | None = None,
        parent_terminal_event_hash: str | None = None,
    ) -> EpisodeReceiptV2:
        ordered_policies = tuple(sorted(policies, key=lambda item: item.descriptor_id))
        ordered_artifacts = tuple(sorted(artifacts, key=lambda item: item.digest))
        body = {
            "schema": SCHEMA_V2,
            "receipt_id": ZERO_DIGEST,
            "episode_id": episode_id,
            "parent_episode_id": parent_episode_id,
            "parent_terminal_event_hash": parent_terminal_event_hash,
            "environment": environment.to_doc(),
            "policies": [item.to_doc() for item in ordered_policies],
            "seed": seed,
            "compute": compute.to_doc(),
            "scored": scored,
            "termination_reason": termination_reason.value,
            "turns_completed": turns_completed,
            "terminal_event_hash": terminal_event_hash,
            "artifacts": [item.to_doc() for item in ordered_artifacts],
        }
        return cls(
            receipt_id=_episode_receipt_id(body),
            episode_id=episode_id,
            parent_episode_id=parent_episode_id,
            parent_terminal_event_hash=parent_terminal_event_hash,
            environment=environment,
            policies=ordered_policies,
            seed=seed,
            compute=compute,
            scored=scored,
            termination_reason=termination_reason,
            turns_completed=turns_completed,
            terminal_event_hash=terminal_event_hash,
            artifacts=ordered_artifacts,
        )

    def bind_terminal_event(self, event_hash: str) -> EpisodeReceiptV2:
        return EpisodeReceiptV2.create(
            episode_id=self.episode_id,
            parent_episode_id=self.parent_episode_id,
            parent_terminal_event_hash=self.parent_terminal_event_hash,
            environment=self.environment,
            policies=self.policies,
            seed=self.seed,
            compute=self.compute,
            scored=self.scored,
            termination_reason=self.termination_reason,
            turns_completed=self.turns_completed,
            terminal_event_hash=event_hash,
            artifacts=self.artifacts,
        )

    def to_doc(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "receipt_id": self.receipt_id,
            "episode_id": self.episode_id,
            "parent_episode_id": self.parent_episode_id,
            "parent_terminal_event_hash": self.parent_terminal_event_hash,
            "environment": self.environment.to_doc(),
            "policies": [item.to_doc() for item in self.policies],
            "seed": self.seed,
            "compute": self.compute.to_doc(),
            "scored": self.scored,
            "termination_reason": self.termination_reason.value,
            "turns_completed": self.turns_completed,
            "terminal_event_hash": self.terminal_event_hash,
            "artifacts": [item.to_doc() for item in self.artifacts],
        }

    @classmethod
    def from_doc(cls, doc: Mapping[str, Any]) -> EpisodeReceiptV2:
        raw = dict(doc)
        validate_doc(cls.SCHEMA_REF, raw)
        return cls(
            receipt_id=raw["receipt_id"],
            episode_id=raw["episode_id"],
            parent_episode_id=raw["parent_episode_id"],
            parent_terminal_event_hash=raw["parent_terminal_event_hash"],
            environment=EnvironmentDescriptorV2.from_doc(raw["environment"]),
            policies=tuple(PolicyDescriptorV2.from_doc(item) for item in raw["policies"]),
            seed=raw["seed"],
            compute=ComputeConfigV2.from_doc(raw["compute"]),
            scored=raw["scored"],
            termination_reason=EpisodeTerminationV2(raw["termination_reason"]),
            turns_completed=raw["turns_completed"],
            terminal_event_hash=raw["terminal_event_hash"],
            artifacts=tuple(ArtifactRefV2.from_doc(item) for item in raw["artifacts"]),
            schema=raw["schema"],
        )


class EventTypeV2(enum.StrEnum):
    EPISODE_STARTED = "EpisodeStarted"
    OBSERVATION_RECORDED = "ObservationRecorded"
    LEGAL_ACTIONS_RECORDED = "LegalActionsRecorded"
    ACTION_GRAPH_COMPILED = "ActionGraphCompiled"
    POLICY_PROPOSAL_RECORDED = "PolicyProposalRecorded"
    ACTION_AUTHORIZED = "ActionAuthorized"
    ACTION_EXECUTION_STARTED = "ActionExecutionStarted"
    ACTION_EXECUTION_COMPLETED = "ActionExecutionCompleted"
    POSTCONDITION_VERIFIED = "PostconditionVerified"
    GRAPH_REGION_INVALIDATED = "GraphRegionInvalidated"
    TURN_COMPLETED = "TurnCompleted"
    EPISODE_TERMINATED = "EpisodeTerminated"
    VALIDATION_RECORDED = "ValidationRecorded"


type EventPayloadValue = (
    ArtifactRefV2 | AuthorizationV2 | ActionResultV2 | TurnReceiptV2 | EpisodeReceiptV2 | str
)

EVENT_PAYLOAD_NAME: dict[EventTypeV2, str] = {
    EventTypeV2.EPISODE_STARTED: "artifact",
    EventTypeV2.OBSERVATION_RECORDED: "artifact",
    EventTypeV2.LEGAL_ACTIONS_RECORDED: "artifact",
    EventTypeV2.ACTION_GRAPH_COMPILED: "artifact",
    EventTypeV2.POLICY_PROPOSAL_RECORDED: "artifact",
    EventTypeV2.ACTION_AUTHORIZED: "authorization",
    EventTypeV2.ACTION_EXECUTION_STARTED: "message_code",
    EventTypeV2.ACTION_EXECUTION_COMPLETED: "result",
    EventTypeV2.POSTCONDITION_VERIFIED: "result",
    EventTypeV2.GRAPH_REGION_INVALIDATED: "message_code",
    EventTypeV2.TURN_COMPLETED: "turn_receipt",
    EventTypeV2.EPISODE_TERMINATED: "episode_receipt",
    EventTypeV2.VALIDATION_RECORDED: "artifact",
}


def _payload_doc(name: str, value: EventPayloadValue) -> dict[str, Any]:
    return {name: value if isinstance(value, str) else value.to_doc()}


def _event_semantic_doc(doc: Mapping[str, Any]) -> dict[str, Any]:
    payload = dict(doc["payload"])
    episode = payload.get("episode_receipt")
    if isinstance(episode, dict):
        episode = dict(episode)
        episode["terminal_event_hash"] = ZERO_DIGEST
        payload["episode_receipt"] = episode
    return {
        "schema": doc["schema"],
        "event_type": doc["event_type"],
        "schema_ref": doc["schema_ref"],
        "episode_id": doc["episode_id"],
        "turn_id": doc["turn_id"],
        "correlation_id": doc["correlation_id"],
        "causation_id": doc["causation_id"],
        "payload": payload,
    }


def event_semantic_hash(doc: Mapping[str, Any]) -> str:
    return sha256_hex(canonical(_event_semantic_doc(doc)))


def event_identity_hash(doc: Mapping[str, Any]) -> str:
    return sha256_hex(
        canonical(
            {
                "episode_id": doc["episode_id"],
                "sequence_number": doc["sequence_number"],
                "semantic_hash": doc["semantic_hash"],
            }
        )
    )


def event_chain_hash(doc: Mapping[str, Any]) -> str:
    return sha256_hex(
        canonical(
            {
                "previous_event_hash": doc["previous_event_hash"],
                "sequence_number": doc["sequence_number"],
                "occurred_at": doc["occurred_at"],
                "semantic_hash": doc["semantic_hash"],
            }
        )
    )


@dataclass(frozen=True)
class EventV2:
    event_id: str
    event_type: EventTypeV2
    schema_ref: str
    episode_id: str
    turn_id: int | None
    correlation_id: str
    causation_id: str | None
    sequence_number: int
    occurred_at: str
    payload_name: str
    payload_value: EventPayloadValue
    previous_event_hash: str
    semantic_hash: str
    event_hash: str
    schema: int = SCHEMA_V2

    SCHEMA_REF: ClassVar[str] = "urn:civ-arena:event:2"

    def __post_init__(self) -> None:
        if not isinstance(self.event_type, EventTypeV2):
            raise ContractError("event_type must be EventTypeV2")
        if EVENT_PAYLOAD_NAME[self.event_type] != self.payload_name:
            raise ContractError("event payload does not match its event type")
        if isinstance(self.payload_value, ArtifactRefV2):
            expected_schema_ref = self.payload_value.schema_ref
        elif isinstance(self.payload_value, str):
            expected_schema_ref = (
                LegalActionV2.SCHEMA_REF
                if self.event_type is EventTypeV2.ACTION_EXECUTION_STARTED
                else ActionGraphV2.SCHEMA_REF
            )
        else:
            expected_schema_ref = self.payload_value.SCHEMA_REF
        if self.schema_ref != expected_schema_ref:
            raise ContractError("event schema_ref does not match its typed payload")
        doc = self.to_doc()
        validate_doc(self.SCHEMA_REF, doc)
        semantic_hash = event_semantic_hash(doc)
        if self.semantic_hash != semantic_hash:
            raise ContractError("event semantic identity mismatch")
        if self.event_id != event_identity_hash(doc):
            raise ContractError("event identity mismatch")
        if self.event_hash != event_chain_hash(doc):
            raise ContractError("event chain hash mismatch")
        if self.event_type is EventTypeV2.EPISODE_TERMINATED:
            receipt = self.payload_value
            if not isinstance(receipt, EpisodeReceiptV2):
                raise ContractError("EpisodeTerminated must embed EpisodeReceiptV2")
            if receipt.terminal_event_hash != self.event_hash:
                raise ContractError("episode receipt is not bound to its terminal event")

    @classmethod
    def create(
        cls,
        *,
        event_type: EventTypeV2,
        schema_ref: str,
        episode_id: str,
        turn_id: int | None,
        correlation_id: str,
        causation_id: str | None,
        sequence_number: int,
        occurred_at: str,
        payload_value: EventPayloadValue,
        previous_event_hash: str,
    ) -> EventV2:
        payload_name = EVENT_PAYLOAD_NAME[event_type]
        body = {
            "schema": SCHEMA_V2,
            "event_id": ZERO_DIGEST,
            "event_type": event_type.value,
            "schema_ref": schema_ref,
            "episode_id": episode_id,
            "turn_id": turn_id,
            "correlation_id": correlation_id,
            "causation_id": causation_id,
            "sequence_number": sequence_number,
            "occurred_at": occurred_at,
            "payload": _payload_doc(payload_name, payload_value),
            "previous_event_hash": previous_event_hash,
            "semantic_hash": ZERO_DIGEST,
            "event_hash": ZERO_DIGEST,
        }
        semantic_hash = event_semantic_hash(body)
        body["semantic_hash"] = semantic_hash
        event_id = event_identity_hash(body)
        body["event_id"] = event_id
        chain_hash = event_chain_hash(body)
        if event_type is EventTypeV2.EPISODE_TERMINATED:
            if not isinstance(payload_value, EpisodeReceiptV2):
                raise ContractError("EpisodeTerminated requires EpisodeReceiptV2")
            payload_value = payload_value.bind_terminal_event(chain_hash)
            body["payload"] = _payload_doc(payload_name, payload_value)
            # terminal_event_hash is intentionally normalized out of semantic_hash
            assert event_semantic_hash(body) == semantic_hash
            assert event_chain_hash(body) == chain_hash
        return cls(
            event_id=event_id,
            event_type=event_type,
            schema_ref=schema_ref,
            episode_id=episode_id,
            turn_id=turn_id,
            correlation_id=correlation_id,
            causation_id=causation_id,
            sequence_number=sequence_number,
            occurred_at=occurred_at,
            payload_name=payload_name,
            payload_value=payload_value,
            previous_event_hash=previous_event_hash,
            semantic_hash=semantic_hash,
            event_hash=chain_hash,
        )

    def to_doc(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "event_id": self.event_id,
            "event_type": self.event_type.value,
            "schema_ref": self.schema_ref,
            "episode_id": self.episode_id,
            "turn_id": self.turn_id,
            "correlation_id": self.correlation_id,
            "causation_id": self.causation_id,
            "sequence_number": self.sequence_number,
            "occurred_at": self.occurred_at,
            "payload": _payload_doc(self.payload_name, self.payload_value),
            "previous_event_hash": self.previous_event_hash,
            "semantic_hash": self.semantic_hash,
            "event_hash": self.event_hash,
        }

    @classmethod
    def from_doc(cls, doc: Mapping[str, Any]) -> EventV2:
        raw = dict(doc)
        validate_doc(cls.SCHEMA_REF, raw)
        event_type = EventTypeV2(raw["event_type"])
        payload_name = EVENT_PAYLOAD_NAME[event_type]
        payload_raw = raw["payload"][payload_name]
        parsers: dict[str, Any] = {
            "artifact": ArtifactRefV2.from_doc,
            "authorization": AuthorizationV2.from_doc,
            "episode_receipt": EpisodeReceiptV2.from_doc,
            "result": ActionResultV2.from_doc,
            "turn_receipt": TurnReceiptV2.from_doc,
        }
        payload_value = (
            payload_raw if payload_name == "message_code" else parsers[payload_name](payload_raw)
        )
        return cls(
            event_id=raw["event_id"],
            event_type=event_type,
            schema_ref=raw["schema_ref"],
            episode_id=raw["episode_id"],
            turn_id=raw["turn_id"],
            correlation_id=raw["correlation_id"],
            causation_id=raw["causation_id"],
            sequence_number=raw["sequence_number"],
            occurred_at=raw["occurred_at"],
            payload_name=payload_name,
            payload_value=payload_value,
            previous_event_hash=raw["previous_event_hash"],
            semantic_hash=raw["semantic_hash"],
            event_hash=raw["event_hash"],
            schema=raw["schema"],
        )


@dataclass(frozen=True)
class TurnContextV2:
    """Policy input. No adapter, referee, or mutation callable crosses this seam."""

    observation: ObservationV2
    legal_actions: LegalActionSetV2
    graph: ActionGraphV2
    policy: PolicyDescriptorV2

    def __post_init__(self) -> None:
        if self.legal_actions.observation_id != self.observation.observation_id:
            raise ContractError("turn context legal actions are stale")
        if self.graph.observation_id != self.observation.observation_id:
            raise ContractError("turn context graph is stale")
        if self.graph.legal_action_set_id != self.legal_actions.legal_action_set_id:
            raise ContractError("turn context graph is bound to another action set")
