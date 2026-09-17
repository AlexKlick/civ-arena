"""Lane D ledger: append-only writer semantics, exercised on tmp_path only."""

from __future__ import annotations

from pathlib import Path

from civ_arena.research.ledger import (
    EXPLANATION,
    HEADER,
    append_iteration,
    iteration_dir,
)


def test_first_write_creates_header_and_explanation(tmp_path):
    path = tmp_path / "research" / "RESEARCH-LEDGER.md"
    append_iteration(path, "## Iteration 001 - probe\n- fact one\n- fact two")
    text = path.read_text(encoding="utf-8")
    assert text.startswith(f"{HEADER}\n")
    assert EXPLANATION in text
    assert "## Iteration 001 - probe" in text
    assert "- fact two\n" in text  # the section got its own terminating newline
    assert text.endswith("\n\n")  # section, then exactly one blank line


def test_two_appends_preserve_full_prefix(tmp_path):
    path = tmp_path / "ledger.md"
    append_iteration(path, "## one\nbody one")
    first = path.read_text(encoding="utf-8")
    append_iteration(path, "## two\nbody two")
    second = path.read_text(encoding="utf-8")
    assert second.startswith(first)
    assert first.endswith("\n\n")
    assert "## one" in second and "## two" in second
    assert second.index("## one") < second.index("## two")


def test_preexisting_content_is_never_rewritten_or_headered(tmp_path):
    path = tmp_path / "ledger.md"
    path.write_text("Pre-existing operator notes.\n", encoding="utf-8")
    append_iteration(path, "## added later\nbody")
    text = path.read_text(encoding="utf-8")
    assert text.startswith("Pre-existing operator notes.\n")
    assert HEADER not in text
    assert "## added later" in text


def test_unterminated_preexisting_line_gets_separator_not_a_rewrite(tmp_path):
    path = tmp_path / "ledger.md"
    path.write_text("no trailing newline", encoding="utf-8")
    append_iteration(path, "## section")
    text = path.read_text(encoding="utf-8")
    assert text.startswith("no trailing newline\n")
    assert "## section" in text


def test_iteration_dir_zero_pads_and_creates(tmp_path):
    seventh = iteration_dir(tmp_path, 7)
    assert seventh == Path(tmp_path) / "iterations" / "007"
    assert seventh.is_dir()
    assert iteration_dir(tmp_path, 123).name == "123"
    assert iteration_dir(tmp_path, 7) == seventh  # idempotent
