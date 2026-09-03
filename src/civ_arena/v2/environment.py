"""Split observable execution and private referee-monitor environment facets."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from civ_arena.arena.visibility import Scope, VisibilityPolicy
from civ_arena.canonical import canonical, sha256_hex
from civ_arena.game.adapter import ActionCommand, MutationRecord, ObserveKind, ObserveRequest
from civ_arena.game.sim.simulator import SimulatorAdapter
from civ_arena.game.sim.state import BUILDINGS, TECHS, TERRAIN, UNIT_TECH_REQ, UNIT_TYPES
from civ_arena.v2.contracts import (
    ActionKindV2,
    ActionStatusV2,
    AdapterKindV2,
    AuthorizationDecisionV2,
    AuthorizationV2,
    EntityRefV2,
    EntityTypeV2,
    EnvironmentCapabilityV2,
    EnvironmentDescriptorV2,
    FactSourceV2,
    FactSubjectScopeV2,
    KnowledgeValueV2,
    LegalActionV2,
    ObservableFactV2,
    ObservationPhaseV2,
    ObservationV2,
)
from civ_arena.v2.schemas import ContractError

FAKE_GAME_VERSION = "civ-arena-sim-v2"
FAKE_RULESET_DIGEST = sha256_hex(
    canonical(
        {
            "buildings": BUILDINGS,
            "techs": TECHS,
            "terrain": TERRAIN,
            "unit_tech_requirements": UNIT_TECH_REQ,
            "unit_types": UNIT_TYPES,
        }
    )
)
NO_MOD_DIGEST = sha256_hex(canonical({"mod": "none"}))


@dataclass(frozen=True)
class EnvironmentExecutionV2:
    """Receipt-safe adapter outcome. Raw engine errors/mutations never cross."""

    status: ActionStatusV2
    rejection_code: str | None = None
    safe_message: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.status, ActionStatusV2):
            raise ContractError("environment execution status must be ActionStatusV2")
        failed = self.status in {ActionStatusV2.REJECTED, ActionStatusV2.DIVERGED}
        if failed != (self.rejection_code is not None):
            raise ContractError("failed environment executions require a safe reason code")
        if self.safe_message is not None and (
            len(self.safe_message) > 160
            or any(ord(character) < 32 for character in self.safe_message)
        ):
            raise ContractError("environment safe message is invalid")


@runtime_checkable
class ObservableExecutionFacetV2(Protocol):
    @property
    def descriptor(self) -> EnvironmentDescriptorV2: ...

    async def reset(self, config: Mapping[str, Any]) -> None: ...

    async def begin_turn(self, player_id: int, turn: int) -> ObservationV2: ...

    async def observe(self, player_id: int) -> ObservationV2: ...

    async def execute_authorized(
        self, authorization: AuthorizationV2, action: LegalActionV2
    ) -> EnvironmentExecutionV2: ...


@runtime_checkable
class PrivateRefereeMonitorV2(Protocol):
    def snapshot(self) -> Any: ...

    def restore(self, snapshot: Any) -> None: ...

    def state_hash(self) -> str: ...

    def drain_mutations(self) -> list[Any]: ...

    def drain_authorized_mutations(self) -> list[Any]: ...


def _player_ref(player_id: int) -> EntityRefV2:
    return EntityRefV2(EntityTypeV2.PLAYER, f"p{player_id}")


def _fact(
    subject: EntityRefV2,
    scope: FactSubjectScopeV2,
    predicate: str,
    value: Any,
    turn: int,
    *,
    source: FactSourceV2 = FactSourceV2.DIRECT,
) -> ObservableFactV2:
    if isinstance(value, list):
        value = tuple(value)
    return ObservableFactV2(
        subject=subject,
        subject_scope=scope,
        predicate=predicate,
        value=KnowledgeValueV2.known(value, observed_turn=turn),
        source=source,
    )


def _entity_ref(kind: EntityTypeV2, entity_id: str) -> EntityRefV2:
    return EntityRefV2(kind, entity_id)


class _AdapterCoreV2:
    """Private shared core captured by two closure-backed facets."""

    def __init__(
        self,
        adapter: Any,
        descriptor: EnvironmentDescriptorV2,
        visibility: VisibilityPolicy,
    ) -> None:
        self.adapter = adapter
        self.descriptor = descriptor
        self.visibility = visibility
        self.observation_sequence = 0
        self.last_observations: dict[int, ObservationV2] = {}
        self.observation_owners: dict[str, int] = {}
        self.authorized_mutations: list[MutationRecord] = []

    async def reset(self, config: Mapping[str, Any]) -> None:
        await self.adapter.setup(dict(config))
        self.observation_sequence = 0
        self.last_observations.clear()
        self.observation_owners.clear()
        self.authorized_mutations.clear()

    async def begin_turn(self, player_id: int, turn: int) -> ObservationV2:
        info = await self.adapter.begin_phase(player_id, turn)
        self.authorized_mutations.extend(
            MutationRecord.from_doc(item) for item in info.get("manifest", [])
        )
        return await self.observe(player_id)

    async def _project(
        self,
        player_id: int,
        kind: ObserveKind,
        *,
        subject_id: str | None = None,
    ) -> Any:
        raw = await self.adapter.observe(ObserveRequest(kind, player_id, subject_id))
        observable, remembered = self.adapter.visibility_for(player_id)
        return self.visibility.project(
            raw,
            kind.value,
            player_id,
            observable,
            remembered,
            Scope.PRIVATE_PLAYER,
        )

    async def observe(self, player_id: int) -> ObservationV2:
        phase = await self.adapter.current_phase()
        overview = await self._project(player_id, ObserveKind.OVERVIEW)
        units = await self._project(player_id, ObserveKind.UNITS)
        cities = await self._project(player_id, ObserveKind.CITIES)
        visible_map = await self._project(player_id, ObserveKind.VISIBLE_MAP)
        research = await self._project(player_id, ObserveKind.AVAILABLE_RESEARCH)
        production: dict[str, list[dict[str, Any]]] = {}
        for city in cities:
            if city.get("owner", city.get("owner_id")) == player_id:
                production[city["city_id"]] = await self._project(
                    player_id,
                    ObserveKind.AVAILABLE_PRODUCTION,
                    subject_id=city["city_id"],
                )
        turn = int(overview["turn"])
        facts = self._facts_from_projection(
            player_id,
            turn,
            overview,
            units,
            cities,
            visible_map,
            research,
            production,
        )
        mandatory: list[ActionKindV2] = []
        if not overview["you"].get("researching") and research:
            mandatory.append(ActionKindV2.SET_RESEARCH)
        if any(
            not city.get("production_queue")
            for city in cities
            if city.get("owner", city.get("owner_id")) == player_id
        ):
            mandatory.append(ActionKindV2.SET_CITY_PRODUCTION)
        phase_player = int(phase["phase_player"])
        if phase_player == player_id:
            observation_phase = ObservationPhaseV2.TURN
            active = _player_ref(player_id)
        elif phase_player == -1:
            observation_phase = ObservationPhaseV2.POST_TURN
            active = EntityRefV2(EntityTypeV2.SYSTEM, "no-active-player")
        else:
            observation_phase = ObservationPhaseV2.PRE_TURN
            active = _player_ref(phase_player)
        observation = ObservationV2.create(
            environment_id=self.descriptor.descriptor_id,
            game_version=self.descriptor.game_version,
            ruleset_digest=self.descriptor.ruleset_digest,
            turn=turn,
            active_player=active,
            observing_player=_player_ref(player_id),
            phase=observation_phase,
            observed_at_seq=self.observation_sequence,
            facts=facts,
            mandatory_action_kinds=mandatory,
        )
        self.observation_sequence += 1
        self.last_observations[player_id] = observation
        self.observation_owners[observation.observation_id] = player_id
        return observation

    def _facts_from_projection(
        self,
        player_id: int,
        turn: int,
        overview: dict[str, Any],
        units: list[dict[str, Any]],
        cities: list[dict[str, Any]],
        visible_map: dict[str, Any],
        research: list[dict[str, Any]],
        production: dict[str, list[dict[str, Any]]],
    ) -> list[ObservableFactV2]:
        facts: list[ObservableFactV2] = []
        player = _player_ref(player_id)
        for key in (
            "civ_name",
            "gold",
            "researched",
            "researching",
            "science_bucket",
        ):
            if key in overview["you"] and overview["you"][key] is not None:
                facts.append(
                    _fact(
                        player,
                        FactSubjectScopeV2.SELF,
                        key,
                        overview["you"][key],
                        turn,
                    )
                )
        for public in overview["public"]["players"]:
            public_ref = _player_ref(int(public["player_id"]))
            scope = (
                FactSubjectScopeV2.SELF
                if int(public["player_id"]) == player_id
                else FactSubjectScopeV2.PUBLIC
            )
            for key, predicate in (("civ_name", "civ_name"), ("alive", "alive")):
                if scope is FactSubjectScopeV2.SELF and key == "civ_name":
                    continue
                if key in public:
                    facts.append(_fact(public_ref, scope, predicate, public[key], turn))

        unit_keys = (
            "owner_id",
            "type",
            "coord",
            "hp",
            "hp_bucket",
            "movement",
            "max_movement",
            "strength",
            "ranged_strength",
            "fortified",
        )
        for unit in units:
            unit_ref = _entity_ref(EntityTypeV2.UNIT, unit["unit_id"])
            owned = int(unit["owner_id"]) == player_id
            scope = (
                FactSubjectScopeV2.OWNED
                if owned
                else FactSubjectScopeV2.VISIBLE_FOREIGN
            )
            if owned:
                facts.append(_fact(unit_ref, scope, "available", True, turn))
            for key in unit_keys:
                if key not in unit:
                    continue
                if not owned and key == "hp":
                    continue
                facts.append(_fact(unit_ref, scope, key, unit[key], turn))

        city_keys = (
            "owner_id",
            "name",
            "coord",
            "hp",
            "population",
            "production_queue",
            "food_bucket",
            "production_bucket",
            "buildings",
            "border_radius",
        )
        for city in cities:
            city_ref = _entity_ref(EntityTypeV2.CITY, city["city_id"])
            owner = int(city.get("owner", city.get("owner_id")))
            owned = owner == player_id
            scope = (
                FactSubjectScopeV2.OWNED
                if owned
                else FactSubjectScopeV2.VISIBLE_FOREIGN
            )
            for key in city_keys:
                value_key = "owner" if key == "owner_id" and "owner" in city else key
                if value_key not in city:
                    continue
                predicate = key
                value = city[value_key]
                if not owned and key == "hp":
                    predicate = "hp_bucket"
                    value = int(value) // 25
                if not owned and predicate not in {
                    "coord",
                    "hp_bucket",
                    "name",
                    "owner_id",
                    "population",
                }:
                    continue
                facts.append(_fact(city_ref, scope, predicate, value, turn))

        for coord, tile in sorted(visible_map["tiles"].items()):
            tile_ref = _entity_ref(EntityTypeV2.TILE, f"tile:{coord}")
            direct = "owner_id" in tile
            source = FactSourceV2.DIRECT if direct else FactSourceV2.REMEMBERED
            for key in ("coord", "terrain", "owner_id", "city_id"):
                if key in tile:
                    facts.append(
                        _fact(
                            tile_ref,
                            FactSubjectScopeV2.PUBLIC,
                            key,
                            tile[key],
                            turn,
                            source=source,
                        )
                    )

        for tech in research:
            tech_ref = _entity_ref(EntityTypeV2.TECHNOLOGY, tech["tech_id"])
            facts.extend(
                [
                    _fact(tech_ref, FactSubjectScopeV2.OWNED, "available", True, turn),
                    _fact(tech_ref, FactSubjectScopeV2.OWNED, "cost", tech["cost"], turn),
                    _fact(
                        tech_ref,
                        FactSubjectScopeV2.OWNED,
                        "prereq",
                        tech.get("prereq", []),
                        turn,
                    ),
                ]
            )
        for city_id, entries in sorted(production.items()):
            for item in entries:
                item_ref = _entity_ref(
                    EntityTypeV2.PRODUCTION_ITEM,
                    f"{city_id}:{item['item_id']}",
                )
                facts.extend(
                    [
                        _fact(item_ref, FactSubjectScopeV2.OWNED, "available", True, turn),
                        _fact(item_ref, FactSubjectScopeV2.OWNED, "city_id", city_id, turn),
                        _fact(item_ref, FactSubjectScopeV2.OWNED, "cost", item["cost"], turn),
                        _fact(item_ref, FactSubjectScopeV2.OWNED, "kind", item["kind"], turn),
                    ]
                )
                if type(item.get("turns")) is int and item["turns"] >= 0:
                    facts.append(
                        _fact(
                            item_ref,
                            FactSubjectScopeV2.OWNED,
                            "turns_remaining",
                            item["turns"],
                            turn,
                        )
                    )
                purchase_cost = item.get("purchase_cost")
                if purchase_cost is None and (
                    self.descriptor.adapter_kind is AdapterKindV2.FAKE
                ):
                    purchase_cost = int(item["cost"]) * 2
                if type(purchase_cost) is int and purchase_cost >= 0:
                    facts.append(
                        _fact(
                            item_ref,
                            FactSubjectScopeV2.OWNED,
                            "purchase_cost",
                            purchase_cost,
                            turn,
                        )
                    )
        return facts

    async def execute_authorized(
        self,
        authorization: AuthorizationV2,
        action: LegalActionV2,
    ) -> EnvironmentExecutionV2:
        if authorization.decision is not AuthorizationDecisionV2.AUTHORIZED:
            raise ContractError("environment received a refused authorization")
        if authorization.action_id != action.action_id:
            raise ContractError("authorization does not name the supplied action")
        player_id = self.observation_owners.get(action.observation_id)
        if player_id is None:
            raise ContractError("action is not bound to an observed player turn")
        if player_id not in self.last_observations:
            raise ContractError("stale observation authorization refused before adapter")
        current = self.last_observations[player_id]
        if (
            authorization.observation_id != current.observation_id
            or action.observation_id != current.observation_id
        ):
            raise ContractError("stale observation authorization refused before adapter")
        if (
            action.action_kind is ActionKindV2.END_TURN
            and current.mandatory_action_kinds
        ):
            return EnvironmentExecutionV2(
                ActionStatusV2.REJECTED,
                rejection_code="mandatory_unresolved",
                safe_message="end_turn requires all mandatory decisions",
            )
        if action.action_kind is ActionKindV2.END_TURN:
            try:
                info = await self.adapter.end_phase(player_id, current.turn)
                self.authorized_mutations.extend(
                    MutationRecord.from_doc(item) for item in info.get("manifest", [])
                )
            except Exception:
                self.last_observations.pop(player_id, None)
                return EnvironmentExecutionV2(
                    ActionStatusV2.DIVERGED,
                    rejection_code="adapter_failure",
                    safe_message="end_turn outcome could not be verified",
                )
            else:
                self.last_observations.pop(player_id, None)
                return EnvironmentExecutionV2(ActionStatusV2.ACCEPTED)

        try:
            result = await self.adapter.act(
                ActionCommand(
                    tool=action.action_kind.value,
                    args=dict(action.parameters),
                    player_id=player_id,
                    idempotency_key=authorization.authorization_id,
                    lease_id=f"v2:{authorization.authorization_id}",
                )
            )
        except Exception:
            self.last_observations.pop(player_id, None)
            return EnvironmentExecutionV2(
                ActionStatusV2.DIVERGED,
                rejection_code="adapter_failure",
                safe_message="action outcome could not be verified",
            )
        if result.status in {"accepted", "duplicate"}:
            if result.status == "accepted":
                self.authorized_mutations.extend(result.mutations)
            # No second authorization bound to this observation can reach the
            # adapter. The executor must re-observe and recompile first.
            self.last_observations.pop(player_id, None)
            return EnvironmentExecutionV2(ActionStatusV2(result.status))
        return EnvironmentExecutionV2(
            ActionStatusV2.REJECTED,
            rejection_code="adapter_rejected",
            safe_message=f"{action.action_kind.value} rejected",
        )


class _ObservableFacet:
    __slots__ = ("__begin_turn", "__descriptor", "__execute", "__observe", "__reset")

    def __init__(self, core: _AdapterCoreV2) -> None:
        self.__descriptor = core.descriptor
        self.__reset = core.reset
        self.__begin_turn = core.begin_turn
        self.__observe = core.observe
        self.__execute = core.execute_authorized

    @property
    def descriptor(self) -> EnvironmentDescriptorV2:
        return self.__descriptor

    async def reset(self, config: Mapping[str, Any]) -> None:
        await self.__reset(config)

    async def begin_turn(self, player_id: int, turn: int) -> ObservationV2:
        return await self.__begin_turn(player_id, turn)

    async def observe(self, player_id: int) -> ObservationV2:
        return await self.__observe(player_id)

    async def execute_authorized(
        self, authorization: AuthorizationV2, action: LegalActionV2
    ) -> EnvironmentExecutionV2:
        return await self.__execute(authorization, action)

    def __dir__(self) -> list[str]:
        return [
            "begin_turn",
            "descriptor",
            "execute_authorized",
            "observe",
            "reset",
        ]


class _PrivateMonitorFacet:
    __slots__ = (
        "__drain",
        "__drain_authorized",
        "__restore",
        "__snapshot",
        "__state_hash",
    )

    def __init__(self, core: _AdapterCoreV2) -> None:
        self.__snapshot = core.adapter.snapshot
        self.__restore = core.adapter.restore
        self.__state_hash = core.adapter.state_hash
        self.__drain = core.adapter.drain_mutations
        self.__drain_authorized = self._authorized_drainer(core)

    @staticmethod
    def _authorized_drainer(core: _AdapterCoreV2) -> Any:
        def drain() -> list[MutationRecord]:
            out = list(core.authorized_mutations)
            core.authorized_mutations.clear()
            return out

        return drain

    def snapshot(self) -> Any:
        return self.__snapshot()

    def restore(self, snapshot: Any) -> None:
        self.__restore(snapshot)

    def state_hash(self) -> str:
        return self.__state_hash()

    def drain_mutations(self) -> list[Any]:
        return self.__drain()

    def drain_authorized_mutations(self) -> list[Any]:
        return self.__drain_authorized()


def split_adapter_v2(
    adapter: Any,
    descriptor: EnvironmentDescriptorV2,
    *,
    visibility: VisibilityPolicy | None = None,
) -> tuple[ObservableExecutionFacetV2, PrivateRefereeMonitorV2]:
    required = {
        EnvironmentCapabilityV2.ACTION_EXECUTION,
        EnvironmentCapabilityV2.PLAYER_OBSERVATION,
        EnvironmentCapabilityV2.PRIVATE_REFEREE_MONITOR,
    }
    if not required.issubset(descriptor.capabilities):
        raise ContractError("V2 adapter descriptor lacks required environment capabilities")
    core = _AdapterCoreV2(adapter, descriptor, visibility or VisibilityPolicy())
    return _ObservableFacet(core), _PrivateMonitorFacet(core)


def simulator_facets_v2() -> tuple[ObservableExecutionFacetV2, PrivateRefereeMonitorV2]:
    descriptor = EnvironmentDescriptorV2.create(
        adapter_kind=AdapterKindV2.FAKE,
        adapter_version="2.0",
        game_version=FAKE_GAME_VERSION,
        ruleset_digest=FAKE_RULESET_DIGEST,
        mod_digest=NO_MOD_DIGEST,
        capabilities=[
            EnvironmentCapabilityV2.ACTION_EXECUTION,
            EnvironmentCapabilityV2.DETERMINISTIC_FAKE_REPLAY,
            EnvironmentCapabilityV2.PLAYER_OBSERVATION,
            EnvironmentCapabilityV2.PRIVATE_REFEREE_MONITOR,
            EnvironmentCapabilityV2.SAVE_LOAD_RESEARCH_ONLY,
            EnvironmentCapabilityV2.TURN_EVENTS,
        ],
        deterministic=True,
    )
    return split_adapter_v2(SimulatorAdapter(), descriptor)


def firetuner_facets_v2(
    adapter: Any,
    *,
    adapter_version: str,
    game_version: str,
    ruleset_digest: str,
    mod_digest: str,
) -> tuple[ObservableExecutionFacetV2, PrivateRefereeMonitorV2]:
    """Bind a verified live adapter identity without probing or hiding defaults.

    The caller obtains these identities during the live handshake. Supplying
    them explicitly prevents a local repository constant from masquerading as
    live environment proof.
    """

    descriptor = EnvironmentDescriptorV2.create(
        adapter_kind=AdapterKindV2.FIRETUNER,
        adapter_version=adapter_version,
        game_version=game_version,
        ruleset_digest=ruleset_digest,
        mod_digest=mod_digest,
        capabilities=[
            EnvironmentCapabilityV2.ACTION_EXECUTION,
            EnvironmentCapabilityV2.LIVE_OBSERVATIONAL_REPLAY,
            EnvironmentCapabilityV2.PLAYER_OBSERVATION,
            EnvironmentCapabilityV2.PRIVATE_REFEREE_MONITOR,
            EnvironmentCapabilityV2.TURN_EVENTS,
        ],
        deterministic=False,
    )
    return split_adapter_v2(adapter, descriptor)
