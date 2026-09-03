"""CAR-M1 A/B/C/D turn-core experiment.

The three controls in this module are intentionally unsafe and may call the
legacy fake adapter directly.  They are isolated under ``civ_arena.experiments``
and cannot be selected by a match config.  Normal matches continue to use only
``TransactionalExecutorV2``.

Every treatment restores the same canonical fake state and reparses the same
captured ``TurnProposalV2`` document.  The controls differ only in when they
filter/order/revalidate those intents:

* A: author-order sequential attempts with adapter legality only;
* B: initial observable legal-list filter, then author order;
* C: initial legal graph filter and canonical order, without recompilation;
* D: the production graph-bound transactional executor.
"""

from __future__ import annotations

import copy
import enum
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from civ_arena.canonical import atomic_write_text, canonical, sha256_hex
from civ_arena.game.adapter import ActionCommand, ActionResult
from civ_arena.game.sim.layouts import duel_start
from civ_arena.game.sim.rules import apply_action
from civ_arena.game.sim.simulator import SimulatorAdapter
from civ_arena.game.sim.state import SimState, neighbors, tile_key
from civ_arena.v2.contracts import (
    PLAYER_ACTION_KINDS_V2,
    ActionGraphV2,
    ActionIntentV2,
    ActionKindV2,
    ActionStatusV2,
    EdgeKindV2,
    KnowledgeValueV2,
    LegalActionSetV2,
    LegalActionV2,
    PolicyDescriptorV2,
    PolicyKindV2,
    PreconditionClaimV2,
    PreconditionOperatorV2,
    RejectionCodeV2,
    TurnProposalV2,
    TurnTerminationV2,
    VerificationStatusV2,
)
from civ_arena.v2.enumeration import ActionEnumeratorV2
from civ_arena.v2.environment import simulator_facets_v2, split_adapter_v2
from civ_arena.v2.executor import (
    TransactionalExecutorV2,
    verify_observable_postconditions,
)
from civ_arena.v2.graph import (
    PROHIBITIVE_EDGE_KINDS,
    ActionGraphCompilerV2,
    canonical_action_order,
)
from civ_arena.v2.schemas import ContractError

MANIFEST_SCHEMA = 2
EXPERIMENT_ID = "car-m1-v2-turn-core-abcd"
GENERATED_FIXTURES = 80
CURATED_FIXTURES = 20
ALL_FAILURE_CLASSES = (
    "adapter_rejected_mutations",
    "forced_turn_closures",
    "stale_authorizations_reaching_adapter",
    "unhandled_postcondition_divergences",
    "unresolved_mandatory_decisions",
)


class TreatmentV2(enum.StrEnum):
    SEQUENTIAL = "A_sequential"
    LEGAL_LIST = "B_legal_list"
    DAG = "C_dag"
    DAG_TX = "D_dag_tx"


class FixtureFaultV2(enum.StrEnum):
    NONE = "none"
    DROP_RESEARCH_EFFECT = "drop_research_effect"
    REJECT_FORTIFY = "reject_fortify"


class FixtureVariantV2(enum.StrEnum):
    STANDARD = "standard"
    CITY = "city"
    COMBAT = "combat"


@dataclass(frozen=True)
class FixtureRecipeV2:
    fixture_id: str
    fixture_class: str
    seed: int
    variant: FixtureVariantV2
    fault: FixtureFaultV2

    def to_doc(self) -> dict[str, Any]:
        return {
            "fixture_id": self.fixture_id,
            "fixture_class": self.fixture_class,
            "seed": self.seed,
            "variant": self.variant.value,
            "fault": self.fault.value,
        }

    @classmethod
    def from_doc(cls, doc: Mapping[str, Any]) -> FixtureRecipeV2:
        if set(doc) != {"fixture_id", "fixture_class", "seed", "variant", "fault"}:
            raise ContractError("fixture recipe has unknown or missing fields")
        fixture_id = doc["fixture_id"]
        fixture_class = doc["fixture_class"]
        seed = doc["seed"]
        if not isinstance(fixture_id, str) or not fixture_id:
            raise ContractError("fixture_id must be a non-empty string")
        if fixture_class not in {"generated", "curated"}:
            raise ContractError("fixture_class must be generated or curated")
        if type(seed) is not int or seed < 1:
            raise ContractError("fixture seed must be a positive integer")
        return cls(
            fixture_id,
            fixture_class,
            seed,
            FixtureVariantV2(doc["variant"]),
            FixtureFaultV2(doc["fault"]),
        )


def fixture_recipes_v2() -> tuple[FixtureRecipeV2, ...]:
    """Return the preregistered 80 generated + 20 adversarial recipes."""

    generated = [
        FixtureRecipeV2(
            f"generated-{index:03d}",
            "generated",
            410_003 + index * 7_919,
            FixtureVariantV2.STANDARD,
            FixtureFaultV2.NONE,
        )
        for index in range(GENERATED_FIXTURES)
    ]
    curated: list[FixtureRecipeV2] = []
    for index in range(CURATED_FIXTURES):
        if index < 4:
            variant = FixtureVariantV2.STANDARD
            fault = FixtureFaultV2.DROP_RESEARCH_EFFECT
        elif index < 8:
            variant = FixtureVariantV2.STANDARD
            fault = FixtureFaultV2.REJECT_FORTIFY
        elif index < 12:
            variant = FixtureVariantV2.CITY
            fault = FixtureFaultV2.NONE
        elif index < 16:
            variant = FixtureVariantV2.CITY
            fault = FixtureFaultV2.DROP_RESEARCH_EFFECT
        else:
            variant = FixtureVariantV2.COMBAT
            fault = FixtureFaultV2.NONE
        curated.append(
            FixtureRecipeV2(
                f"curated-{index:03d}",
                "curated",
                910_003 + index * 7_919,
                variant,
                fault,
            )
        )
    return tuple([*generated, *curated])


def _fixture_state(recipe: FixtureRecipeV2) -> dict[str, Any]:
    state = SimState.from_doc(duel_start(recipe.seed))
    if recipe.variant is FixtureVariantV2.CITY:
        settler = next(
            unit
            for unit in sorted(state.units.values(), key=lambda row: row["unit_id"])
            if unit["owner"] == 0 and unit["type"] == "SETTLER"
        )
        apply_action(
            state,
            0,
            ActionKindV2.FOUND_CITY.value,
            {"unit_id": settler["unit_id"], "name": "Arena Fixture"},
        )
    elif recipe.variant is FixtureVariantV2.COMBAT:
        own = next(
            unit
            for unit in sorted(state.units.values(), key=lambda row: row["unit_id"])
            if unit["owner"] == 0 and unit["type"] == "WARRIOR"
        )
        rival = next(
            unit
            for unit in sorted(state.units.values(), key=lambda row: row["unit_id"])
            if unit["owner"] == 1 and unit["type"] == "WARRIOR"
        )
        occupied = {
            tile_key(unit["q"], unit["r"])
            for unit in state.units.values()
            if unit["unit_id"] != rival["unit_id"]
        }
        destination = next(
            (q, r)
            for q, r in sorted(neighbors(own["q"], own["r"]))
            if tile_key(q, r) in state.tiles and tile_key(q, r) not in occupied
        )
        state.tiles[tile_key(*destination)]["terrain"] = "PLAINS"
        rival["q"], rival["r"] = destination
        state.extend_revealed(0, state.sight_tiles(0))
    return state.to_doc()


class _FaultingSimulatorAdapter(SimulatorAdapter):
    """Deterministic, declared fault injector used only by curated fixtures."""

    def __init__(self, fault: FixtureFaultV2) -> None:
        super().__init__()
        self.fault = fault
        self.fault_fired = False

    async def act(self, cmd: ActionCommand) -> ActionResult:
        if (
            self.fault is FixtureFaultV2.REJECT_FORTIFY
            and not self.fault_fired
            and cmd.tool == ActionKindV2.FORTIFY.value
        ):
            self.fault_fired = True
            return ActionResult(
                status="rejected",
                result=None,
                rejection="curated_adapter_rejection",
                error="curated adapter rejection",
            )
        before_research: str | None = None
        if (
            self.fault is FixtureFaultV2.DROP_RESEARCH_EFFECT
            and not self.fault_fired
            and cmd.tool == ActionKindV2.SET_RESEARCH.value
        ):
            self._require_state()
            assert self.state is not None
            before_research = self.state.player(cmd.player_id)["researching"]
        result = await super().act(cmd)
        if before_research is not None and result.status == "accepted":
            assert self.state is not None
            self.state.player(cmd.player_id)["researching"] = before_research
            self._hash_cache = None
            self.fault_fired = True
        return result


@dataclass
class _FixtureRuntime:
    adapter: _FaultingSimulatorAdapter
    environment: Any
    monitor: Any
    observation: Any
    legal: LegalActionSetV2
    graph: ActionGraphV2


async def _open_fixture(recipe: FixtureRecipeV2) -> _FixtureRuntime:
    adapter = _FaultingSimulatorAdapter(recipe.fault)
    await adapter.setup({"seed": recipe.seed})
    adapter.import_state(_fixture_state(recipe))
    descriptor = simulator_facets_v2()[0].descriptor
    environment, monitor = split_adapter_v2(adapter, descriptor)
    observation = await environment.begin_turn(0, 1)
    legal = ActionEnumeratorV2().enumerate(observation)
    graph = ActionGraphCompilerV2().compile(observation, legal)
    return _FixtureRuntime(adapter, environment, monitor, observation, legal, graph)


def experiment_policy_v2() -> PolicyDescriptorV2:
    return PolicyDescriptorV2.create(
        policy_kind=PolicyKindV2.REPLAY,
        policy_version="car-m1-deterministic-proposals-v2",
        registered_action_kinds=PLAYER_ACTION_KINDS_V2,
    )


def _intent(action: LegalActionV2, index: int) -> ActionIntentV2:
    return ActionIntentV2.create(
        action.action_kind,
        action.actor,
        target=action.target,
        parameters=dict(action.parameters),
        proposal_index=index,
    )


def deterministic_proposal_v2(
    observation: Any,
    legal: LegalActionSetV2,
    policy: PolicyDescriptorV2 | None = None,
) -> TurnProposalV2:
    """Build the fixed adversarial proposal shape from observable actions only."""

    policy = policy or experiment_policy_v2()
    fortify = next(
        action
        for action in legal.actions
        if action.action_kind is ActionKindV2.FORTIFY
    )
    intents = [
        ActionIntentV2.create(
            ActionKindV2.MOVE_UNIT,
            fortify.actor,
            parameters={"unit_id": fortify.actor.entity_id, "dest": "99,99"},
            proposal_index=0,
        ),
        ActionIntentV2.create(
            ActionKindV2.END_TURN,
            observation.observing_player,
            proposal_index=1,
        ),
        _intent(fortify, 2),
    ]
    mandatory = [action for action in legal.actions if action.mandatory]
    chosen_by_kind: dict[ActionKindV2, LegalActionV2] = {}
    for action in mandatory:
        chosen_by_kind.setdefault(action.action_kind, action)
    for action in sorted(chosen_by_kind.values(), key=lambda item: item.action_id):
        intents.append(_intent(action, len(intents)))
    if not chosen_by_kind:
        raise ContractError("CAR-M1 fixture unexpectedly has no mandatory action")
    return TurnProposalV2.create(
        policy_id=policy.descriptor_id,
        observation_id=observation.observation_id,
        intents=intents,
    )


def _matches(intent: ActionIntentV2, action: LegalActionV2) -> bool:
    return (
        intent.action_kind is action.action_kind
        and intent.actor == action.actor
        and intent.target == action.target
        and intent.parameters == action.parameters
    )


def _match_one(
    intent: ActionIntentV2,
    legal: LegalActionSetV2,
) -> LegalActionV2 | None:
    matches = [action for action in legal.actions if _matches(intent, action)]
    return matches[0] if len(matches) == 1 else None


def _dag_selection(
    proposal: TurnProposalV2,
    legal: LegalActionSetV2,
    graph: ActionGraphV2,
) -> list[tuple[ActionIntentV2, LegalActionV2]]:
    """Initial-graph selection for control C, with no later recompilation."""

    by_action: dict[str, tuple[ActionIntentV2, LegalActionV2]] = {}
    for intent in proposal.intents:
        action = _match_one(intent, legal)
        if action is None:
            continue
        candidate = (intent, action)
        previous = by_action.get(action.action_id)
        if previous is None or intent.intent_id < previous[0].intent_id:
            by_action[action.action_id] = candidate

    prohibited = {
        frozenset((edge.source_action_id, edge.target_action_id))
        for edge in graph.edges
        if edge.kind in PROHIBITIVE_EDGE_KINDS
    }
    selected: list[str] = []
    for action_id in sorted(by_action):
        if not any(frozenset((action_id, prior)) in prohibited for prior in selected):
            selected.append(action_id)
    selected_set = set(selected)
    while True:
        orphaned = {
            edge.target_action_id
            for edge in graph.edges
            if edge.kind is EdgeKindV2.REQUIRES
            and edge.target_action_id in selected_set
            and edge.source_action_id not in selected_set
        }
        if not orphaned:
            break
        selected_set.difference_update(orphaned)
    return [by_action[action_id] for action_id in canonical_action_order(graph, selected_set)]


def _failure_counts() -> dict[str, int]:
    return {name: 0 for name in ALL_FAILURE_CLASSES}


def _row(
    *,
    recipe: FixtureRecipeV2,
    treatment: TreatmentV2,
    proposal_sha256: str,
    initial_observation_id: str,
    initial_graph_id: str,
    final_state_hash: str,
    attempted: int,
    accepted: int,
    detected_divergences: int,
    completed: bool,
    termination: str,
    failures: Mapping[str, int],
) -> dict[str, Any]:
    counts = {name: failures[name] for name in ALL_FAILURE_CLASSES}
    return {
        "schema": MANIFEST_SCHEMA,
        "fixture_id": recipe.fixture_id,
        "fixture_class": recipe.fixture_class,
        "treatment": treatment.value,
        "proposal_sha256": proposal_sha256,
        "initial_observation_id": initial_observation_id,
        "initial_graph_id": initial_graph_id,
        "final_state_hash": final_state_hash,
        "actions_attempted": attempted,
        "actions_accepted": accepted,
        "detected_postcondition_divergences": detected_divergences,
        "turn_completed": completed,
        "termination": termination,
        "failures": counts,
        "control_failure_total": sum(counts.values()),
    }


async def _run_control(
    recipe: FixtureRecipeV2,
    proposal_doc: Mapping[str, Any],
    treatment: TreatmentV2,
) -> dict[str, Any]:
    runtime = await _open_fixture(recipe)
    proposal_bytes = canonical(dict(proposal_doc))
    proposal = TurnProposalV2.from_doc(dict(proposal_doc))
    if proposal.observation_id != runtime.observation.observation_id:
        raise ContractError("captured proposal does not bind the restored fixture")

    if treatment is TreatmentV2.SEQUENTIAL:
        selected: list[tuple[ActionIntentV2, LegalActionV2 | None]] = [
            (intent, _match_one(intent, runtime.legal)) for intent in proposal.intents
        ]
    elif treatment is TreatmentV2.LEGAL_LIST:
        selected = [
            (intent, action)
            for intent in proposal.intents
            if (action := _match_one(intent, runtime.legal)) is not None
        ]
    elif treatment is TreatmentV2.DAG:
        selected = list(_dag_selection(proposal, runtime.legal, runtime.graph))
    else:  # pragma: no cover - caller routes D separately
        raise AssertionError("control runner received the production treatment")

    failures = _failure_counts()
    attempted = 0
    accepted = 0
    accepted_mutations = 0
    detected = 0
    forced = False
    for index, (intent, action) in enumerate(selected):
        attempted += 1
        if (
            treatment in {TreatmentV2.LEGAL_LIST, TreatmentV2.DAG}
            and accepted_mutations > 0
        ):
            failures["stale_authorizations_reaching_adapter"] += 1

        if intent.action_kind is ActionKindV2.END_TURN:
            if runtime.adapter.state is None or runtime.adapter.state.phase_player != 0:
                failures["adapter_rejected_mutations"] += 1
                continue
            before_close = await runtime.environment.observe(0)
            if before_close.mandatory_action_kinds:
                failures["forced_turn_closures"] += 1
                failures["unresolved_mandatory_decisions"] += 1
                forced = True
            await runtime.adapter.end_phase(0, 1)
            accepted += 1
            accepted_mutations += 1
            continue

        result = await runtime.adapter.act(
            ActionCommand(
                tool=intent.action_kind.value,
                args=dict(intent.parameters),
                player_id=0,
                idempotency_key=f"{recipe.fixture_id}:{treatment.value}:{index}",
                lease_id=f"experiment:{recipe.fixture_id}:{treatment.value}",
            )
        )
        if result.status != "accepted":
            failures["adapter_rejected_mutations"] += 1
            continue
        accepted += 1
        accepted_mutations += 1
        if action is not None and runtime.adapter.state is not None \
                and runtime.adapter.state.phase_player == 0:
            audit_observation = await runtime.environment.observe(0)
            verification = verify_observable_postconditions(action, audit_observation)
            if verification is VerificationStatusV2.MISMATCHED:
                failures["unhandled_postcondition_divergences"] += 1

    if runtime.adapter.state is not None and runtime.adapter.state.phase_player == 0:
        before_forced = await runtime.environment.observe(0)
        failures["forced_turn_closures"] += 1
        forced = True
        if before_forced.mandatory_action_kinds:
            failures["unresolved_mandatory_decisions"] += 1
        await runtime.adapter.end_phase(0, 1)

    completed = runtime.adapter.state is not None and runtime.adapter.state.phase_player == -1
    return _row(
        recipe=recipe,
        treatment=treatment,
        proposal_sha256=sha256_hex(proposal_bytes),
        initial_observation_id=runtime.observation.observation_id,
        initial_graph_id=runtime.graph.graph_id,
        final_state_hash=runtime.adapter.state_hash(),
        attempted=attempted,
        accepted=accepted,
        detected_divergences=detected,
        completed=completed,
        termination="forced_closed" if forced else "completed",
        failures=failures,
    )


async def _run_dag_tx(
    recipe: FixtureRecipeV2,
    proposal_doc: Mapping[str, Any],
    policy: PolicyDescriptorV2,
) -> dict[str, Any]:
    runtime = await _open_fixture(recipe)
    proposal_bytes = canonical(dict(proposal_doc))
    proposal = TurnProposalV2.from_doc(dict(proposal_doc))
    receipt = await TransactionalExecutorV2(
        runtime.environment,
        policy,
        episode_id=f"{EXPERIMENT_ID}-{recipe.fixture_id}-D",
        max_replans_per_turn=2,
    ).execute(proposal, runtime.observation, runtime.graph)
    failures = _failure_counts()
    failures["adapter_rejected_mutations"] = sum(
        result.rejection_code is RejectionCodeV2.ADAPTER_REJECTED
        for result in receipt.results
    )
    failures["unresolved_mandatory_decisions"] = int(
        receipt.termination is TurnTerminationV2.MANDATORY_UNRESOLVED
    )
    detected = sum(result.status is ActionStatusV2.DIVERGED for result in receipt.results)
    accepted = sum(
        result.status in {ActionStatusV2.ACCEPTED, ActionStatusV2.DUPLICATE}
        for result in receipt.results
    )
    return _row(
        recipe=recipe,
        treatment=TreatmentV2.DAG_TX,
        proposal_sha256=sha256_hex(proposal_bytes),
        initial_observation_id=runtime.observation.observation_id,
        initial_graph_id=runtime.graph.graph_id,
        final_state_hash=runtime.monitor.state_hash(),
        attempted=len(receipt.authorizations),
        accepted=accepted,
        detected_divergences=detected,
        completed=receipt.termination is TurnTerminationV2.COMPLETED,
        termination=receipt.termination.value,
        failures=failures,
    )


def _requires_probe(
    observation: Any,
    legal: LegalActionSetV2,
) -> bool:
    fortify = [
        action for action in legal.actions if action.action_kind is ActionKindV2.FORTIFY
    ]
    source, base_target = next(
        (left, right)
        for left in fortify
        for right in fortify
        if left.actor != right.actor
    )
    requires = PreconditionClaimV2(
        source.actor,
        "fortified",
        PreconditionOperatorV2.EQ,
        KnowledgeValueV2.known(True, observed_turn=observation.turn),
    )
    target = LegalActionV2.create(
        base_target.action_kind,
        base_target.actor,
        observation.observation_id,
        target=base_target.target,
        parameters=dict(base_target.parameters),
        preconditions=[*base_target.preconditions, requires],
        resource_claims=base_target.resource_claims,
        expected_effects=base_target.expected_effects,
        affected_regions=base_target.affected_regions,
    )
    probe_set = LegalActionSetV2.create(observation.observation_id, [source, target])
    graph = ActionGraphCompilerV2()._compile_bound(observation, probe_set)
    return {edge.kind for edge in graph.edges} == {EdgeKindV2.REQUIRES}


async def build_fixture_manifest_v2(
    recipes: Sequence[FixtureRecipeV2] | None = None,
) -> dict[str, Any]:
    selected = tuple(recipes or fixture_recipes_v2())
    policy = experiment_policy_v2()
    entries: list[dict[str, Any]] = []
    coverage: dict[str, list[str]] = {kind.value: [] for kind in EdgeKindV2}
    failure_coverage: dict[str, list[str]] = {name: [] for name in ALL_FAILURE_CLASSES}
    for recipe in selected:
        state_doc = _fixture_state(recipe)
        runtime = await _open_fixture(recipe)
        proposal = deterministic_proposal_v2(runtime.observation, runtime.legal, policy)
        edge_kinds = sorted({edge.kind.value for edge in runtime.graph.edges})
        if recipe.fixture_id == "curated-019":
            if not _requires_probe(runtime.observation, runtime.legal):
                raise ContractError("REQUIRES claim-kernel coverage probe failed")
            edge_kinds.append(EdgeKindV2.REQUIRES.value)
            edge_kinds.sort()
        for edge_kind in edge_kinds:
            coverage[edge_kind].append(recipe.fixture_id)
        expected_failures = list(ALL_FAILURE_CLASSES[:3])
        expected_failures.append("unresolved_mandatory_decisions")
        if recipe.fault is FixtureFaultV2.DROP_RESEARCH_EFFECT:
            expected_failures.append("unhandled_postcondition_divergences")
        for failure in expected_failures:
            failure_coverage[failure].append(recipe.fixture_id)
        proposal_doc = proposal.to_doc()
        entries.append(
            {
                **recipe.to_doc(),
                "state_sha256": sha256_hex(canonical(state_doc)),
                "observation_id": runtime.observation.observation_id,
                "legal_action_set_id": runtime.legal.legal_action_set_id,
                "graph_id": runtime.graph.graph_id,
                "edge_coverage": edge_kinds,
                "failure_coverage": sorted(set(expected_failures)),
                "proposal_sha256": sha256_hex(canonical(proposal_doc)),
                "proposal": proposal_doc,
            }
        )
    body = {
        "schema": MANIFEST_SCHEMA,
        "experiment_id": EXPERIMENT_ID,
        "fixture_counts": {
            "generated": sum(row["fixture_class"] == "generated" for row in entries),
            "curated": sum(row["fixture_class"] == "curated" for row in entries),
            "total": len(entries),
        },
        "policy": policy.to_doc(),
        "edge_coverage": {kind: sorted(ids) for kind, ids in sorted(coverage.items())},
        "failure_coverage": {
            name: sorted(ids) for name, ids in sorted(failure_coverage.items())
        },
        "fixtures": entries,
    }
    return {
        "schema": MANIFEST_SCHEMA,
        "manifest_sha256": sha256_hex(canonical(body)),
        "body": body,
    }


def validate_fixture_manifest_v2(
    doc: Mapping[str, Any],
    *,
    require_full_corpus: bool = True,
) -> dict[str, Any]:
    if set(doc) != {"schema", "manifest_sha256", "body"} or doc.get("schema") != 2:
        raise ContractError("fixture manifest envelope is invalid")
    body = doc["body"]
    if not isinstance(body, dict):
        raise ContractError("fixture manifest body must be an object")
    if set(body) != {
        "schema",
        "experiment_id",
        "fixture_counts",
        "policy",
        "edge_coverage",
        "failure_coverage",
        "fixtures",
    }:
        raise ContractError("fixture manifest body has unknown or missing fields")
    if body["schema"] != MANIFEST_SCHEMA:
        raise ContractError("fixture manifest body has the wrong schema")
    if sha256_hex(canonical(body)) != doc["manifest_sha256"]:
        raise ContractError("fixture manifest digest mismatch")
    if body.get("experiment_id") != EXPERIMENT_ID:
        raise ContractError("fixture manifest names the wrong experiment")
    fixtures = body.get("fixtures")
    if not isinstance(fixtures, list) or not fixtures:
        raise ContractError("fixture manifest must contain fixtures")
    ids = [row.get("fixture_id") for row in fixtures if isinstance(row, dict)]
    if len(ids) != len(fixtures) or len(set(ids)) != len(ids):
        raise ContractError("fixture ids must be present and unique")
    actual_counts = {
        "generated": sum(row.get("fixture_class") == "generated" for row in fixtures),
        "curated": sum(row.get("fixture_class") == "curated" for row in fixtures),
        "total": len(fixtures),
    }
    if body["fixture_counts"] != actual_counts:
        raise ContractError("fixture counts do not match the manifest inventory")
    policy = PolicyDescriptorV2.from_doc(body["policy"])
    if any(kind.value not in body["edge_coverage"] for kind in EdgeKindV2):
        raise ContractError("fixture manifest does not cover every edge kind")
    if require_full_corpus:
        counts = body.get("fixture_counts")
        if counts != {
            "generated": GENERATED_FIXTURES,
            "curated": CURATED_FIXTURES,
            "total": GENERATED_FIXTURES + CURATED_FIXTURES,
        }:
            raise ContractError("full fixture manifest must contain exactly 80+20 cases")
        if any(not body["edge_coverage"][kind.value] for kind in EdgeKindV2):
            raise ContractError("fixture manifest has an empty edge coverage class")
    if set(body["failure_coverage"]) != set(ALL_FAILURE_CLASSES):
        raise ContractError("fixture manifest failure coverage is incomplete")
    if require_full_corpus and any(
        not body["failure_coverage"][name] for name in ALL_FAILURE_CLASSES
    ):
        raise ContractError("fixture manifest has an empty failure coverage class")
    for row in fixtures:
        if set(row) != {
            "fixture_id",
            "fixture_class",
            "seed",
            "variant",
            "fault",
            "state_sha256",
            "observation_id",
            "legal_action_set_id",
            "graph_id",
            "edge_coverage",
            "failure_coverage",
            "proposal_sha256",
            "proposal",
        }:
            raise ContractError("fixture entry has unknown or missing fields")
        recipe = FixtureRecipeV2.from_doc(
            {key: row[key] for key in ("fixture_id", "fixture_class", "seed", "variant", "fault")}
        )
        proposal = TurnProposalV2.from_doc(row["proposal"])
        if proposal.policy_id != policy.descriptor_id:
            raise ContractError("fixture proposal policy identity mismatch")
        if proposal.observation_id != row["observation_id"]:
            raise ContractError("fixture proposal observation identity mismatch")
        if sha256_hex(canonical(proposal.to_doc())) != row["proposal_sha256"]:
            raise ContractError("fixture proposal digest mismatch")
        if recipe.fixture_class != row["fixture_class"]:
            raise ContractError("fixture recipe class mismatch")
        for field in (
            "state_sha256",
            "observation_id",
            "legal_action_set_id",
            "graph_id",
            "proposal_sha256",
        ):
            value = row[field]
            if (
                not isinstance(value, str)
                or len(value) != 64
                or any(character not in "0123456789abcdef" for character in value)
            ):
                raise ContractError(f"fixture {field} is not a SHA-256 digest")
        if sorted(set(row["edge_coverage"])) != row["edge_coverage"]:
            raise ContractError("fixture edge coverage must be unique and sorted")
        if sorted(set(row["failure_coverage"])) != row["failure_coverage"]:
            raise ContractError("fixture failure coverage must be unique and sorted")
        if any(
            recipe.fixture_id not in body["edge_coverage"][kind]
            for kind in row["edge_coverage"]
        ):
            raise ContractError("fixture edge coverage disagrees with the aggregate")
        if any(
            recipe.fixture_id not in body["failure_coverage"][failure]
            for failure in row["failure_coverage"]
        ):
            raise ContractError("fixture failure coverage disagrees with the aggregate")
    return copy.deepcopy(body)


async def run_fixture_v2(
    fixture_entry: Mapping[str, Any],
    policy: PolicyDescriptorV2,
) -> list[dict[str, Any]]:
    recipe = FixtureRecipeV2.from_doc(
        {
            key: fixture_entry[key]
            for key in ("fixture_id", "fixture_class", "seed", "variant", "fault")
        }
    )
    if sha256_hex(canonical(_fixture_state(recipe))) != fixture_entry["state_sha256"]:
        raise ContractError("fixture state digest mismatch")
    proposal_doc = fixture_entry["proposal"]
    rows = [
        await _run_control(recipe, proposal_doc, treatment)
        for treatment in (
            TreatmentV2.SEQUENTIAL,
            TreatmentV2.LEGAL_LIST,
            TreatmentV2.DAG,
        )
    ]
    rows.append(await _run_dag_tx(recipe, proposal_doc, policy))
    proposal_digests = {row["proposal_sha256"] for row in rows}
    if proposal_digests != {fixture_entry["proposal_sha256"]}:
        raise ContractError("treatments did not consume identical proposal bytes")
    for row in rows:
        if row["initial_observation_id"] != fixture_entry["observation_id"]:
            raise ContractError("treatment fixture observation drifted")
        if row["initial_graph_id"] != fixture_entry["graph_id"]:
            raise ContractError("treatment fixture graph drifted")
    return rows


async def run_experiment_v2(
    manifest_doc: Mapping[str, Any],
    *,
    limit: int | None = None,
    require_full_corpus: bool = True,
) -> dict[str, Any]:
    body = validate_fixture_manifest_v2(
        manifest_doc,
        require_full_corpus=require_full_corpus,
    )
    fixtures = body["fixtures"]
    if limit is not None:
        if type(limit) is not int or not 1 <= limit <= len(fixtures):
            raise ContractError("experiment limit is outside the fixture corpus")
        fixtures = fixtures[:limit]
    policy = PolicyDescriptorV2.from_doc(body["policy"])
    rows: list[dict[str, Any]] = []
    for fixture in fixtures:
        rows.extend(await run_fixture_v2(fixture, policy))
    totals = {
        treatment.value: sum(
            row["control_failure_total"]
            for row in rows
            if row["treatment"] == treatment.value
        )
        for treatment in TreatmentV2
    }
    d_total = totals[TreatmentV2.DAG_TX.value]
    ordering_holds = (
        d_total < totals[TreatmentV2.SEQUENTIAL.value]
        and d_total < totals[TreatmentV2.LEGAL_LIST.value]
        and d_total <= totals[TreatmentV2.DAG.value]
    )
    result_body = {
        "schema": MANIFEST_SCHEMA,
        "experiment_id": EXPERIMENT_ID,
        "fixture_manifest_sha256": manifest_doc["manifest_sha256"],
        "proposal_source": "deterministic_observation_only",
        "fixtures_run": len(fixtures),
        "rows": rows,
        "control_failure_totals": totals,
        "deterministic_ordering_holds": ordering_holds,
        "deterministic_verdict": (
            "D_STRICTLY_BELOW_A_B_AND_NO_WORSE_THAN_C"
            if ordering_holds
            else "ORDERING_NOT_ESTABLISHED"
        ),
    }
    return {
        "schema": MANIFEST_SCHEMA,
        "result_sha256": sha256_hex(canonical(result_body)),
        "body": result_body,
    }


def write_immutable_json(path: Path | str, doc: Mapping[str, Any]) -> None:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    raw = canonical(dict(doc)) + "\n"
    if destination.exists():
        if destination.is_symlink() or destination.read_text(encoding="utf-8") != raw:
            raise ContractError("immutable experiment artifact already exists with other bytes")
        return
    atomic_write_text(destination, raw, fsync=True)
