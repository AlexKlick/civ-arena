"""M19b — the case base: retrieval-as-evidence prior over past decisions.

A case is a past planner DECISION abstracted to its strategic signature —
the search ``root_key`` projected by dropping ``turn`` and each player's
``development`` (cases generalize across turns and build states; everything
the option menu is sensitive to — the candidates vector, cities,
population, gold bucket, research state, unit counts — stays). The miner
aggregates M19a label docs into per-signature per-option outcome stats;
the runtime looks up the CURRENT world's signature and ranks options by
historical win rate among takens. The prior can only PERMUTE the search's
visit order (the M16b authority model): it is compiled legal-now exactly
like a proposer ranking and merges AFTER the live proposer's.

Determinism contract: the signature is a pure function of the same
determinized world the trace ``root_key`` came from
(``build_state_doc(belief, seed=turn)`` — the runtime's ``bstate`` IS that
world), and the artifact is immutable once constructed, so
(belief, artifact) -> prior is bit-stable across resume.

    uv run python -m civ_arena.planner.casebase --mine \
        --labels-glob 'runs/exp3/b*/planner-*/labels.json' \
        --index runs/labels-exp3-b8.json --index runs/labels-llm.json \
        --out configs/casebase-exp3.json
    uv run python -m civ_arena.planner.casebase --path configs/casebase-exp3.json
"""

from __future__ import annotations

import argparse
import copy
import glob
import hashlib
import json
import os
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from civ_arena.canonical import CanonicalError, canonical, sha256_hex
from civ_arena.planner.search import abstract_doc

SCHEMA = 1

# abstract_key doc fields the case signature DROPS: "turn" (top level) and
# each player's "development" — everything else is identity.
_PLAYER_KEEP = ("cities", "population", "gold_bucket", "researched",
                "researching", "units")


def _project(doc: dict[str, Any]) -> dict[str, Any]:
    """The case-signature projection of an abstract-key doc. Single source
    for both entry points below. Loud on a malformed doc (KeyError/TypeError
    propagate — a silently mis-shaped root_key must never mine a bogus case)."""
    out: dict[str, Any] = {"candidates": list(doc["candidates"])}
    for pid in sorted(k for k in doc if k not in ("turn", "candidates")):
        player = doc[pid]
        out[pid] = {field: player[field] for field in _PLAYER_KEEP}
    return out


def signature_from_key(root_key: str) -> str:
    """MINER entry point: project a trace root_key (canonical JSON text, the
    labels.json decisions[].root_key field) and hash."""
    return sha256_hex(canonical(_project(json.loads(root_key))))


def signature_from_state(state: Any, pid: int) -> str:
    """RUNTIME entry point: same projection over the live determinized world.
    Callers must pass the SAME world the trace root_key is computed from —
    ``SimState.from_doc(build_state_doc(belief, seed=turn))``; the runtime's
    ``bstate`` is exactly that construction."""
    return sha256_hex(canonical(_project(abstract_doc(state, pid))))


def mine(label_docs: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate label docs into case stats. Per (signature, option):
    ``n`` counts CANDIDACIES (decisions where the option was on the menu),
    ``taken`` selections, ``wins`` wins among takens (outcome_sign == 1),
    ``diff_sum`` the summed match_value_differential of takens. A seat with
    no score vector (outcome_sign/differential None) still counts its n and
    taken — the outcome fields alone are skipped, never fabricated as 0."""
    cases: dict[str, dict[str, dict[str, Any]]] = {}
    runs = 0
    for doc in label_docs:
        runs += 1
        for row in doc.get("decisions", []):
            sig = signature_from_key(row["root_key"])
            bucket = cases.setdefault(sig, {})
            seen: set[str] = set()
            for oid in row["candidates"]:
                if not isinstance(oid, str) or oid in seen:
                    continue
                seen.add(oid)
                rec = bucket.setdefault(
                    oid, {"n": 0, "taken": 0, "wins": 0, "diff_sum": 0})
                rec["n"] += 1
            chosen = row.get("chosen")
            if isinstance(chosen, str) and chosen in bucket:
                rec = bucket[chosen]
                rec["taken"] += 1
                if row.get("outcome_sign") == 1:
                    rec["wins"] += 1
                diff = row.get("match_value_differential")
                if isinstance(diff, int) and not isinstance(diff, bool):
                    rec["diff_sum"] += diff
    return {"cases": cases, "runs": runs}


def _validate_cases(cases: Any) -> dict[str, dict[str, dict[str, Any]]]:
    """Fail-closed shape check (construction time — the runtime's per-turn
    fail-soft boundary RELIES on the artifact having been validated here)."""
    if not isinstance(cases, dict):
        raise ValueError(f"case base artifact 'cases' must be a mapping, "
                         f"got {type(cases).__name__}")
    for sig, bucket in cases.items():
        if not isinstance(sig, str) or not isinstance(bucket, dict):
            raise ValueError("case base artifact cases must map signature "
                             "strings to option mappings")
        for oid, rec in bucket.items():
            if not isinstance(oid, str) or not isinstance(rec, dict):
                raise ValueError("case base artifact option entries must map "
                                 "option ids to stat mappings")
            for field in ("n", "taken", "wins"):
                val = rec.get(field)
                if not isinstance(val, int) or isinstance(val, bool) or val < 0:
                    raise ValueError(
                        f"case base artifact stat {sig[:12]}..{oid}.{field} "
                        f"must be a non-negative integer, got {val!r}")
            diff_sum = rec.get("diff_sum")
            if not isinstance(diff_sum, int) or isinstance(diff_sum, bool):
                raise ValueError(
                    f"case base artifact stat {sig[:12]}..{oid}.diff_sum "
                    f"must be an integer, got {diff_sum!r}")
    return cases


class CaseBase:
    """Immutable retrieval index over mined case stats. Constructed loudly
    (``from_file`` refuses missing/corrupt/wrong-schema artifacts — the
    RecallCorpus discipline: construction-time, before any match spend);
    consumed fail-soft at runtime (any per-turn failure degrades to no case
    prior, the _propose boundary shape)."""

    def __init__(self, cases: dict[str, dict[str, dict[str, Any]]],
                 artifact_sha256: str | None = None) -> None:
        # validated copy: callers keep no alias into the index
        self._cases: dict[str, dict[str, dict[str, Any]]] = \
            _validate_cases(copy.deepcopy(cases))
        self.artifact_sha256 = artifact_sha256

    @classmethod
    def from_file(cls, path: Path | str) -> CaseBase:
        """Load + validate an artifact. LOUD on missing/corrupt/wrong-schema
        (ValueError) — never a silently-empty case base."""
        path = Path(path)
        if not path.is_file():
            raise ValueError(f"case base artifact {path} does not exist — "
                             "refusing an empty case base")
        raw = path.read_text(encoding="utf-8")
        artifact_sha256 = hashlib.sha256(path.read_bytes()).hexdigest()
        try:
            doc = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError(f"case base artifact {path} is not valid JSON: "
                             f"{exc}") from exc
        got = doc.get("schema") if isinstance(doc, dict) else doc
        if not isinstance(doc, dict) or doc.get("schema") != SCHEMA:
            raise ValueError(f"case base artifact {path}: expected schema "
                             f"{SCHEMA}, got {got!r}")
        try:
            canonical(doc)  # no floats / non-canonical types anywhere
        except CanonicalError as exc:
            raise ValueError(f"case base artifact {path} is not canonical: "
                             f"{exc}") from exc
        return cls(doc["cases"], artifact_sha256=artifact_sha256)

    def hit(self, sig: str) -> bool:
        """Signature present in the artifact."""
        return sig in self._cases

    def rank(self, sig: str) -> list[str]:
        """Options for that signature ordered by win rate among takens
        (ties by option_id asc). Options never taken under this signature
        carry no win-rate evidence and are excluded; a missing signature or
        one with zero takens yields []. The rates are ordering-only floats —
        they never reach an artifact."""
        bucket = self._cases.get(sig)
        if not bucket:
            return []
        rated = [(rec["wins"] / rec["taken"], oid)
                 for oid, rec in bucket.items() if rec["taken"] > 0]
        rated.sort(key=lambda pair: (-pair[0], pair[1]))
        return [oid for _, oid in rated]

    def __len__(self) -> int:
        return len(self._cases)

    def summary(self) -> str:
        """Human-readable one-block summary (the --path CLI surface)."""
        options = sum(len(bucket) for bucket in self._cases.values())
        takens = sum(rec["taken"] for bucket in self._cases.values()
                     for rec in bucket.values())
        return (f"artifact_sha256: {self.artifact_sha256}\n"
                f"signatures: {len(self._cases)}\n"
                f"option buckets: {options}\n"
                f"takens: {takens}")


# ------------------------------------------------------------------ CLI


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_artifact(out: Path, artifact: dict[str, Any]) -> None:
    """Atomic tmp+replace (the labels/journal discipline); canonical text
    refuses a float-bearing artifact at write time, never on disk."""
    path = Path(out)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(canonical(artifact) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def main() -> None:
    ap = argparse.ArgumentParser(
        prog="civ-arena-casebase",
        description="Mine M19a label docs into the case-base artifact, or "
                    "load + summarize an existing one.")
    ap.add_argument("--mine", action="store_true",
                    help="mine label docs from --labels-glob into --out")
    ap.add_argument("--labels-glob", metavar="GLOB",
                    help="label doc paths to mine, e.g. "
                         "'runs/exp3/b8/planner-*/labels.json'")
    ap.add_argument("--index", action="append", type=Path, default=[],
                    metavar="PATH",
                    help="corpus index the label docs came from (repeatable); "
                         "recorded in the artifact source as path+sha256")
    ap.add_argument("--out", type=Path, help="artifact path to write")
    ap.add_argument("--path", type=Path,
                    help="load + print a summary of an existing artifact")
    opts = ap.parse_args()

    if opts.path is not None:
        if opts.mine or opts.labels_glob or opts.out is not None:
            ap.error("--path cannot be combined with --mine/--labels-glob/--out")
        print(CaseBase.from_file(opts.path).summary())
        return
    if not opts.mine:
        ap.error("give --mine (with --labels-glob and --out) or --path")
    if not opts.labels_glob or opts.out is None:
        ap.error("--mine requires --labels-glob and --out")

    paths = sorted(glob.glob(opts.labels_glob))
    if not paths:
        raise SystemExit(f"error: no label docs under {opts.labels_glob}")
    docs: list[dict[str, Any]] = []
    for p in paths:
        doc = json.loads(Path(p).read_text(encoding="utf-8"))
        if not isinstance(doc, dict):
            raise SystemExit(f"error: {p} is not a JSON object")
        docs.append(doc)
    mined = mine(docs)
    for index in opts.index:
        if not index.is_file():
            raise SystemExit(f"error: --index {index} does not exist")
    artifact = {
        "schema": SCHEMA,
        "source": {
            "indexes": [{"path": str(i), "sha256": _sha256_file(i)}
                        for i in opts.index],
            "runs": mined["runs"],
        },
        "cases": mined["cases"],
    }
    write_artifact(opts.out, artifact)
    print(f"mined {mined['runs']} label docs -> {opts.out} "
          f"({len(mined['cases'])} signatures)")


if __name__ == "__main__":
    main()
