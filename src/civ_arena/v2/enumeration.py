"""Complete legal-action enumeration from player-scoped ObservationV2 only."""

from __future__ import annotations

import heapq
from dataclasses import dataclass
from typing import Any

from civ_arena.game.sim.state import (
    TERRAIN,
    hex_dist,
    neighbors,
    parse_key,
    tile_key,
    tiles_within,
)
from civ_arena.v2.contracts import (
    ActionKindV2,
    EffectClaimV2,
    EffectKindV2,
    EntityRefV2,
    EntityTypeV2,
    FactSourceV2,
    FactSubjectScopeV2,
    KnowledgeStateV2,
    KnowledgeValueV2,
    LegalActionSetV2,
    LegalActionV2,
    ObservationPhaseV2,
    ObservationV2,
    PreconditionClaimV2,
    PreconditionOperatorV2,
    ResourceClaimV2,
    ResourceKindV2,
    ResourceModeV2,
)
from civ_arena.v2.schemas import ContractError


class GraphOverflowError(ContractError):
    """The complete legal set exceeds the configured closed-world bound."""


@dataclass(frozen=True)
class _FactEntry:
    value: Any
    source: FactSourceV2
    scope: FactSubjectScopeV2


class ObservationIndexV2:
    """Read-only index derived exclusively from an ObservationV2."""

    def __init__(self, observation: ObservationV2) -> None:
        self.observation = observation
        self._values: dict[tuple[EntityTypeV2, str], dict[str, _FactEntry]] = {}
        for fact in observation.facts:
            if fact.value.state is not KnowledgeStateV2.KNOWN:
                continue
            key = (fact.subject.entity_type, fact.subject.entity_id)
            predicates = self._values.setdefault(key, {})
            if fact.predicate in predicates:
                raise ContractError("observation contains duplicate subject predicate")
            predicates[fact.predicate] = _FactEntry(
                fact.value.value,
                fact.source,
                fact.subject_scope,
            )

    def entities(self, entity_type: EntityTypeV2) -> tuple[EntityRefV2, ...]:
        return tuple(
            EntityRefV2(kind, entity_id)
            for kind, entity_id in sorted(self._values)
            if kind is entity_type
        )

    def entry(self, ref: EntityRefV2, predicate: str) -> _FactEntry | None:
        return self._values.get((ref.entity_type, ref.entity_id), {}).get(predicate)

    def get(self, ref: EntityRefV2, predicate: str, default: Any = None) -> Any:
        entry = self.entry(ref, predicate)
        return default if entry is None else entry.value

    def has_entity(self, ref: EntityRefV2) -> bool:
        return (ref.entity_type, ref.entity_id) in self._values

    def scope(self, ref: EntityRefV2) -> FactSubjectScopeV2 | None:
        values = self._values.get((ref.entity_type, ref.entity_id), {})
        return next(iter(values.values())).scope if values else None

    def facts_for(self, ref: EntityRefV2) -> dict[str, Any]:
        """Return a detached known-fact view for one observable entity."""

        return {
            predicate: entry.value
            for predicate, entry in self._values.get(
                (ref.entity_type, ref.entity_id), {}
            ).items()
        }


def _known(value: Any, turn: int) -> KnowledgeValueV2:
    if isinstance(value, list):
        value = tuple(value)
    return KnowledgeValueV2.known(value, observed_turn=turn)


def _precondition(
    subject: EntityRefV2,
    predicate: str,
    expected: Any,
    turn: int,
    operator: PreconditionOperatorV2 = PreconditionOperatorV2.EQ,
) -> PreconditionClaimV2:
    return PreconditionClaimV2(
        subject=subject,
        predicate=predicate,
        operator=operator,
        expected=_known(expected, turn),
    )


def _resource(
    kind: ResourceKindV2,
    owner: EntityRefV2,
    amount: int,
    mode: ResourceModeV2,
    region: str,
) -> ResourceClaimV2:
    return ResourceClaimV2(kind, owner, amount, mode, region)


def _effect(
    kind: EffectKindV2,
    subject: EntityRefV2,
    attribute: str,
    expected: KnowledgeValueV2,
    region: str,
) -> EffectClaimV2:
    return EffectClaimV2(kind, subject, attribute, expected, region)


def _visible_tiles(index: ObservationIndexV2) -> dict[str, str]:
    out: dict[str, str] = {}
    for ref in index.entities(EntityTypeV2.TILE):
        terrain = index.entry(ref, "terrain")
        coord = index.get(ref, "coord")
        if (
            terrain is not None
            and terrain.source is FactSourceV2.DIRECT
            and isinstance(coord, str)
            and isinstance(terrain.value, str)
        ):
            out[coord] = terrain.value
    return out


def _reachable(
    start: str,
    movement: int,
    terrain: dict[str, str],
    blocked: set[str],
) -> dict[str, int]:
    start_coord = parse_key(start)
    distances = {start: 0}
    heap: list[tuple[int, int, int]] = [(0, *start_coord)]
    while heap:
        cost, q, r = heapq.heappop(heap)
        if cost > distances.get(tile_key(q, r), 1 << 30):
            continue
        for nq, nr in neighbors(q, r):
            coord = tile_key(nq, nr)
            terrain_kind = terrain.get(coord)
            if terrain_kind is None or coord in blocked:
                continue
            terrain_spec = TERRAIN.get(terrain_kind)
            if terrain_spec is None:
                continue
            step = terrain_spec["move"]
            if step == 0:
                continue
            candidate = cost + step
            if candidate <= movement and candidate < distances.get(coord, 1 << 30):
                distances[coord] = candidate
                heapq.heappush(heap, (candidate, nq, nr))
    if start in blocked:
        distances.pop(start, None)
    return distances


class ActionEnumeratorV2:
    """Enumerate the complete conservative legal set without referee state."""

    def __init__(self, max_graph_actions: int = 1024) -> None:
        if max_graph_actions < 1:
            raise ValueError("max_graph_actions must be positive")
        self.max_graph_actions = max_graph_actions

    def enumerate(self, observation: ObservationV2) -> LegalActionSetV2:
        if observation.phase is not ObservationPhaseV2.TURN:
            raise ContractError("legal actions require a current turn observation")
        if observation.active_player != observation.observing_player:
            raise ContractError("legal actions require the observing player's active phase")
        index = ObservationIndexV2(observation)
        actions: list[LegalActionV2] = []
        actions.extend(self._system_actions(observation, index))
        actions.extend(self._unit_actions(observation, index))
        actions.extend(self._research_actions(observation, index))
        actions.extend(self._city_actions(observation, index))
        if not observation.mandatory_action_kinds:
            player = observation.observing_player
            region = f"player:{player.entity_id}:turn"
            actions.append(
                LegalActionV2.create(
                    ActionKindV2.END_TURN,
                    player,
                    observation.observation_id,
                    preconditions=[
                        _precondition(
                            player,
                            "mandatory_resolved",
                            True,
                            observation.turn,
                        )
                    ],
                    resource_claims=[
                        _resource(
                            ResourceKindV2.TURN,
                            player,
                            1,
                            ResourceModeV2.CONSUME,
                            region,
                        )
                    ],
                    expected_effects=[
                        _effect(
                            EffectKindV2.TERMINAL,
                            player,
                            "turn",
                            KnowledgeValueV2.unknown(
                                "next turn is environment-controlled",
                                observed_turn=observation.turn,
                            ),
                            region,
                        )
                    ],
                    affected_regions=[region],
                    terminal=True,
                )
            )
        if len(actions) > self.max_graph_actions:
            raise GraphOverflowError(
                f"complete legal set has {len(actions)} actions; "
                f"configured maximum is {self.max_graph_actions}"
            )
        return LegalActionSetV2.create(observation.observation_id, actions)

    @staticmethod
    def _system_actions(
        observation: ObservationV2,
        index: ObservationIndexV2,
    ) -> list[LegalActionV2]:
        """Enumerate player-visible live blockers as typed system actions.

        These actions are absent from simulator observations.  A live
        coordinator may propose them through its registered system policy, but
        they remain ordinary observation-bound graph nodes and receive no
        special mutation authority.
        """

        player = observation.observing_player
        raw = index.get(player, "turn_blockers", ())
        blockers = tuple(raw) if isinstance(raw, tuple | list) else ()
        kinds = {
            "civic_choice": ActionKindV2.RESOLVE_CIVIC,
            "policy_slots": ActionKindV2.FILL_POLICY_SLOTS,
        }
        actions: list[LegalActionV2] = []
        for blocker, action_kind in kinds.items():
            if blocker not in blockers:
                continue
            region = f"player:{player.entity_id}:{blocker}"
            actions.append(
                LegalActionV2.create(
                    action_kind,
                    player,
                    observation.observation_id,
                    preconditions=[
                        _precondition(
                            player,
                            "turn_blockers",
                            blocker,
                            observation.turn,
                            PreconditionOperatorV2.CONTAINS,
                        )
                    ],
                    expected_effects=[
                        _effect(
                            EffectKindV2.SET,
                            player,
                            "turn_blockers",
                            KnowledgeValueV2.unknown(
                                "live blocker set is environment-controlled",
                                observed_turn=observation.turn,
                            ),
                            region,
                        )
                    ],
                    affected_regions=[region],
                    mandatory=True,
                )
            )
        return actions

    def _unit_actions(
        self,
        observation: ObservationV2,
        index: ObservationIndexV2,
    ) -> list[LegalActionV2]:
        actions: list[LegalActionV2] = []
        terrain = _visible_tiles(index)
        foreign_units = [
            ref
            for ref in index.entities(EntityTypeV2.UNIT)
            if index.scope(ref) is FactSubjectScopeV2.VISIBLE_FOREIGN
        ]
        blocked = {
            coord
            for ref in foreign_units
            if isinstance((coord := index.get(ref, "coord")), str)
        }
        owned_units = [
            ref
            for ref in index.entities(EntityTypeV2.UNIT)
            if index.scope(ref) is FactSubjectScopeV2.OWNED
        ]
        for unit in owned_units:
            coord = index.get(unit, "coord")
            movement = index.get(unit, "movement", 0)
            unit_type = index.get(unit, "type")
            strength = index.get(unit, "strength", 0)
            ranged_strength = index.get(unit, "ranged_strength", 0)
            fortified = index.get(unit, "fortified", False)
            if not isinstance(coord, str) or type(movement) is not int:
                continue
            movement_region = f"unit:{unit.entity_id}:movement"
            unit_region = f"unit:{unit.entity_id}"
            if movement > 0:
                for dest, cost in sorted(
                    _reachable(coord, movement, terrain, blocked).items()
                ):
                    actions.append(
                        LegalActionV2.create(
                            ActionKindV2.MOVE_UNIT,
                            unit,
                            observation.observation_id,
                            parameters={"unit_id": unit.entity_id, "dest": dest},
                            preconditions=[
                                _precondition(
                                    unit,
                                    "movement",
                                    cost,
                                    observation.turn,
                                    PreconditionOperatorV2.GTE,
                                ),
                                _precondition(
                                    unit,
                                    "owner_id",
                                    self._player_id(observation),
                                    observation.turn,
                                ),
                            ],
                            resource_claims=[
                                _resource(
                                    ResourceKindV2.MOVEMENT,
                                    unit,
                                    cost,
                                    ResourceModeV2.CONSUME,
                                    movement_region,
                                )
                            ],
                            expected_effects=[
                                _effect(
                                    EffectKindV2.MOVE,
                                    unit,
                                    "coord",
                                    _known(dest, observation.turn),
                                    unit_region,
                                ),
                                _effect(
                                    EffectKindV2.SET,
                                    unit,
                                    "movement",
                                    (
                                        _known(movement - cost, observation.turn)
                                        if observation.game_version
                                        == "civ-arena-sim-v2"
                                        else KnowledgeValueV2.unknown(
                                            "remaining movement is environment-controlled",
                                            observed_turn=observation.turn,
                                        )
                                    ),
                                    movement_region,
                                ),
                            ],
                            affected_regions=[unit_region, movement_region, f"tile:{dest}"],
                        )
                    )
            if movement > 0 and (strength > 0 or ranged_strength > 0):
                for target in foreign_units:
                    target_coord = index.get(target, "coord")
                    if not isinstance(target_coord, str):
                        continue
                    distance = hex_dist(parse_key(coord), parse_key(target_coord))
                    in_range = distance <= 2 if ranged_strength > 0 else distance == 1
                    if not in_range:
                        continue
                    actions.append(
                        LegalActionV2.create(
                            ActionKindV2.ATTACK,
                            unit,
                            observation.observation_id,
                            target=target,
                            parameters={
                                "unit_id": unit.entity_id,
                                "target_id": target.entity_id,
                            },
                            preconditions=[
                                _precondition(
                                    unit,
                                    "movement",
                                    1,
                                    observation.turn,
                                    PreconditionOperatorV2.GTE,
                                ),
                                _precondition(target, "target_exists", True, observation.turn),
                                _precondition(target, "range", distance, observation.turn),
                            ],
                            resource_claims=[
                                _resource(
                                    ResourceKindV2.MOVEMENT,
                                    unit,
                                    movement,
                                    ResourceModeV2.CONSUME,
                                    movement_region,
                                )
                            ],
                            expected_effects=[
                                _effect(
                                    EffectKindV2.DAMAGE,
                                    target,
                                    "hp",
                                    KnowledgeValueV2.unknown(
                                        "combat resolution is stochastic",
                                        observed_turn=observation.turn,
                                    ),
                                    f"unit:{target.entity_id}",
                                ),
                                _effect(
                                    EffectKindV2.SET,
                                    unit,
                                    "movement",
                                    _known(0, observation.turn),
                                    movement_region,
                                ),
                            ],
                            affected_regions=[
                                unit_region,
                                movement_region,
                                f"unit:{target.entity_id}",
                            ],
                        )
                    )
            if fortified is False:
                actions.append(
                    LegalActionV2.create(
                        ActionKindV2.FORTIFY,
                        unit,
                        observation.observation_id,
                        parameters={"unit_id": unit.entity_id},
                        preconditions=[
                            _precondition(unit, "available", True, observation.turn),
                            _precondition(unit, "fortified", False, observation.turn),
                        ],
                        resource_claims=[
                            _resource(
                                ResourceKindV2.UNIT,
                                unit,
                                1,
                                ResourceModeV2.RESERVE,
                                unit_region,
                            )
                        ],
                        expected_effects=[
                            _effect(
                                EffectKindV2.SET,
                                unit,
                                "fortified",
                                _known(True, observation.turn),
                                unit_region,
                            )
                        ],
                        affected_regions=[unit_region],
                    )
                )
            if unit_type == "SETTLER" and self._can_found(index, coord, foreign_units):
                claimed_regions = [
                    f"tile:{tile_key(q, r)}"
                    for q, r in tiles_within(parse_key(coord), 2)
                    if EntityRefV2(EntityTypeV2.TILE, f"tile:{tile_key(q, r)}")
                    in index.entities(EntityTypeV2.TILE)
                ]
                actions.append(
                    LegalActionV2.create(
                        ActionKindV2.FOUND_CITY,
                        unit,
                        observation.observation_id,
                        parameters={
                            "unit_id": unit.entity_id,
                            "name": f"Arena-{unit.entity_id}",
                        },
                        preconditions=[
                            _precondition(unit, "available", True, observation.turn),
                            _precondition(unit, "type", "SETTLER", observation.turn),
                            _precondition(
                                EntityRefV2(EntityTypeV2.TILE, f"tile:{coord}"),
                                "owner_id",
                                -1,
                                observation.turn,
                            ),
                        ],
                        resource_claims=[
                            _resource(
                                ResourceKindV2.UNIT,
                                unit,
                                1,
                                ResourceModeV2.CONSUME,
                                unit_region,
                            )
                        ],
                        expected_effects=[
                            _effect(
                                EffectKindV2.DELETE,
                                unit,
                                "status",
                                _known("consumed", observation.turn),
                                unit_region,
                            )
                        ],
                        affected_regions=[unit_region, *claimed_regions],
                    )
                )
        return actions

    def _can_found(
        self,
        index: ObservationIndexV2,
        coord: str,
        foreign_units: list[EntityRefV2],
    ) -> bool:
        tile = EntityRefV2(EntityTypeV2.TILE, f"tile:{coord}")
        if index.get(tile, "owner_id") != -1:
            return False
        terrain = index.get(tile, "terrain")
        if (
            not isinstance(terrain, str)
            or terrain not in TERRAIN
            or TERRAIN[terrain]["move"] == 0
        ):
            return False
        for city in index.entities(EntityTypeV2.CITY):
            city_coord = index.get(city, "coord")
            if (
                isinstance(city_coord, str)
                and hex_dist(parse_key(city_coord), parse_key(coord)) <= 2
            ):
                return False
        return all(index.get(unit, "coord") != coord for unit in foreign_units)

    def _research_actions(
        self,
        observation: ObservationV2,
        index: ObservationIndexV2,
    ) -> list[LegalActionV2]:
        actions: list[LegalActionV2] = []
        player = observation.observing_player
        current = index.get(player, "researching")
        mandatory = ActionKindV2.SET_RESEARCH in observation.mandatory_action_kinds
        region = f"player:{player.entity_id}:research"
        for tech in index.entities(EntityTypeV2.TECHNOLOGY):
            if index.get(tech, "available") is not True or tech.entity_id == current:
                continue
            actions.append(
                LegalActionV2.create(
                    ActionKindV2.SET_RESEARCH,
                    player,
                    observation.observation_id,
                    target=tech,
                    parameters={"tech_id": tech.entity_id},
                    preconditions=[
                        _precondition(tech, "available", True, observation.turn)
                    ],
                    resource_claims=[
                        _resource(
                            ResourceKindV2.RESEARCH_SLOT,
                            player,
                            1,
                            ResourceModeV2.RESERVE,
                            region,
                        )
                    ],
                    expected_effects=[
                        _effect(
                            EffectKindV2.SET,
                            player,
                            "researching",
                            _known(tech.entity_id, observation.turn),
                            region,
                        )
                    ],
                    affected_regions=[region],
                    mandatory=mandatory,
                )
            )
        return actions

    def _city_actions(
        self,
        observation: ObservationV2,
        index: ObservationIndexV2,
    ) -> list[LegalActionV2]:
        actions: list[LegalActionV2] = []
        player = observation.observing_player
        gold = index.get(player, "gold", 0)
        if type(gold) is not int:
            gold = 0
        mandatory_kind = ActionKindV2.SET_CITY_PRODUCTION in observation.mandatory_action_kinds
        owned_cities = {
            ref.entity_id: ref
            for ref in index.entities(EntityTypeV2.CITY)
            if index.scope(ref) is FactSubjectScopeV2.OWNED
        }
        for item in index.entities(EntityTypeV2.PRODUCTION_ITEM):
            if index.get(item, "available") is not True:
                continue
            city_id = index.get(item, "city_id")
            if city_id not in owned_cities:
                continue
            city = owned_cities[city_id]
            item_id = item.entity_id.split(":", 1)[1]
            cost = index.get(item, "cost")
            if type(cost) is not int:
                continue
            production_region = f"city:{city_id}:production"
            queue = index.get(city, "production_queue", ())
            is_mandatory = mandatory_kind and not queue
            actions.append(
                LegalActionV2.create(
                    ActionKindV2.SET_CITY_PRODUCTION,
                    city,
                    observation.observation_id,
                    target=item,
                    parameters={"city_id": city_id, "item_id": item_id},
                    preconditions=[
                        _precondition(item, "available", True, observation.turn)
                    ],
                    resource_claims=[
                        _resource(
                            ResourceKindV2.PRODUCTION_SLOT,
                            city,
                            1,
                            ResourceModeV2.RESERVE,
                            production_region,
                        )
                    ],
                    expected_effects=[
                        _effect(
                            EffectKindV2.SET,
                            city,
                            "production_queue",
                            _known((item_id,), observation.turn),
                            production_region,
                        )
                    ],
                    affected_regions=[production_region],
                    mandatory=is_mandatory,
                )
            )
            purchase_cost = index.get(item, "purchase_cost")
            if type(purchase_cost) is not int or gold < purchase_cost:
                continue
            gold_region = f"player:{player.entity_id}:gold"
            actions.append(
                LegalActionV2.create(
                    ActionKindV2.PURCHASE,
                    city,
                    observation.observation_id,
                    target=item,
                    parameters={"city_id": city_id, "item_id": item_id},
                    preconditions=[
                        _precondition(
                            player,
                            "gold",
                            purchase_cost,
                            observation.turn,
                            PreconditionOperatorV2.GTE,
                        ),
                        _precondition(item, "available", True, observation.turn),
                    ],
                    resource_claims=[
                        _resource(
                            ResourceKindV2.GOLD,
                            player,
                            purchase_cost,
                            ResourceModeV2.CONSUME,
                            gold_region,
                        )
                    ],
                    expected_effects=[
                        _effect(
                            EffectKindV2.SET,
                            player,
                            "gold",
                            _known(gold - purchase_cost, observation.turn),
                            gold_region,
                        ),
                        _effect(
                            EffectKindV2.CREATE,
                            item,
                            "created",
                            KnowledgeValueV2.unknown(
                                "created entity identity is assigned by environment",
                                observed_turn=observation.turn,
                            ),
                            f"city:{city_id}",
                        ),
                    ],
                    affected_regions=[gold_region, f"city:{city_id}"],
                )
            )
        return actions

    @staticmethod
    def _player_id(observation: ObservationV2) -> int:
        return int(observation.observing_player.entity_id[1:])
