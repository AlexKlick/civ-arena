"""The research ledger: an append-only markdown writer.

``research/RESEARCH-LEDGER.md`` is the campaign's durable record of what
each iteration concluded. The writer contract is deliberately tiny — it
NEVER rewrites, reorders, or reformats existing content. It appends a
section plus a blank line, creating the file (with its H1 header and a
one-line explanation) on first write only. No markdown parsing, no
reading for meaning: lanes write it, humans read it.

The append-only property is ASSERTED on every write: the pre-write bytes
must survive verbatim as a prefix of the post-write bytes.
"""

from __future__ import annotations

import os
from pathlib import Path

LEDGER_NAME = "RESEARCH-LEDGER.md"
HEADER = "# civ-arena Research Ledger"
EXPLANATION = (
    "Append-only record of the research loop: one section per iteration, "
    "written by the lane that ran it, never edited after the fact."
)


def iteration_dir(research_root: Path, n: int) -> Path:
    """``research_root/iterations/NNN`` (zero-padded to 3), created on demand."""
    path = Path(research_root) / "iterations" / f"{n:03d}"
    path.mkdir(parents=True, exist_ok=True)
    return path


def append_iteration(ledger_path: Path, section: str) -> None:
    """Append ``section`` (+ a blank line) to the ledger file.

    On first write (file missing or empty) the H1 header and the one-line
    explanation are written first; a pre-existing non-empty file is
    appended to exactly as it stands. Parent dirs are created lazily, the
    write is fsynced, and the pre-existing bytes are asserted to survive
    verbatim as a prefix — any reordering or rewrite is a hard failure.
    """
    ledger_path = Path(ledger_path)
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    existing = ledger_path.read_text(encoding="utf-8") if ledger_path.exists() else ""

    block = ""
    if not existing:
        block += f"{HEADER}\n\n{EXPLANATION}\n\n"
    elif not existing.endswith("\n"):
        # a prefix-preserving separator only: existing bytes are untouched
        block += "\n"
    block += (section if section.endswith("\n") else section + "\n") + "\n"

    with ledger_path.open("a", encoding="utf-8") as fh:
        fh.write(block)
        fh.flush()
        os.fsync(fh.fileno())

    after = ledger_path.read_text(encoding="utf-8")
    assert after.startswith(existing), "ledger append-only violation"
