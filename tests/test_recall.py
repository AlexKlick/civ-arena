"""RecallCorpus: an agent's own prior-match lessons, deterministic lexical
retrieval, built straight from prior event logs (the trust root).
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any

import pytest

from civ_arena.recall import LESSON_LIMIT, RecallCorpus

# ---------------------------------------------------------------- helpers


def _write_run(run_dir: Path, match_id: str) -> Path:
    """A finished prior match whose roman agent wrote three lessons; the
    turtler wrote one (scoped out of roman's recall)."""
    records: list[dict[str, Any]] = [
        {"kind": "MATCH_START", "seq": 0, "match_id": match_id,
         "game_instance_id": "g", "turn": 0, "phase_player_id": -1,
         "player_id": None, "agent_id": None, "visibility_scope": "referee",
         "config": {"agents": [["roman", 0, "llm"],
                               ["korean-turtler", 1, "turtler"]]}},
    ]
    records += _pair("record_lesson",
                     {"text": "Axial coordinate neighbors exclude (q+1,r+1) "
                              "— hex grids have six neighbors"},
                     1, 0, 4, "roman", match_id)
    records += _pair("record_lesson",
                     {"text": "Found cities on grassland near fresh water "
                              "for growth"}, 3, 0, 8, "roman", match_id)
    records += _pair("record_lesson",
                     {"text": "Archers behind warriors win fights; stacks "
                              "work"}, 5, 0, 12, "roman", match_id)
    records += _pair("record_lesson",
                     {"text": "Turtle behind walls and never leave home"},
                     7, 1, 15, "korean-turtler", match_id)
    records.append({"kind": "MATCH_END", "seq": 9, "match_id": match_id,
                    "game_instance_id": "g", "turn": 20,
                    "phase_player_id": -1, "player_id": None,
                    "agent_id": None, "visibility_scope": "referee",
                    "summary": {"final_turn": 20, "scores": {}}})
    d = run_dir / match_id
    d.mkdir(parents=True)
    (d / "events.jsonl").write_text(
        "".join(json.dumps(r, sort_keys=True) + "\n" for r in records))
    return d


def _pair(tool: str, args: dict[str, Any], seq: int, pid: int, turn: int,
          agent: str, match_id: str) -> list[dict[str, Any]]:
    def base(kind: str, **extra: Any) -> dict[str, Any]:
        return {"kind": kind, "seq": seq if kind == "TOOL_CALL" else seq + 1,
                "match_id": match_id, "game_instance_id": "g", "turn": turn,
                "phase_player_id": pid, "player_id": pid, "agent_id": agent,
                "visibility_scope": "private_player", **extra}
    return [
        base("TOOL_CALL", tool=tool, args=args),
        base("TOOL_RESULT", tool=tool, status="accepted"),
    ]


def _corpus(tmp_path: Path, priors: list[str], self_id: str = "self-match",
            **kw: Any) -> RecallCorpus:
    for prior in set(priors) - {"self-match"}:
        _write_run(tmp_path, prior, **kw)
    return RecallCorpus.from_runs(tmp_path, self_id, priors)


# ------------------------------------------------------------------ corpus


def test_corpus_extracts_own_agent_lessons_from_prior_logs(tmp_path: Path):
    corpus = _corpus(tmp_path, ["prior-a"])
    assert corpus.size("roman") == 3
    assert corpus.size("korean-turtler") == 1  # present, but scoped out
    assert corpus.size() == 4


def test_corpus_excludes_self_and_dedupes_prior_ids(tmp_path: Path):
    corpus = _corpus(tmp_path, ["prior-a", "prior-a", "self-match"])
    assert corpus.size() == 4  # prior-a counted once; self-match never read
    assert all(e["match_id"] == "prior-a" for e in corpus.query(
        "roman", "cities grassland"))


def test_corpus_missing_run_fails_loudly(tmp_path: Path):
    with pytest.raises(ValueError, match="missing run 'nope'"):
        RecallCorpus.from_runs(tmp_path, "self", ["nope"])


def test_corpus_rejects_log_without_roster(tmp_path: Path):
    d = tmp_path / "empty-run"
    d.mkdir()
    (d / "events.jsonl").write_text(
        json.dumps({"kind": "TURN_END", "seq": 0, "match_id": "empty-run",
                    "turn": 1}) + "\n")
    with pytest.raises(ValueError, match="no MATCH_START roster"):
        RecallCorpus.from_runs(tmp_path, "self", ["empty-run"])


def test_corpus_requires_intact_seq(tmp_path: Path):
    # the strict loader discipline: a prior log with broken seq fails
    # closed instead of yielding a silently-wrong corpus
    _write_run(tmp_path, "broken")
    path = tmp_path / "broken" / "events.jsonl"
    lines = path.read_text().splitlines()
    rec = json.loads(lines[-1])
    rec["seq"] = 99  # position 10 must carry seq 10 — this log is corrupt
    lines[-1] = json.dumps(rec, sort_keys=True)
    path.write_text("\n".join(lines) + "\n")
    with pytest.raises(ValueError, match="seq broken"):
        RecallCorpus.from_runs(tmp_path, "self", ["broken"])


# ---------------------------------------------------------------- retrieval


def test_query_ranks_by_term_overlap_and_never_fills():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        _write_run(tmp, "prior-a")
        _write_run(tmp, "prior-b")
        corpus = RecallCorpus.from_runs(tmp, "self", ["prior-a", "prior-b"])
        hits = corpus.query("roman", "city placement grassland water")
        assert hits, "expected grassland-city lesson to match"
        assert hits[0]["text"].startswith("Found cities on grassland")
        assert all("match_id" in h and "turn" in h and "lesson_id" in h
                   for h in hits)
        # zero-score queries return nothing — no filler
        assert corpus.query("roman", "diplomacy spaceship") == []
        assert corpus.query("korean-turtler", "grassland") == []  # own-only


def test_query_is_deterministic_and_capped():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        for run in ("prior-a", "prior-b"):
            _write_run(tmp, run)
        corpus = RecallCorpus.from_runs(tmp, "self", ["prior-a", "prior-b"])
        first = corpus.query("roman", "neighbors coordinate hex axial")
        second = corpus.query("roman", "neighbors coordinate hex axial")
        assert first == second
        assert len(corpus.query("roman", "lesson warriors archers cities "
                                        "grassland hex stacks")) <= LESSON_LIMIT
