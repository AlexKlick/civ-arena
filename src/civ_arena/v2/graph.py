"""Observable-only V2 legal-action graph compiler and exact canonical kernel.

The compiler consumes a complete :class:`ObservationV2` and its bound
``LegalActionSetV2``.  It neither executes actions nor consults an adapter.
Every unordered action pair receives either one or more explicit constraints,
or a single derived ``COMMUTES_WITH`` witness.  Missing proof fails closed as
``CONFLICTS_WITH``; absence of an edge is never an independence claim.

``canonical_action_order`` operates on a selected subset of graph nodes.  It
refuses prohibited co-selections and unsatisfied ``REQUIRES`` edges, then uses
stable action ids for every topological frontier.  Proposal/enumeration order
is deliberately irrelevant.
"""

from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from civ_arena.game.sim.state import TERRAIN, hex_dist, parse_key
from civ_arena.v2.contracts import (
    EDGE_KINDS_IN_ORDER,
    ActionGraphEdgeV2,
    ActionGraphMetricsV2,
    ActionGraphNodeV2,
    ActionGraphV2,
    ActionKindV2,
    EdgeAuthorityV2,
    EdgeKindV2,
    EdgeReasonV2,
    EffectClaimV2,
    EffectKindV2,
    EntityRefV2,
    EntityTypeV2,
    KnowledgeStateV2,
    LegalActionSetV2,
    LegalActionV2,
    ObservationV2,
    PreconditionClaimV2,
    PreconditionOperatorV2,
    ResourceClaimV2,
    ResourceKindV2,
    ResourceModeV2,
)
from civ_arena.v2.enumeration import ActionEnumeratorV2, ObservationIndexV2
from civ_arena.v2.schemas import ContractError

PROHIBITIVE_EDGE_KINDS = frozenset(
    {
        EdgeKindV2.CONFLICTS_WITH,
        EdgeKindV2.MUTEX,
        EdgeKindV2.CONSUMES_SHARED_RESOURCE,
    }
)

# Exact compatible-subset counting is exponential.  The result remains exact
# through the property-test range and normal hand-sized graphs.  Above the
# bound, the schema field carries a deterministic upper estimate and the
# metrics say so explicitly instead of pretending it is exact.
EXACT_PLAN_COUNT_MAX_ACTIONS = 20


class GraphSelectionError(ContractError):
    """A selected action subset violates its compiled graph."""


@dataclass(frozen=True)
class _PairClassification:
    edges: tuple[ActionGraphEdgeV2, ...]
    unknown: bool = False


def _edge(
    source: LegalActionV2,
    target: LegalActionV2,
    kind: EdgeKindV2,
    reason: EdgeReasonV2,
    *,
    region: str | None = None,
) -> ActionGraphEdgeV2:
    authority = (
        EdgeAuthorityV2.DERIVED
        if kind is EdgeKindV2.COMMUTES_WITH
        else EdgeAuthorityV2.AUTHORITATIVE
    )
    return ActionGraphEdgeV2.create(
        source.action_id,
        target.action_id,
        kind,
        authority,
        reason,
        region=region,
    )


def _parameters(action: LegalActionV2) -> dict[str, Any]:
    return dict(action.parameters)


def _region_intersects(left: str, right: str) -> bool:
    return (
        left == right
        or left.startswith(right + ":")
        or right.startswith(left + ":")
    )


def _overlap_region(left: LegalActionV2, right: LegalActionV2) -> str | None:
    for left_region in left.affected_regions:
        for right_region in right.affected_regions:
            if _region_intersects(left_region, right_region):
                return min(left_region, right_region)
    return None


def _resource_key(claim: ResourceClaimV2) -> tuple[ResourceKindV2, EntityRefV2, str]:
    return claim.resource, claim.owner, claim.region


def _shared_resource_claims(
    left: LegalActionV2,
    right: LegalActionV2,
) -> tuple[tuple[ResourceClaimV2, ResourceClaimV2], ...]:
    pairs = [
        (left_claim, right_claim)
        for left_claim in left.resource_claims
        for right_claim in right.resource_claims
        if _resource_key(left_claim) == _resource_key(right_claim)
        and left_claim.mode is not ResourceModeV2.READ
        and right_claim.mode is not ResourceModeV2.READ
    ]
    return tuple(
        sorted(
            pairs,
            key=lambda pair: (
                pair[0].resource.value,
                pair[0].owner.entity_type.value,
                pair[0].owner.entity_id,
                pair[0].region,
            ),
        )
    )


def _resource_budget(
    observation_index: ObservationIndexV2,
    claim: ResourceClaimV2,
) -> int | None:
    if claim.resource is ResourceKindV2.GOLD:
        value = observation_index.get(claim.owner, "gold")
    elif claim.resource is ResourceKindV2.MOVEMENT:
        value = observation_index.get(claim.owner, "movement")
    elif claim.resource in {
        ResourceKindV2.PRODUCTION_SLOT,
        ResourceKindV2.RESEARCH_SLOT,
        ResourceKindV2.TURN,
        ResourceKindV2.UNIT,
    }:
        value = 1
    else:  # pragma: no cover - enum exhaustiveness guard
        return None
    return value if type(value) is int and value >= 0 else None


def _exclusive_slot(
    pairs: tuple[tuple[ResourceClaimV2, ResourceClaimV2], ...],
) -> ResourceClaimV2 | None:
    for left, right in pairs:
        if left.resource in {
            ResourceKindV2.PRODUCTION_SLOT,
            ResourceKindV2.RESEARCH_SLOT,
        } and (
            left.mode is ResourceModeV2.RESERVE
            or right.mode is ResourceModeV2.RESERVE
        ):
            return left
        if left.resource is ResourceKindV2.UNIT and (
            left.mode is ResourceModeV2.CONSUME
            or right.mode is ResourceModeV2.CONSUME
        ):
            return left
    return None


def _insufficient_resource(
    observation_index: ObservationIndexV2,
    pairs: tuple[tuple[ResourceClaimV2, ResourceClaimV2], ...],
) -> tuple[ResourceClaimV2 | None, bool]:
    """Return (conflicting claim, unknown budget).

    Multiple claims for one resource key are normalized by the enumerator, but
    summing by key here makes the compiler fail closed for independently
    constructed, schema-valid actions too.
    """

    for left, right in pairs:
        budget = _resource_budget(observation_index, left)
        if budget is None:
            return left, True
        if left.amount + right.amount > budget:
            return left, False
    return None, False


def _effect_key(effect: EffectClaimV2) -> tuple[EntityRefV2, str]:
    return effect.subject, effect.attribute


def _precondition_key(claim: PreconditionClaimV2) -> tuple[EntityRefV2, str]:
    return claim.subject, claim.predicate


def _known_satisfies(value: Any, claim: PreconditionClaimV2) -> bool:
    expected = claim.expected
    if expected.state is not KnowledgeStateV2.KNOWN:
        return False
    target = expected.value
    try:
        if claim.operator is PreconditionOperatorV2.EQ:
            return value == target
        if claim.operator is PreconditionOperatorV2.NE:
            return value != target
        if claim.operator is PreconditionOperatorV2.GTE:
            return type(value) is int and type(target) is int and value >= target
        if claim.operator is PreconditionOperatorV2.LTE:
            return type(value) is int and type(target) is int and value <= target
        if claim.operator is PreconditionOperatorV2.PRESENT:
            return value is not None
        if claim.operator is PreconditionOperatorV2.ABSENT:
            return value is None
        if claim.operator is PreconditionOperatorV2.CONTAINS:
            return isinstance(value, tuple | list | str) and target in value
    except TypeError:
        return False
    return False


def _observation_satisfies(
    index: ObservationIndexV2,
    claim: PreconditionClaimV2,
) -> bool:
    entry = index.entry(claim.subject, claim.predicate)
    return entry is not None and _known_satisfies(entry.value, claim)


def _effect_satisfies(effect: EffectClaimV2, claim: PreconditionClaimV2) -> bool:
    if _effect_key(effect) != _precondition_key(claim):
        return False
    if effect.effect_kind is EffectKindV2.DELETE:
        value: Any = None
    elif effect.expected.state is KnowledgeStateV2.KNOWN:
        value = effect.expected.value
    else:
        return False
    return _known_satisfies(value, claim)


def _dependency_edges(
    source: LegalActionV2,
    target: LegalActionV2,
    index: ObservationIndexV2,
) -> list[ActionGraphEdgeV2]:
    edges: list[ActionGraphEdgeV2] = []
    for effect in source.expected_effects:
        for precondition in target.preconditions:
            if _effect_key(effect) != _precondition_key(precondition):
                continue
            if _effect_satisfies(effect, precondition):
                if not _observation_satisfies(index, precondition):
                    edges.append(
                        _edge(
                            source,
                            target,
                            EdgeKindV2.REQUIRES,
                            EdgeReasonV2.PRECONDITION_DEPENDENCY,
                            region=effect.region,
                        )
                    )
            else:
                edges.append(
                    _edge(
                        source,
                        target,
                        EdgeKindV2.INVALIDATES,
                        EdgeReasonV2.MUTATION_INVALIDATES,
                        region=effect.region,
                    )
                )
    return edges


def _move_enables_attack(
    move: LegalActionV2,
    attack: LegalActionV2,
    index: ObservationIndexV2,
) -> bool:
    if move.actor != attack.actor:
        return False
    destination = _parameters(move).get("dest")
    target = attack.target
    if not isinstance(destination, str) or target is None:
        return False
    target_coord = index.get(target, "coord")
    ranged = index.get(move.actor, "ranged_strength", 0)
    if not isinstance(target_coord, str) or type(ranged) is not int:
        return False
    remaining = next(
        (
            effect.expected.value
            for effect in move.expected_effects
            if effect.subject == move.actor
            and effect.attribute == "movement"
            and effect.expected.state is KnowledgeStateV2.KNOWN
        ),
        None,
    )
    if type(remaining) is not int or remaining <= 0:
        return False
    distance = hex_dist(parse_key(destination), parse_key(target_coord))
    return distance <= 2 if ranged > 0 else distance == 1


def _destination_can_found(
    move: LegalActionV2,
    index: ObservationIndexV2,
) -> bool:
    destination = _parameters(move).get("dest")
    if not isinstance(destination, str):
        return False
    tile = EntityRefV2(EntityTypeV2.TILE, f"tile:{destination}")
    terrain = index.get(tile, "terrain")
    owner = index.get(tile, "owner_id")
    if owner != -1 or not isinstance(terrain, str):
        return False
    # The enumerator only emits moves through passable direct-visible tiles;
    # an absent terrain fact therefore cannot be treated as buildable proof.
    if terrain not in TERRAIN or TERRAIN[terrain]["move"] == 0:
        return False
    for city in index.entities(EntityTypeV2.CITY):
        coord = index.get(city, "coord")
        if isinstance(coord, str) and hex_dist(parse_key(coord), parse_key(destination)) <= 2:
            return False
    for unit in index.entities(EntityTypeV2.UNIT):
        if unit != move.actor and index.get(unit, "coord") == destination:
            return False
    return True


def _specific_sequence(
    left: LegalActionV2,
    right: LegalActionV2,
    index: ObservationIndexV2,
) -> _PairClassification | None:
    kinds = {left.action_kind, right.action_kind}

    if left.terminal != right.terminal:
        before, terminal = (right, left) if left.terminal else (left, right)
        return _PairClassification(
            (
                _edge(
                    before,
                    terminal,
                    EdgeKindV2.MUST_PRECEDE,
                    EdgeReasonV2.MUTATION_INVALIDATES,
                    region=terminal.affected_regions[0]
                    if terminal.affected_regions
                    else None,
                ),
            )
        )

    if kinds == {ActionKindV2.PURCHASE, ActionKindV2.SET_CITY_PRODUCTION}:
        purchase = left if left.action_kind is ActionKindV2.PURCHASE else right
        production = right if purchase is left else left
        if _parameters(purchase).get("city_id") == _parameters(production).get("city_id"):
            city_id = str(_parameters(purchase)["city_id"])
            return _PairClassification(
                (
                    _edge(
                        purchase,
                        production,
                        EdgeKindV2.MUST_PRECEDE,
                        EdgeReasonV2.MUTATION_INVALIDATES,
                        region=f"city:{city_id}",
                    ),
                    _edge(
                        purchase,
                        production,
                        EdgeKindV2.INVALIDATES,
                        EdgeReasonV2.MUTATION_INVALIDATES,
                        region=f"city:{city_id}",
                    ),
                )
            )

    if left.actor == right.actor and left.actor.entity_type is EntityTypeV2.UNIT:
        if kinds == {ActionKindV2.MOVE_UNIT, ActionKindV2.ATTACK}:
            move = left if left.action_kind is ActionKindV2.MOVE_UNIT else right
            attack = right if move is left else left
            if not _move_enables_attack(move, attack, index):
                return _PairClassification(
                    (
                        _edge(
                            left,
                            right,
                            EdgeKindV2.CONFLICTS_WITH,
                            EdgeReasonV2.OVERLAPPING_EFFECT,
                            region=f"unit:{left.actor.entity_id}",
                        ),
                    )
                )
            return _PairClassification(
                (
                    _edge(
                        move,
                        attack,
                        EdgeKindV2.MUST_PRECEDE,
                        EdgeReasonV2.SAME_ACTOR_SEQUENCE,
                        region=f"unit:{move.actor.entity_id}",
                    ),
                    _edge(
                        move,
                        attack,
                        EdgeKindV2.ENABLES,
                        EdgeReasonV2.EFFECT_ENABLES,
                        region=f"unit:{move.actor.entity_id}",
                    ),
                )
            )

        if kinds == {ActionKindV2.MOVE_UNIT, ActionKindV2.FOUND_CITY}:
            move = left if left.action_kind is ActionKindV2.MOVE_UNIT else right
            found = right if move is left else left
            if not _destination_can_found(move, index):
                return _PairClassification(
                    (
                        _edge(
                            left,
                            right,
                            EdgeKindV2.CONFLICTS_WITH,
                            EdgeReasonV2.OVERLAPPING_EFFECT,
                            region=f"unit:{left.actor.entity_id}",
                        ),
                    )
                )
            return _PairClassification(
                (
                    _edge(
                        move,
                        found,
                        EdgeKindV2.MUST_PRECEDE,
                        EdgeReasonV2.SAME_ACTOR_SEQUENCE,
                        region=f"unit:{move.actor.entity_id}",
                    ),
                    _edge(
                        move,
                        found,
                        EdgeKindV2.ENABLES,
                        EdgeReasonV2.EFFECT_ENABLES,
                        region=f"unit:{move.actor.entity_id}",
                    ),
                )
            )

        if ActionKindV2.FORTIFY in kinds:
            other = right if left.action_kind is ActionKindV2.FORTIFY else left
            fortify = left if left.action_kind is ActionKindV2.FORTIFY else right
            if other.action_kind in {
                ActionKindV2.MOVE_UNIT,
                ActionKindV2.ATTACK,
                ActionKindV2.FOUND_CITY,
            }:
                return _PairClassification(
                    (
                        _edge(
                            other,
                            fortify,
                            EdgeKindV2.MUST_PRECEDE,
                            EdgeReasonV2.SAME_ACTOR_SEQUENCE,
                            region=f"unit:{left.actor.entity_id}",
                        ),
                    )
                )

        # Two alternative current actions for one actor have no safe order
        # unless one of the explicit rules above proves it.
        return _PairClassification(
            (
                _edge(
                    left,
                    right,
                    EdgeKindV2.CONFLICTS_WITH,
                    EdgeReasonV2.OVERLAPPING_EFFECT,
                    region=f"{left.actor.entity_type.value}:{left.actor.entity_id}",
                ),
            )
        )

    if left.action_kind is right.action_kind is ActionKindV2.ATTACK and left.target == right.target:
        region = f"unit:{left.target.entity_id}" if left.target is not None else None
        return _PairClassification(
            (
                _edge(
                    left,
                    right,
                    EdgeKindV2.INVALIDATES,
                    EdgeReasonV2.MUTATION_INVALIDATES,
                    region=region,
                ),
                _edge(
                    right,
                    left,
                    EdgeKindV2.INVALIDATES,
                    EdgeReasonV2.MUTATION_INVALIDATES,
                    region=region,
                ),
            )
        )

    if left.action_kind is right.action_kind is ActionKindV2.FOUND_CITY:
        return _PairClassification(
            (
                _edge(
                    left,
                    right,
                    EdgeKindV2.INVALIDATES,
                    EdgeReasonV2.MUTATION_INVALIDATES,
                    region="system:city-identities",
                ),
                _edge(
                    right,
                    left,
                    EdgeKindV2.INVALIDATES,
                    EdgeReasonV2.MUTATION_INVALIDATES,
                    region="system:city-identities",
                ),
            )
        )
    return None


def _has_conflicting_effects(left: LegalActionV2, right: LegalActionV2) -> str | None:
    for left_effect in left.expected_effects:
        for right_effect in right.expected_effects:
            if _effect_key(left_effect) != _effect_key(right_effect):
                continue
            if (
                left_effect.effect_kind != right_effect.effect_kind
                or left_effect.expected != right_effect.expected
            ):
                return min(left_effect.region, right_effect.region)
    return None


def _proven_disjoint(left: LegalActionV2, right: LegalActionV2) -> bool:
    if not left.affected_regions or not right.affected_regions:
        return False
    if _overlap_region(left, right) is not None:
        return False
    if _shared_resource_claims(left, right):
        return False
    left_effects = {_effect_key(effect) for effect in left.expected_effects}
    right_effects = {_effect_key(effect) for effect in right.expected_effects}
    if not left_effects or not right_effects or left_effects & right_effects:
        return False
    left_preconditions = {_precondition_key(claim) for claim in left.preconditions}
    right_preconditions = {_precondition_key(claim) for claim in right.preconditions}
    if left_effects & right_preconditions or right_effects & left_preconditions:
        return False
    # Two stochastic combats consume one hidden RNG stream.  It is not an
    # observable resource claim, so the compiler must refuse to invent a
    # commute proof for the pair.
    return not (left.action_kind is right.action_kind is ActionKindV2.ATTACK)


def _dedupe_edges(edges: Iterable[ActionGraphEdgeV2]) -> tuple[ActionGraphEdgeV2, ...]:
    by_id = {edge.edge_id: edge for edge in edges}
    return tuple(sorted(by_id.values(), key=lambda edge: edge.edge_id))


def _classify_pair(
    left: LegalActionV2,
    right: LegalActionV2,
    index: ObservationIndexV2,
) -> _PairClassification:
    shared = _shared_resource_claims(left, right)
    exclusive = _exclusive_slot(shared)
    if exclusive is not None:
        return _PairClassification(
            (
                _edge(
                    left,
                    right,
                    EdgeKindV2.MUTEX,
                    EdgeReasonV2.EXCLUSIVE_CHOICE,
                    region=exclusive.region,
                ),
            )
        )

    specific = _specific_sequence(left, right, index)
    if specific is not None:
        return specific

    insufficient, unknown_budget = _insufficient_resource(index, shared)
    if insufficient is not None:
        kind = (
            EdgeKindV2.CONFLICTS_WITH
            if unknown_budget
            else EdgeKindV2.CONSUMES_SHARED_RESOURCE
        )
        reason = (
            EdgeReasonV2.UNKNOWN_RELATIONSHIP
            if unknown_budget
            else EdgeReasonV2.INSUFFICIENT_SHARED_RESOURCE
        )
        return _PairClassification(
            (_edge(left, right, kind, reason, region=insufficient.region),),
            unknown=unknown_budget,
        )

    if shared:
        # The combined claim fits, but executing either mutation changes the
        # basis of the other's current authorization and expected effect.
        # This semantic invalidation dominates an apparent overlapping-effect
        # conflict (notably for two affordable gold purchases).
        region = shared[0][0].region
        return _PairClassification(
            (
                _edge(
                    left,
                    right,
                    EdgeKindV2.INVALIDATES,
                    EdgeReasonV2.MUTATION_INVALIDATES,
                    region=region,
                ),
                _edge(
                    right,
                    left,
                    EdgeKindV2.INVALIDATES,
                    EdgeReasonV2.MUTATION_INVALIDATES,
                    region=region,
                ),
            )
        )

    conflicting_region = _has_conflicting_effects(left, right)
    if conflicting_region is not None:
        return _PairClassification(
            (
                _edge(
                    left,
                    right,
                    EdgeKindV2.CONFLICTS_WITH,
                    EdgeReasonV2.OVERLAPPING_EFFECT,
                    region=conflicting_region,
                ),
            )
        )

    edges: list[ActionGraphEdgeV2] = []
    edges.extend(_dependency_edges(left, right, index))
    edges.extend(_dependency_edges(right, left, index))
    if edges:
        return _PairClassification(_dedupe_edges(edges))

    if _proven_disjoint(left, right):
        return _PairClassification(
            (
                _edge(
                    left,
                    right,
                    EdgeKindV2.COMMUTES_WITH,
                    EdgeReasonV2.PROVEN_DISJOINT,
                ),
            )
        )

    region = _overlap_region(left, right)
    return _PairClassification(
        (
            _edge(
                left,
                right,
                EdgeKindV2.CONFLICTS_WITH,
                (
                    EdgeReasonV2.OVERLAPPING_EFFECT
                    if region is not None
                    else EdgeReasonV2.UNKNOWN_RELATIONSHIP
                ),
                region=region,
            ),
        ),
        unknown=region is None,
    )


def _interaction_components(
    node_ids: tuple[str, ...],
    edges: tuple[ActionGraphEdgeV2, ...],
) -> int:
    if not node_ids:
        return 0
    parent = {node: node for node in node_ids}

    def find(node: str) -> str:
        while parent[node] != node:
            parent[node] = parent[parent[node]]
            node = parent[node]
        return node

    def union(left: str, right: str) -> None:
        left_root, right_root = find(left), find(right)
        if left_root == right_root:
            return
        if right_root < left_root:
            left_root, right_root = right_root, left_root
        parent[right_root] = left_root

    for edge in edges:
        if edge.kind is not EdgeKindV2.COMMUTES_WITH:
            union(edge.source_action_id, edge.target_action_id)
    return len({find(node) for node in node_ids})


def _raw_ordered_plan_count(action_count: int) -> int:
    if action_count == 0:
        return 1
    factorial = math.factorial(action_count)
    return sum(
        factorial // math.factorial(action_count - size)
        for size in range(1, action_count + 1)
    )


def _canonical_plan_count(
    node_ids: tuple[str, ...],
    edges: tuple[ActionGraphEdgeV2, ...],
) -> tuple[int, bool]:
    count = len(node_ids)
    if count == 0:
        return 0, True
    if count > EXACT_PLAN_COUNT_MAX_ACTIONS:
        return (1 << count) - 1, False
    position = {node: index for index, node in enumerate(node_ids)}
    prohibited: set[tuple[int, int]] = set()
    required: list[tuple[int, int]] = []
    for edge in edges:
        source = position[edge.source_action_id]
        target = position[edge.target_action_id]
        if edge.kind in PROHIBITIVE_EDGE_KINDS:
            prohibited.add((min(source, target), max(source, target)))
        elif edge.kind is EdgeKindV2.REQUIRES:
            required.append((source, target))
    valid = 0
    for mask in range(1, 1 << count):
        if any(mask & (1 << left) and mask & (1 << right) for left, right in prohibited):
            continue
        if any(mask & (1 << target) and not mask & (1 << source) for source, target in required):
            continue
        valid += 1
    return valid, True


class ActionGraphCompilerV2:
    """Compile a complete observable legal-action set into a canonical graph."""

    def __init__(self, max_graph_actions: int = 1024) -> None:
        if max_graph_actions < 1:
            raise ValueError("max_graph_actions must be positive")
        self.max_graph_actions = max_graph_actions

    def compile(
        self,
        observation: ObservationV2,
        legal_actions: LegalActionSetV2,
    ) -> ActionGraphV2:
        if legal_actions.observation_id != observation.observation_id:
            raise ContractError("legal action set is stale for graph compilation")
        expected = ActionEnumeratorV2(self.max_graph_actions).enumerate(observation)
        if expected != legal_actions:
            raise ContractError(
                "legal action set is not the complete observable enumeration"
            )
        return self._compile_bound(observation, legal_actions)

    def _compile_bound(
        self,
        observation: ObservationV2,
        legal_actions: LegalActionSetV2,
    ) -> ActionGraphV2:
        """Compile already-bound claims; private seam for exhaustive kernel tests."""

        # Re-derivation binds the supplied set id to its exact node inventory,
        # even when a caller bypassed ``LegalActionSetV2.create`` via a forged
        # but otherwise schema-valid document.
        rebound = LegalActionSetV2.create(observation.observation_id, legal_actions.actions)
        if rebound.legal_action_set_id != legal_actions.legal_action_set_id:
            raise ContractError("legal action set identity does not match its actions")

        nodes = tuple(ActionGraphNodeV2.from_action(action) for action in legal_actions.actions)
        index = ObservationIndexV2(observation)
        edges: list[ActionGraphEdgeV2] = []
        unknown_pairs = 0
        for right_index, right in enumerate(legal_actions.actions):
            for left in legal_actions.actions[:right_index]:
                classified = _classify_pair(left, right, index)
                if classified.unknown:
                    unknown_pairs += 1
                edges.extend(classified.edges)
        frozen_edges = _dedupe_edges(edges)
        node_ids = tuple(node.node_id for node in nodes)
        counts = {kind: 0 for kind in EDGE_KINDS_IN_ORDER}
        for edge in frozen_edges:
            counts[edge.kind] += 1
        canonical_count, exact = _canonical_plan_count(node_ids, frozen_edges)
        raw_count = _raw_ordered_plan_count(len(nodes))
        reasons: list[str] = []
        if not exact:
            reasons.append(
                f"canonical_plan_count_upper_bound_for_{len(nodes)}_actions"
            )
        if unknown_pairs:
            reasons.append(f"unknown_relationship_pairs={unknown_pairs}")
        metrics = ActionGraphMetricsV2(
            legal_action_count=len(nodes),
            edge_count_by_kind=tuple(
                (kind, counts[kind]) for kind in EDGE_KINDS_IN_ORDER
            ),
            independent_action_groups=_interaction_components(node_ids, frozen_edges),
            raw_permutation_estimate=raw_count,
            canonical_plan_count=canonical_count,
            reduction_ratio_fixed=(
                raw_count * 10_000 // canonical_count if canonical_count else 0
            ),
            compile_duration_ms=0,
            revalidation_regions=tuple(
                sorted(
                    {
                        region
                        for action in legal_actions.actions
                        for region in action.affected_regions
                    }
                )
            ),
            rejected_reasons=tuple(reasons),
        )
        return ActionGraphV2.create(
            observation_id=observation.observation_id,
            legal_action_set_id=legal_actions.legal_action_set_id,
            nodes=nodes,
            edges=frozen_edges,
            metrics=metrics,
        )


def canonical_action_order(
    graph: ActionGraphV2,
    selected_action_ids: Iterable[str] | None = None,
) -> tuple[str, ...]:
    """Return the stable topological order for one graph-valid selection."""

    all_ids = tuple(node.node_id for node in graph.nodes)
    known = set(all_ids)
    selected_tuple = all_ids if selected_action_ids is None else tuple(selected_action_ids)
    selected = set(selected_tuple)
    if len(selected) != len(selected_tuple):
        raise GraphSelectionError("selected action ids must be unique")
    missing = sorted(selected - known)
    if missing:
        raise GraphSelectionError("selected action is absent from graph: " + missing[0])

    predecessors: dict[str, set[str]] = {node: set() for node in selected}
    for edge in graph.edges:
        source_selected = edge.source_action_id in selected
        target_selected = edge.target_action_id in selected
        if edge.kind in PROHIBITIVE_EDGE_KINDS and source_selected and target_selected:
            raise GraphSelectionError(
                f"selected actions violate {edge.kind.value}: "
                f"{edge.source_action_id} / {edge.target_action_id}"
            )
        if edge.kind is EdgeKindV2.REQUIRES and target_selected and not source_selected:
            raise GraphSelectionError(
                f"selected action {edge.target_action_id} requires {edge.source_action_id}"
            )
        if (
            edge.kind in {EdgeKindV2.MUST_PRECEDE, EdgeKindV2.REQUIRES}
            and source_selected
            and target_selected
        ):
            predecessors[edge.target_action_id].add(edge.source_action_id)

    pending = set(selected)
    complete: set[str] = set()
    ordered: list[str] = []
    while pending:
        ready = sorted(node for node in pending if predecessors[node] <= complete)
        if not ready:
            # ActionGraphV2 rejects a cycle at construction, so reaching this
            # branch means the graph object was mutated outside its frozen
            # contract or a future edge kind was mishandled.
            raise GraphSelectionError("selected action graph contains a directed cycle")
        chosen = ready[0]
        pending.remove(chosen)
        complete.add(chosen)
        ordered.append(chosen)
    return tuple(ordered)
