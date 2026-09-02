from __future__ import annotations

import copy
import itertools
import math
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from civ_arena.canonical import canonical
from civ_arena.game.sim.rules import apply_action, check_action
from civ_arena.game.sim.state import SimState
from civ_arena.v2.contracts import (
    ActionGraphMetricsV2,
    ActionGraphNodeV2,
    ActionGraphV2,
    ActionKindV2,
    EdgeKindV2,
    EffectClaimV2,
    EffectKindV2,
    EntityRefV2,
    EntityTypeV2,
    FactSourceV2,
    FactSubjectScopeV2,
    KnowledgeValueV2,
    LegalActionSetV2,
    LegalActionV2,
    ObservableFactV2,
    ObservationPhaseV2,
    ObservationV2,
    PreconditionClaimV2,
    PreconditionOperatorV2,
    ResourceClaimV2,
    ResourceKindV2,
    ResourceModeV2,
)
from civ_arena.v2.enumeration import ActionEnumeratorV2
from civ_arena.v2.environment import simulator_facets_v2
from civ_arena.v2.graph import (
    ActionGraphCompilerV2,
    GraphSelectionError,
    canonical_action_order,
)
from civ_arena.v2.schemas import ContractError

PLAYER = EntityRefV2(EntityTypeV2.PLAYER, "p0")
U1 = EntityRefV2(EntityTypeV2.UNIT, "u1")
U2 = EntityRefV2(EntityTypeV2.UNIT, "u2")
ENEMY = EntityRefV2(EntityTypeV2.UNIT, "u9")
RULESET = "a" * 64


def _fact(
    subject: EntityRefV2,
    predicate: str,
    value: Any,
    *,
    scope: FactSubjectScopeV2 = FactSubjectScopeV2.OWNED,
) -> ObservableFactV2:
    return ObservableFactV2(
        subject=subject,
        subject_scope=scope,
        predicate=predicate,
        value=KnowledgeValueV2.known(value, observed_turn=1),
        source=FactSourceV2.DIRECT,
    )


def _observation(*, gold: int = 100) -> ObservationV2:
    facts = [
        _fact(PLAYER, "gold", gold, scope=FactSubjectScopeV2.SELF),
        _fact(U1, "coord", "0,0"),
        _fact(U1, "movement", 2),
        _fact(U1, "ranged_strength", 0),
        _fact(U1, "fortified", False),
        _fact(U2, "coord", "2,0"),
        _fact(U2, "movement", 2),
        _fact(U2, "ranged_strength", 0),
        _fact(U2, "fortified", False),
        _fact(
            ENEMY,
            "coord",
            "2,-1",
            scope=FactSubjectScopeV2.VISIBLE_FOREIGN,
        ),
        _fact(
            EntityRefV2(EntityTypeV2.TILE, "tile:1,0"),
            "terrain",
            "PLAINS",
            scope=FactSubjectScopeV2.PUBLIC,
        ),
        _fact(
            EntityRefV2(EntityTypeV2.TILE, "tile:1,0"),
            "owner_id",
            -1,
            scope=FactSubjectScopeV2.PUBLIC,
        ),
    ]
    return ObservationV2.create(
        environment_id="fake-v2",
        game_version="fake-1",
        ruleset_digest=RULESET,
        turn=1,
        active_player=PLAYER,
        observing_player=PLAYER,
        phase=ObservationPhaseV2.TURN,
        observed_at_seq=0,
        facts=facts,
    )


def _precondition(
    subject: EntityRefV2,
    predicate: str,
    value: Any,
    operator: PreconditionOperatorV2 = PreconditionOperatorV2.EQ,
) -> PreconditionClaimV2:
    return PreconditionClaimV2(
        subject,
        predicate,
        operator,
        KnowledgeValueV2.known(value, observed_turn=1),
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
    value: Any,
    region: str,
    *,
    unknown: bool = False,
) -> EffectClaimV2:
    expected = (
        KnowledgeValueV2.unknown("engine-resolved", observed_turn=1)
        if unknown
        else KnowledgeValueV2.known(value, observed_turn=1)
    )
    return EffectClaimV2(kind, subject, attribute, expected, region)


def _fortify(
    observation: ObservationV2,
    unit: EntityRefV2,
    *,
    preconditions: list[PreconditionClaimV2] | None = None,
) -> LegalActionV2:
    region = f"unit:{unit.entity_id}"
    return LegalActionV2.create(
        ActionKindV2.FORTIFY,
        unit,
        observation.observation_id,
        parameters={"unit_id": unit.entity_id},
        preconditions=preconditions or [_precondition(unit, "fortified", False)],
        resource_claims=[
            _resource(ResourceKindV2.UNIT, unit, 1, ResourceModeV2.RESERVE, region)
        ],
        expected_effects=[_effect(EffectKindV2.SET, unit, "fortified", True, region)],
        affected_regions=[region],
    )


def _research(observation: ObservationV2, tech_id: str) -> LegalActionV2:
    tech = EntityRefV2(EntityTypeV2.TECHNOLOGY, tech_id)
    region = "player:p0:research"
    return LegalActionV2.create(
        ActionKindV2.SET_RESEARCH,
        PLAYER,
        observation.observation_id,
        target=tech,
        parameters={"tech_id": tech_id},
        preconditions=[_precondition(tech, "available", True)],
        resource_claims=[
            _resource(
                ResourceKindV2.RESEARCH_SLOT,
                PLAYER,
                1,
                ResourceModeV2.RESERVE,
                region,
            )
        ],
        expected_effects=[
            _effect(EffectKindV2.SET, PLAYER, "researching", tech_id, region)
        ],
        affected_regions=[region],
    )


def _production(
    observation: ObservationV2,
    city_id: str,
    item_id: str,
) -> LegalActionV2:
    city = EntityRefV2(EntityTypeV2.CITY, city_id)
    item = EntityRefV2(EntityTypeV2.PRODUCTION_ITEM, f"{city_id}:{item_id}")
    region = f"city:{city_id}:production"
    return LegalActionV2.create(
        ActionKindV2.SET_CITY_PRODUCTION,
        city,
        observation.observation_id,
        target=item,
        parameters={"city_id": city_id, "item_id": item_id},
        preconditions=[_precondition(item, "available", True)],
        resource_claims=[
            _resource(
                ResourceKindV2.PRODUCTION_SLOT,
                city,
                1,
                ResourceModeV2.RESERVE,
                region,
            )
        ],
        expected_effects=[
            _effect(
                EffectKindV2.SET,
                city,
                "production_queue",
                (item_id,),
                region,
            )
        ],
        affected_regions=[region],
    )


def _purchase(
    observation: ObservationV2,
    city_id: str,
    item_id: str,
    cost: int,
) -> LegalActionV2:
    city = EntityRefV2(EntityTypeV2.CITY, city_id)
    item = EntityRefV2(EntityTypeV2.PRODUCTION_ITEM, f"{city_id}:{item_id}")
    gold_region = "player:p0:gold"
    return LegalActionV2.create(
        ActionKindV2.PURCHASE,
        city,
        observation.observation_id,
        target=item,
        parameters={"city_id": city_id, "item_id": item_id},
        preconditions=[
            _precondition(PLAYER, "gold", cost, PreconditionOperatorV2.GTE)
        ],
        resource_claims=[
            _resource(
                ResourceKindV2.GOLD,
                PLAYER,
                cost,
                ResourceModeV2.CONSUME,
                gold_region,
            )
        ],
        expected_effects=[
            _effect(EffectKindV2.SET, PLAYER, "gold", 100 - cost, gold_region),
            _effect(
                EffectKindV2.CREATE,
                item,
                "created",
                None,
                f"city:{city_id}",
                unknown=True,
            ),
        ],
        affected_regions=[gold_region, f"city:{city_id}"],
    )


def _move(observation: ObservationV2) -> LegalActionV2:
    return LegalActionV2.create(
        ActionKindV2.MOVE_UNIT,
        U1,
        observation.observation_id,
        parameters={"unit_id": "u1", "dest": "1,0"},
        preconditions=[
            _precondition(U1, "movement", 1, PreconditionOperatorV2.GTE)
        ],
        resource_claims=[
            _resource(
                ResourceKindV2.MOVEMENT,
                U1,
                1,
                ResourceModeV2.CONSUME,
                "unit:u1:movement",
            )
        ],
        expected_effects=[
            _effect(EffectKindV2.MOVE, U1, "coord", "1,0", "unit:u1"),
            _effect(EffectKindV2.SET, U1, "movement", 1, "unit:u1:movement"),
        ],
        affected_regions=["unit:u1", "unit:u1:movement", "tile:1,0"],
    )


def _attack(observation: ObservationV2) -> LegalActionV2:
    return LegalActionV2.create(
        ActionKindV2.ATTACK,
        U1,
        observation.observation_id,
        target=ENEMY,
        parameters={"unit_id": "u1", "target_id": "u9"},
        preconditions=[
            _precondition(U1, "movement", 1, PreconditionOperatorV2.GTE),
            _precondition(ENEMY, "target_exists", True),
        ],
        resource_claims=[
            _resource(
                ResourceKindV2.MOVEMENT,
                U1,
                2,
                ResourceModeV2.CONSUME,
                "unit:u1:movement",
            )
        ],
        expected_effects=[
            _effect(
                EffectKindV2.DAMAGE,
                ENEMY,
                "hp",
                None,
                "unit:u9",
                unknown=True,
            ),
            _effect(EffectKindV2.SET, U1, "movement", 0, "unit:u1:movement"),
        ],
        affected_regions=["unit:u1", "unit:u1:movement", "unit:u9"],
    )


def _compile(
    observation: ObservationV2,
    actions: list[LegalActionV2],
) -> ActionGraphV2:
    legal = LegalActionSetV2.create(observation.observation_id, actions)
    # Edge fixtures intentionally isolate the pure claim kernel.  The public
    # compile path independently re-enumerates and is pinned below.
    return ActionGraphCompilerV2()._compile_bound(observation, legal)


def _edge_kinds(graph: ActionGraphV2) -> set[EdgeKindV2]:
    return {edge.kind for edge in graph.edges}


def test_compiler_emits_every_normative_edge_kind() -> None:
    observation = _observation()
    research = _compile(
        observation,
        [_research(observation, "MINING"), _research(observation, "POTTERY")],
    )
    assert _edge_kinds(research) == {EdgeKindV2.MUTEX}

    production = _compile(
        observation,
        [
            _production(observation, "c1", "WARRIOR"),
            _production(observation, "c1", "MONUMENT"),
        ],
    )
    assert _edge_kinds(production) == {EdgeKindV2.MUTEX}

    over_budget = _compile(
        observation,
        [
            _purchase(observation, "c1", "WARRIOR", 60),
            _purchase(observation, "c2", "ARCHER", 60),
        ],
    )
    assert _edge_kinds(over_budget) == {EdgeKindV2.CONSUMES_SHARED_RESOURCE}

    affordable = _compile(
        observation,
        [
            _purchase(observation, "c1", "WARRIOR", 30),
            _purchase(observation, "c2", "ARCHER", 40),
        ],
    )
    assert _edge_kinds(affordable) == {EdgeKindV2.INVALIDATES}
    assert len(affordable.edges) == 2

    sequence = _compile(observation, [_move(observation), _attack(observation)])
    assert _edge_kinds(sequence) == {
        EdgeKindV2.MUST_PRECEDE,
        EdgeKindV2.ENABLES,
    }
    assert canonical_action_order(sequence) == (
        _move(observation).action_id,
        _attack(observation).action_id,
    )

    requires_source = _fortify(observation, U1)
    requires_target = _fortify(
        observation,
        U2,
        preconditions=[_precondition(U1, "fortified", True)],
    )
    requires = _compile(observation, [requires_source, requires_target])
    assert _edge_kinds(requires) == {EdgeKindV2.REQUIRES}
    with pytest.raises(GraphSelectionError, match="requires"):
        canonical_action_order(requires, [requires_target.action_id])
    ordered = canonical_action_order(
        requires,
        [requires_source.action_id, requires_target.action_id],
    )
    assert ordered == (
        requires_source.action_id,
        requires_target.action_id,
    )

    commutes = _compile(
        observation,
        [_fortify(observation, U1), _fortify(observation, U2)],
    )
    assert _edge_kinds(commutes) == {EdgeKindV2.COMMUTES_WITH}

    unknown_left = LegalActionV2.create(
        ActionKindV2.FORTIFY,
        U1,
        observation.observation_id,
        parameters={"unit_id": "u1"},
    )
    unknown_right = LegalActionV2.create(
        ActionKindV2.FORTIFY,
        U2,
        observation.observation_id,
        parameters={"unit_id": "u2"},
    )
    unknown = _compile(observation, [unknown_left, unknown_right])
    assert _edge_kinds(unknown) == {EdgeKindV2.CONFLICTS_WITH}
    assert unknown.metrics.rejected_reasons == ("unknown_relationship_pairs=1",)


@pytest.mark.parametrize("action_count", range(2, 9))
def test_exhaustive_input_permutations_collapse_by_stable_action_id(
    action_count: int,
) -> None:
    observation = _observation()
    actions = [
        _fortify(observation, EntityRefV2(EntityTypeV2.UNIT, f"u{index + 10}"))
        for index in range(action_count)
    ]
    graph = _compile(observation, actions)
    assert _edge_kinds(graph) == {EdgeKindV2.COMMUTES_WITH}
    expected = tuple(sorted(action.action_id for action in actions))
    for permutation in itertools.permutations(action.action_id for action in actions):
        assert canonical_action_order(graph, permutation) == expected
    assert graph.metrics.canonical_plan_count == (1 << action_count) - 1
    factorial = math.factorial(action_count)
    assert graph.metrics.raw_permutation_estimate == sum(
        factorial // math.factorial(action_count - size)
        for size in range(1, action_count + 1)
    )


@settings(derandomize=True, database=None, max_examples=30)
@given(st.permutations(["u10", "u11", "u12", "u13", "u14"]))
def test_compilation_is_pure_under_action_input_permutation(
    unit_ids: list[str],
) -> None:
    observation = _observation()
    actions = [
        _fortify(observation, EntityRefV2(EntityTypeV2.UNIT, unit_id))
        for unit_id in unit_ids
    ]
    graph = _compile(observation, actions)
    baseline_actions = sorted(actions, key=lambda action: action.action_id)
    baseline = _compile(observation, baseline_actions)
    assert graph == baseline
    assert graph.graph_id == baseline.graph_id


def test_conflict_dominates_commute_and_refuses_co_selection() -> None:
    observation = _observation()
    left = LegalActionV2.create(
        ActionKindV2.FORTIFY,
        U1,
        observation.observation_id,
        parameters={"unit_id": "u1"},
        expected_effects=[_effect(EffectKindV2.SET, U1, "fortified", True, "shared")],
        affected_regions=["shared"],
    )
    right = LegalActionV2.create(
        ActionKindV2.FORTIFY,
        U2,
        observation.observation_id,
        parameters={"unit_id": "u2"},
        expected_effects=[_effect(EffectKindV2.SET, U2, "fortified", True, "shared")],
        affected_regions=["shared"],
    )
    graph = _compile(observation, [left, right])
    assert _edge_kinds(graph) == {EdgeKindV2.CONFLICTS_WITH}
    with pytest.raises(GraphSelectionError, match="CONFLICTS_WITH"):
        canonical_action_order(graph, [left.action_id, right.action_id])


def test_graph_identity_binds_the_exact_legal_action_inventory() -> None:
    observation = _observation()
    actions = [_fortify(observation, U1), _fortify(observation, U2)]
    graph = _compile(observation, actions)
    with pytest.raises(ContractError, match="legal_action_set_id"):
        ActionGraphV2.create(
            observation_id=observation.observation_id,
            legal_action_set_id="f" * 64,
            nodes=[ActionGraphNodeV2.from_action(action) for action in actions],
            edges=graph.edges,
            metrics=graph.metrics,
        )


def test_graph_contract_refuses_missing_pair_classification() -> None:
    observation = _observation()
    actions = [_fortify(observation, U1), _fortify(observation, U2)]
    legal = LegalActionSetV2.create(observation.observation_id, actions)
    metrics = ActionGraphMetricsV2.empty(2)
    with pytest.raises(ContractError, match="explicitly classify every action pair"):
        ActionGraphV2.create(
            observation_id=observation.observation_id,
            legal_action_set_id=legal.legal_action_set_id,
            nodes=[ActionGraphNodeV2.from_action(action) for action in actions],
            edges=[],
            metrics=metrics,
        )


def test_contract_cycle_witness_remains_deterministic() -> None:
    observation = _observation()
    actions = [_fortify(observation, U1), _fortify(observation, U2)]
    graph = _compile(observation, actions)
    left, right = sorted(action.action_id for action in actions)
    from civ_arena.v2.contracts import (
        ActionGraphEdgeV2,
        EdgeAuthorityV2,
        EdgeReasonV2,
    )

    forward = ActionGraphEdgeV2.create(
        left,
        right,
        EdgeKindV2.MUST_PRECEDE,
        EdgeAuthorityV2.AUTHORITATIVE,
        EdgeReasonV2.SAME_ACTOR_SEQUENCE,
    )
    backward = ActionGraphEdgeV2.create(
        right,
        left,
        EdgeKindV2.REQUIRES,
        EdgeAuthorityV2.AUTHORITATIVE,
        EdgeReasonV2.PRECONDITION_DEPENDENCY,
    )
    counts = dict(graph.metrics.edge_count_by_kind)
    counts[EdgeKindV2.COMMUTES_WITH] = 0
    counts[EdgeKindV2.MUST_PRECEDE] = 1
    counts[EdgeKindV2.REQUIRES] = 1
    metrics = ActionGraphMetricsV2(
        legal_action_count=2,
        edge_count_by_kind=tuple((kind, counts[kind]) for kind in EdgeKindV2),
        independent_action_groups=1,
        raw_permutation_estimate=4,
        canonical_plan_count=1,
        reduction_ratio_fixed=40_000,
        compile_duration_ms=0,
        revalidation_regions=(),
        rejected_reasons=(),
    )
    expected = f"directed action-graph cycle: {left} -> {right} -> {left}"
    with pytest.raises(ContractError, match=expected):
        ActionGraphV2.create(
            observation_id=observation.observation_id,
            legal_action_set_id=graph.legal_action_set_id,
            nodes=graph.nodes,
            edges=[backward, forward],
            metrics=metrics,
        )


@pytest.mark.asyncio
async def test_partial_order_reduction_preserves_reachable_fake_states() -> None:
    environment, monitor = simulator_facets_v2()
    await environment.reset({"seed": 41})
    observation = await environment.begin_turn(0, 1)
    legal = ActionEnumeratorV2().enumerate(observation)
    fortify = [
        action
        for action in legal.actions
        if action.action_kind is ActionKindV2.FORTIFY
    ][:4]
    assert len(fortify) >= 2
    graph = _compile(observation, fortify)
    assert _edge_kinds(graph) == {EdgeKindV2.COMMUTES_WITH}

    initial = monitor.snapshot()
    exhaustive_states: set[str] = set()
    for permutation in itertools.permutations(fortify):
        state = SimState.from_doc(initial)
        for action in permutation:
            assert check_action(
                state,
                0,
                action.action_kind.value,
                dict(action.parameters),
            ) is None
            apply_action(state, 0, action.action_kind.value, dict(action.parameters))
        exhaustive_states.add(canonical(state.to_doc()))

    by_id = {action.action_id: action for action in fortify}
    reduced = SimState.from_doc(initial)
    for action_id in canonical_action_order(graph):
        action = by_id[action_id]
        apply_action(reduced, 0, action.action_kind.value, dict(action.parameters))
    assert {canonical(reduced.to_doc())} == exhaustive_states


@pytest.mark.asyncio
async def test_public_compiler_refuses_an_incomplete_or_augmented_legal_set() -> None:
    environment, _ = simulator_facets_v2()
    await environment.reset({"seed": 41})
    observation = await environment.begin_turn(0, 1)
    legal = ActionEnumeratorV2().enumerate(observation)
    compiler = ActionGraphCompilerV2()

    graph = compiler.compile(observation, legal)
    assert graph.legal_action_set_id == legal.legal_action_set_id

    incomplete = LegalActionSetV2.create(
        observation.observation_id,
        legal.actions[:-1],
    )
    with pytest.raises(ContractError, match="complete observable enumeration"):
        compiler.compile(observation, incomplete)

    duplicate_semantic_variant = LegalActionV2.create(
        ActionKindV2.FORTIFY,
        EntityRefV2(EntityTypeV2.UNIT, "u999"),
        observation.observation_id,
        parameters={"unit_id": "u999"},
        expected_effects=[
            _effect(
                EffectKindV2.SET,
                EntityRefV2(EntityTypeV2.UNIT, "u999"),
                "fortified",
                True,
                "unit:u999",
            )
        ],
        affected_regions=["unit:u999"],
    )
    augmented = LegalActionSetV2.create(
        observation.observation_id,
        [*legal.actions, duplicate_semantic_variant],
    )
    with pytest.raises(ContractError, match="complete observable enumeration"):
        compiler.compile(observation, augmented)


@pytest.mark.asyncio
async def test_graph_is_noninterfering_for_equivalent_visible_worlds() -> None:
    control, _ = simulator_facets_v2()
    variant, variant_monitor = simulator_facets_v2()
    await control.reset({"seed": 73})
    await variant.reset({"seed": 73})
    await control.begin_turn(0, 1)
    first_variant = await variant.begin_turn(0, 1)

    hidden = copy.deepcopy(variant_monitor.snapshot())
    visible_ids = {
        fact.subject.entity_id
        for fact in first_variant.facts
        if fact.subject.entity_type is EntityTypeV2.UNIT
    }
    hidden_enemy = next(
        unit
        for unit in hidden["units"].values()
        if unit["owner"] != 0 and unit["unit_id"] not in visible_ids
    )
    hidden_enemy["hp"] -= 9
    hidden["rng"][1][2] ^= 1
    variant_monitor.restore(hidden)

    control_observation = await control.observe(0)
    variant_observation = await variant.observe(0)
    assert control_observation == variant_observation
    enumerator = ActionEnumeratorV2()
    compiler = ActionGraphCompilerV2()
    control_actions = enumerator.enumerate(control_observation)
    variant_actions = enumerator.enumerate(variant_observation)
    assert compiler.compile(control_observation, control_actions) == compiler.compile(
        variant_observation,
        variant_actions,
    )
