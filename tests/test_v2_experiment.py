"""CAR-108: immutable identical-proposal A/B/C/D experiment harness."""

from __future__ import annotations

import copy
from pathlib import Path

import pytest

from civ_arena.canonical import canonical
from civ_arena.experiments.turn_core import (
    ALL_FAILURE_CLASSES,
    CURATED_FIXTURES,
    GENERATED_FIXTURES,
    FixtureFaultV2,
    TreatmentV2,
    build_fixture_manifest_v2,
    fixture_recipes_v2,
    run_experiment_v2,
    validate_fixture_manifest_v2,
    write_immutable_json,
)
from civ_arena.v2.contracts import EdgeKindV2
from civ_arena.v2.schemas import ContractError


def test_fixture_recipe_corpus_is_exactly_preregistered_80_plus_20() -> None:
    first = fixture_recipes_v2()
    second = fixture_recipes_v2()
    assert first == second
    assert len(first) == GENERATED_FIXTURES + CURATED_FIXTURES == 100
    assert sum(item.fixture_class == "generated" for item in first) == 80
    assert sum(item.fixture_class == "curated" for item in first) == 20
    assert len({item.fixture_id for item in first}) == 100
    assert len({item.seed for item in first}) == 100
    assert sum(item.fault is FixtureFaultV2.DROP_RESEARCH_EFFECT for item in first) == 8
    assert sum(item.fault is FixtureFaultV2.REJECT_FORTIFY for item in first) == 4


@pytest.mark.asyncio
async def test_fixture_manifest_is_canonical_digest_bound_and_tamper_evident(
    tmp_path: Path,
) -> None:
    recipe = fixture_recipes_v2()[0]
    first = await build_fixture_manifest_v2((recipe,))
    second = await build_fixture_manifest_v2((recipe,))
    assert canonical(first) == canonical(second)
    body = validate_fixture_manifest_v2(first, require_full_corpus=False)
    assert body["fixture_counts"] == {"generated": 1, "curated": 0, "total": 1}
    proposal = body["fixtures"][0]["proposal"]
    assert proposal["observation_id"] == body["fixtures"][0]["observation_id"]

    path = tmp_path / "fixtures.json"
    write_immutable_json(path, first)
    write_immutable_json(path, first)
    assert path.read_text(encoding="utf-8") == canonical(first) + "\n"

    tampered = copy.deepcopy(first)
    tampered["body"]["fixtures"][0]["seed"] += 1
    with pytest.raises(ContractError, match="digest mismatch"):
        validate_fixture_manifest_v2(tampered, require_full_corpus=False)
    with pytest.raises(ContractError, match="other bytes"):
        write_immutable_json(path, tampered)


@pytest.mark.asyncio
async def test_all_edge_and_failure_classes_have_curated_manifest_coverage() -> None:
    recipes = fixture_recipes_v2()
    selected = (recipes[80], recipes[88], recipes[99])
    manifest = await build_fixture_manifest_v2(selected)
    body = validate_fixture_manifest_v2(manifest, require_full_corpus=False)
    assert {
        kind for kind, fixture_ids in body["edge_coverage"].items() if fixture_ids
    } == {kind.value for kind in EdgeKindV2}
    assert {
        name for name, fixture_ids in body["failure_coverage"].items() if fixture_ids
    } == set(ALL_FAILURE_CLASSES)
    assert "curated-019" in body["edge_coverage"]["REQUIRES"]
    assert "curated-008" in body["edge_coverage"]["CONSUMES_SHARED_RESOURCE"]


@pytest.mark.asyncio
async def test_identical_proposal_normal_fixture_establishes_expected_ordering() -> None:
    manifest = await build_fixture_manifest_v2((fixture_recipes_v2()[0],))
    result = await run_experiment_v2(manifest, require_full_corpus=False)
    rows = result["body"]["rows"]
    assert {row["proposal_sha256"] for row in rows} == {
        manifest["body"]["fixtures"][0]["proposal_sha256"]
    }
    assert result["body"]["control_failure_totals"] == {
        TreatmentV2.SEQUENTIAL.value: 5,
        TreatmentV2.LEGAL_LIST.value: 2,
        TreatmentV2.DAG.value: 2,
        TreatmentV2.DAG_TX.value: 0,
    }
    assert result["body"]["deterministic_ordering_holds"] is True
    assert next(
        row for row in rows if row["treatment"] == TreatmentV2.DAG_TX.value
    )["turn_completed"] is True


@pytest.mark.asyncio
async def test_dag_tx_handles_declared_divergence_without_counting_it_as_failure() -> None:
    recipe = fixture_recipes_v2()[80]
    assert recipe.fault is FixtureFaultV2.DROP_RESEARCH_EFFECT
    manifest = await build_fixture_manifest_v2((recipe,))
    result = await run_experiment_v2(manifest, require_full_corpus=False)
    by_treatment = {
        row["treatment"]: row for row in result["body"]["rows"]
    }
    d = by_treatment[TreatmentV2.DAG_TX.value]
    assert d["termination"] == "failed"
    assert d["detected_postcondition_divergences"] == 1
    assert d["control_failure_total"] == 0
    for treatment in (TreatmentV2.LEGAL_LIST, TreatmentV2.DAG):
        assert (
            by_treatment[treatment.value]["failures"]
            ["unhandled_postcondition_divergences"]
            == 1
        )


def test_unsafe_controls_are_not_reachable_from_production_match_module() -> None:
    root = Path(__file__).parents[1]
    production = (root / "src/civ_arena/match.py").read_text(encoding="utf-8")
    config = (root / "src/civ_arena/config.py").read_text(encoding="utf-8")
    assert "civ_arena.experiments" not in production
    assert "control treatments are experiment-harness only" in config
    direct_calls = []
    for path in sorted((root / "src/civ_arena").rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        if "runtime.adapter.act(" in text:
            direct_calls.append(path.relative_to(root).as_posix())
    assert direct_calls == ["src/civ_arena/experiments/turn_core.py"]

