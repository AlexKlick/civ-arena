"""M20a: the league / self-play harness and the generalized analyze arms.

Hermetic: every Arena run is budget 4, 8 turns, under tmp_path. The
milestone's REPLAY OK carrier is test_selfplay_zero_rejections_and_replay_ok
— a real planner-vs-planner self-play match replayed in-process from its
own emitted config.yaml. Codex round pins: the (pair, seed) CLUSTER is the
inferential unit (A1), match dirs refuse re-entry (A3), variant names stay
injective (A5), the results doc is integer-valued and timing-free (A2/A10),
and planner_analyze drops dirty pairs, surfaces unmatched keys, and
refuses identical arms (A7/A8/A9).
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from typing import Any

import pytest

from civ_arena.replay import replay_run
from civ_arena.v1_compat import load_v1_config_read_only

REPO = Path(__file__).resolve().parents[1]

POP = {"mcts-b4": ("mcts", 4), "mcgs-b4": ("mcgs", 4)}


def _load(name: str) -> Any:
    spec = importlib.util.spec_from_file_location(
        name, REPO / "scripts" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


def _fabricated(match_id: str, a: str, b: str, seed: int, seating: str,
                diff_p0: int, *, violations: int = 0, rej0: int = 0,
                rej1: int = 0) -> dict:
    """A league-schema row without running a match (validity-gate tests)."""
    p0, p1 = (a, b) if seating == "ab" else (b, a)
    return {
        "match_id": match_id, "arm": "-vs-".join(sorted((a, b))),
        "seating": seating, "variant_p0": p0, "variant_p1": p1, "seed": seed,
        "budget_p0": 4, "budget_p1": 4, "method_p0": "mcts", "method_p1": "mcts",
        "turns": 8, "violations": violations,
        "rejections_p0": rej0, "rejections_p1": rej1,
        "value_differential_p0": diff_p0, "value_differential_p1": -diff_p0,
        "decisions_p0": 3, "decisions_p1": 3,
        "chosen_p0": [], "chosen_p1": [], "wall_ms": 1,
    }


def _assert_no_floats(node: Any) -> None:
    """A10: the results doc's contract is exact integers — no float ever."""
    if isinstance(node, float):
        raise AssertionError(f"float leaked into the deterministic doc: {node}")
    if isinstance(node, dict):
        for v in node.values():
            _assert_no_floats(v)
    elif isinstance(node, list):
        for v in node:
            _assert_no_floats(v)


async def test_league_variants_share_seeds_paired(tmp_path):
    league = _load("league")
    # override syntax: ints parse as ints, strings stay strings
    assert league.parse_variant("probe:method=mcgs,budget=9") == (
        "probe", {"method": "mcgs", "budget": 9})
    # an existing name PATCHES its kwargs (a budget override must never
    # silently flip the method); an unknown name is a probe against the
    # explicit PROBE_BASE, normalized to COMPLETE effective kwargs
    resolved = dict(league.resolve_population(
        ["mcts-b8:budget=4", "probe:method=mcgs"]))
    assert resolved["mcts-b8"] == {"method": "mcts", "budget": 4}
    assert resolved["mcgs-b32"] == {"method": "mcgs", "budget": 32}
    assert resolved["probe"] == {"method": "mcgs", "budget": 12}
    # A5: names that would collide in match ids / per_pair keys refuse
    for bad in ("x-vs-y:method=mcts", "a/b:budget=4"):
        with pytest.raises(ValueError, match="-vs-|/"):
            league.resolve_population([bad])
    # A4: an armed case_base serializes as its artifact sha256, and the
    # manifest still carries the COMPLETE effective kwargs
    name, kw = league.parse_variant("probe:case_base=configs/casebase-m19b.json")
    resolved = dict(league.resolve_population(
        ["probe:case_base=configs/casebase-m19b.json"]))
    manifest = league.population_manifest([(name, resolved[name])])
    sha = manifest[0]["kwargs"]["case_base"]["artifact_sha256"]
    assert isinstance(sha, str) and len(sha) == 64
    assert manifest[0]["kwargs"]["method"] == "mcts"  # PROBE_BASE filled in
    assert kw["case_base"].artifact_sha256 == sha

    doc = await league.run_league(
        tmp_path, [(n, {"method": m, "budget": b})
                   for n, (m, b) in POP.items()],
        seeds=1, turns=8)
    rows = doc["results"]
    assert len(rows) == 2  # one unordered pair, BOTH seatings
    r_ab = next(r for r in rows if r["seating"] == "ab")
    r_ba = next(r for r in rows if r["seating"] == "ba")

    # same seed, same pair; only the seat assignment differs
    assert r_ab["seed"] == r_ba["seed"] == league.SEED_BASE
    assert r_ab["arm"] == r_ba["arm"]
    assert r_ab["variant_p0"] == r_ba["variant_p1"]
    assert r_ab["variant_p1"] == r_ba["variant_p0"]
    for r in rows:  # each seat carries ITS variant's kwargs
        assert POP[r["variant_p0"]] == (r["method_p0"], r["budget_p0"])
        assert POP[r["variant_p1"]] == (r["method_p1"], r["budget_p1"])

    # the two MatchSpecs are identical apart from match_id (seat seeds 7/22)
    s_ab = league.spec_for(r_ab["match_id"], league.SEED_BASE, 8)
    s_ba = league.spec_for(r_ba["match_id"], league.SEED_BASE, 8)
    assert s_ab.agents == s_ba.agents
    assert s_ab.seed == s_ba.seed and s_ab.max_turns == s_ba.max_turns

    # match dirs are distinct and both really ran
    assert r_ab["match_id"] != r_ba["match_id"]
    assert len({r["match_id"] for r in rows}) == 2
    for r in rows:
        assert (tmp_path / r["match_id"] / "events.jsonl").exists()


def test_league_validity_gate_blocks_dirty_rows():
    league = _load("league")
    # both dirtiness flavors: a watchdog violation, a seat rejection
    assert league.is_dirty(_fabricated("d1", "va", "vb", 9, "ab", 0,
                                       violations=1))
    assert league.is_dirty(_fabricated("d2", "va", "vb", 9, "ab", 0, rej0=1))
    assert league.is_dirty(_fabricated("d3", "va", "vb", 9, "ab", 0, rej1=1))
    assert not league.is_dirty(_fabricated("d4", "va", "vb", 9, "ab", 0))

    rows = [
        # seed 1: va wins BOTH complementary seatings -> cluster FOR va
        _fabricated("lg-va-vs-vb-s1-ab", "va", "vb", 1, "ab", +12),
        _fabricated("lg-va-vs-vb-s1-ba", "va", "vb", 1, "ba", -8),
        # seed 2: dirty mate (violation) strands its CLEAN twin -> SPLIT,
        # never a lone-Bernoulli inference
        _fabricated("lg-va-vs-vb-s2-ab", "va", "vb", 2, "ab", -99,
                    violations=1),
        _fabricated("lg-va-vs-vb-s2-ba", "va", "vb", 2, "ba", -8),
        # seed 3: 1-1 -> SPLIT
        _fabricated("lg-va-vs-vb-s3-ab", "va", "vb", 3, "ab", +12),
        _fabricated("lg-va-vs-vb-s3-ba", "va", "vb", 3, "ba", +7),
        # seed 4: tie + win -> SPLIT
        _fabricated("lg-va-vs-vb-s4-ab", "va", "vb", 4, "ab", 0),
        _fabricated("lg-va-vs-vb-s4-ba", "va", "vb", 4, "ba", -3),
        # seed 5: vb wins both seatings -> cluster AGAINST va
        _fabricated("lg-va-vs-vb-s5-ab", "va", "vb", 5, "ab", -6),
        _fabricated("lg-va-vs-vb-s5-ba", "va", "vb", 5, "ba", +7),
    ]
    per_pair, text = league.head_to_head(rows)
    entry = per_pair["va-vs-vb"]
    # inferential unit = complete (pair, seed) clusters only
    assert entry["per_seed"] == {"for": 1, "against": 1, "split": 3}
    assert entry["p_num"] == 1 and entry["p_den"] == 2
    assert entry["clean"] == 9 and entry["matches"] == 10
    assert entry["dirty"] == ["lg-va-vs-vb-s2-ab"]
    assert "DIRTY: lg-va-vs-vb-s2-ab" in text
    assert "seed clusters (inferential): for=1 against=1 split=3" in text
    assert "(1/2)" in text  # p_num/p_den render beside the float p
    # descriptive fields survive, clearly labeled non-inferential
    assert entry["match_wins_descriptive"] == {"va": 5, "vb": 3}
    assert entry["match_ties_descriptive"] == 1
    assert entry["per_seating_descriptive"]["va@p0"] == {
        "va": 2, "vb": 1, "ties": 1}
    assert entry["diff_sum"] == {"va": 23, "vb": -23}
    assert entry["diff_count"] == 9

    agg = league.aggregate(rows, ["va", "vb"])
    assert agg["va"]["matches"] == 9 and agg["va"]["wins"] == 5
    assert agg["va"]["losses"] == 3 and agg["va"]["ties"] == 1
    assert agg["va"]["diff_sum"] == 23
    assert agg["va"]["dirty_matches"] == 1 and agg["vb"]["dirty_matches"] == 1


async def test_league_results_schema(tmp_path):
    league = _load("league")
    doc = await league.run_league(
        tmp_path, [(n, {"method": m, "budget": b})
                   for n, (m, b) in POP.items()],
        seeds=1, turns=8)
    assert set(doc) == {"population", "results", "per_pair", "aggregate"}
    on_disk = json.loads((tmp_path / "league-results.json").read_text())
    assert set(on_disk) == set(doc)
    _assert_no_floats(on_disk)  # A10: integer contract, no floats anywhere

    # A4: the manifest carries every variant's COMPLETE effective kwargs
    manifest = {e["name"]: e["kwargs"] for e in on_disk["population"]}
    assert manifest == {n: {"method": m, "budget": b}
                        for n, (m, b) in POP.items()}

    assert len(on_disk["per_pair"]) == 1
    assert set(on_disk["aggregate"]) == set(POP)
    for entry in on_disk["per_pair"].values():
        assert isinstance(entry["p_num"], int) \
            and isinstance(entry["p_den"], int)
        assert "p_one_sided" not in entry
        assert "mean_differential" not in entry  # -> diff_sum/diff_count
        assert "win_rate" not in entry
    for agg in on_disk["aggregate"].values():
        assert isinstance(agg["wins"], int) and isinstance(agg["diff_sum"], int)
        assert "win_rate" not in agg
        assert "value_differential_mean" not in agg
        assert "wall_ms_total" not in agg

    for r in on_disk["results"]:
        for key in ("arm", "seating", "variant_p0", "variant_p1", "seed",
                    "value_differential_p0", "value_differential_p1",
                    "rejections_p0", "rejections_p1", "violations", "turns"):
            assert key in r, f"{r['match_id']}: missing {key}"
        assert "wall_ms" not in r  # A2: timing never enters the doc
        # 2-player self-play: the seats' differentials are sign flips
        assert r["value_differential_p0"] == -r["value_differential_p1"]
        run_dir = tmp_path / r["match_id"]
        assert (run_dir / "config.yaml").exists()
        assert (run_dir / "planner" / "p0-trace.json").exists()
        assert (run_dir / "planner" / "p1-trace.json").exists()
        # the emitted config re-loads into the EXACT spec the match ran
        assert load_v1_config_read_only(run_dir / "config.yaml") == league.spec_for(
            r["match_id"], r["seed"], 8)
        t0 = json.loads((run_dir / "planner" / "p0-trace.json").read_text())
        t1 = json.loads((run_dir / "planner" / "p1-trace.json").read_text())
        assert t0 and t1  # both seats really searched
        assert len(t0) == r["decisions_p0"]
        assert len(t1) == r["decisions_p1"]

    # A2: wall-clock lives in the NON-contractual telemetry sidecar only
    telemetry = json.loads((tmp_path / "telemetry.json").read_text())
    assert "NON-CONTRACTUAL" in telemetry["note"]
    assert set(telemetry["wall_ms"]) == {r["match_id"]
                                         for r in on_disk["results"]}


async def test_selfplay_zero_rejections_and_replay_ok(tmp_path):
    league = _load("league")
    kwargs = {"method": "mcts", "budget": 4}, {"method": "mcgs", "budget": 4}
    row = await league.run_league_match(
        tmp_path, "mcts-b4", kwargs[0], "mcgs-b4", kwargs[1],
        league.SEED_BASE, 8)
    assert row["violations"] == 0
    assert row["rejections_p0"] == 0 and row["rejections_p1"] == 0
    assert row["turns"] == 8

    # A3: a re-run into the same non-empty match dir refuses BEFORE spend
    with pytest.raises(SystemExit, match=row["match_id"]):
        await league.run_league_match(
            tmp_path, "mcts-b4", kwargs[0], "mcgs-b4", kwargs[1],
            league.SEED_BASE, 8)

    run_dir = tmp_path / row["match_id"]
    spec = load_v1_config_read_only(
        run_dir / "config.yaml"
    )  # the emitted V1 compatibility config drives it
    result = await replay_run(run_dir, spec, tmp_path / "replay")
    assert result["identical"], (
        f"self-play replay diverged at comparable-event "
        f"{result['first_divergence']}")
    assert result["live_final_hash"] == result["replayed_final_hash"]


# --------------------------------------------- planner_analyze arm flags

# (seed, side) -> (arm_a diff, arm_b diff, arm_b transposition_hits);
# arm_b wins 3 of 4 decidable pairs
DIFFS: dict[tuple[int, int], tuple[int, int, int]] = {
    (101, 0): (10, 25, 6),
    (101, 1): (5, 20, 2),
    (202, 0): (12, 3, 4),
    (202, 1): (4, 9, 8),
}


def _fixture_rows(arm_a: str, arm_b: str,
                  dirty: tuple[int, int, str] | None = None) -> list[dict]:
    """planner-experiment-schema rows (two arms, paired seed x side);
    `dirty` names one (seed, side, arm) row to mark dirty."""
    rows = []
    for (seed, side), (da, db, tp) in DIFFS.items():
        for arm, d in ((arm_a, da), (arm_b, db)):
            rows.append({
                "match_id": f"{arm}-s{seed}-p{side}", "arm": arm,
                "seed": seed, "side": side, "budget": 16, "turns": 20,
                "violations": 1 if dirty == (seed, side, arm) else 0,
                "planner_rejections": 0,
                "value_differential": d,
                "transposition_hits": tp if arm == arm_b else 0,
            })
    return rows


def _write_fixture(path: Path, rows: list[dict]) -> Path:
    path.write_text(json.dumps({"results": rows}) + "\n")
    return path


def test_planner_analyze_arms_flag_backcompat(tmp_path, capsys):
    pa = _load("planner_analyze")

    fx = _write_fixture(tmp_path / "planner-experiment.json",
                        _fixture_rows("planner-mcts", "planner-mcgs"))
    pa.analyze(str(fx))
    out = capsys.readouterr().out
    # default arms reproduce the exp3 output shape and numbers exactly
    assert "   pairs=4 (mcgs wins 3 / mcts wins 1 / ties 0)" in out
    assert (f"   one-sided exact binomial p (H3: mcgs>mcts) = "
            f"{pa.binom_p_ge(4, 3):.4f}") in out
    assert "   side 0: mcgs 1 / mcts 1 / tie 0" in out
    assert "   side 1: mcgs 2 / mcts 0 / tie 0" in out
    assert "   mean tp_hits: mcgs-won=" in out
    # clean, fully-paired data adds no incompleteness markers
    assert "ANALYSIS INCOMPLETE" not in out

    fx2 = _write_fixture(tmp_path / "renamed-arms.json",
                         _fixture_rows("mcts-b16", "mcts-b8"))
    pa.analyze(str(fx2))  # default arms pair nothing on renamed arms
    assert "pairs=0" in capsys.readouterr().out
    pa.analyze(str(fx2), arm_a="mcts-b16", arm_b="mcts-b8")
    out3 = capsys.readouterr().out
    assert "   pairs=4 (mcts-b8 wins 3 / mcts-b16 wins 1 / ties 0)" in out3
    assert "(H3: mcts-b8>mcts-b16)" in out3


def test_planner_analyze_dirty_exclusion_unmatched_identical_arms(
        tmp_path, capsys):
    pa = _load("planner_analyze")

    # A9: identical arms refuse (usage error)
    fx = _write_fixture(tmp_path / "fx.json",
                        _fixture_rows("planner-mcts", "planner-mcgs"))
    with pytest.raises(SystemExit) as ei:
        pa.main(["--arm-a", "m", "--arm-b", "m", str(fx)])
    assert ei.value.code == 2

    # A7: a dirty member drops its WHOLE key from the paired analysis
    fx7 = _write_fixture(tmp_path / "dirty-member.json",
                         _fixture_rows("planner-mcts", "planner-mcgs",
                                       dirty=(202, 0, "planner-mcgs")))
    pa.analyze(str(fx7))
    out = capsys.readouterr().out
    assert "   pairs=3 (mcgs wins 3 / mcts wins 0 / ties 0)" in out
    assert ("   EXCLUDED pairs (dirty member, both rows dropped): "
            "1 [(202, 0)]") in out
    assert "   ANALYSIS INCOMPLETE" in out

    # A8: a key carrying only one arm surfaces as unmatched
    rows8 = [r for r in _fixture_rows("planner-mcts", "planner-mcgs")
             if not (r["seed"] == 202 and r["side"] == 1
                     and r["arm"] == "planner-mcgs")]
    fx8 = _write_fixture(tmp_path / "unmatched-key.json", rows8)
    pa.analyze(str(fx8))
    out8 = capsys.readouterr().out
    assert "   pairs=3 (mcgs wins 2 / mcts wins 1 / ties 0)" in out8
    assert ("   UNMATCHED keys (arm on one side only): 1 "
            "[(202, 1)]") in out8
    assert "   ANALYSIS INCOMPLETE" in out8
