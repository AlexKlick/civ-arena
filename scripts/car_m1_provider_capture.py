#!/usr/bin/env python3
"""Capture CAR-M1 MiniMax-M3 and GLM-5.3 proposal strata."""

from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
from pathlib import Path

from civ_arena.experiments.provider_capture import (
    PROVIDERS,
    build_global_pilot_gate_v2,
    capture_full_provider_v2,
    capture_pilot_provider_v2,
    new_provider_client,
    proposal_docs_from_corpus_v2,
    source_identity_v2,
)
from civ_arena.experiments.turn_core import run_experiment_v2, write_immutable_json


def _git_identity(allowed_output: Path | None = None) -> tuple[str, str]:
    status = subprocess.run(
        ["git", "status", "--porcelain"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.splitlines()
    try:
        allowed = (
            allowed_output.resolve().relative_to(Path.cwd().resolve()).as_posix()
            if allowed_output is not None
            else None
        )
    except ValueError:
        allowed = None
    dirty = [
        line
        for line in status
        if allowed is None or not line[3:].strip().startswith(allowed + "/")
    ]
    if dirty:
        raise SystemExit("refusing provider evidence from a dirty source tree")
    commit = subprocess.run(
        ["git", "rev-parse", "HEAD"], check=True, capture_output=True, text=True
    ).stdout.strip()
    tree = subprocess.run(
        ["git", "rev-parse", "HEAD^{tree}"], check=True, capture_output=True, text=True
    ).stdout.strip()
    return commit, tree


async def _main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--manifest",
        type=Path,
        default=Path("experiments/car-m1-v2/fixture-manifest.json"),
    )
    ap.add_argument("--evidence-root", type=Path, default=Path("evidence/car-m1-v2/provider"))
    stage = ap.add_mutually_exclusive_group(required=True)
    stage.add_argument("--pilot", action="store_true")
    stage.add_argument("--full", action="store_true")
    stage.add_argument("--evaluate", action="store_true")
    opts = ap.parse_args(argv)
    commit, tree = _git_identity(opts.evidence_root)
    source = source_identity_v2(commit, tree)
    manifest = json.loads(opts.manifest.read_text(encoding="utf-8"))

    if opts.pilot:
        summaries = []
        for spec in PROVIDERS:
            client = new_provider_client(spec)
            try:
                summary = await capture_pilot_provider_v2(
                    manifest_doc=manifest,
                    root=opts.evidence_root,
                    spec=spec,
                    client=client,
                    source=source,
                )
            finally:
                await client.aclose()
            summaries.append(summary)
            print(f"{spec.provider_id}: {summary['body']['status']}")
        gate = build_global_pilot_gate_v2(manifest["manifest_sha256"], summaries)
        write_immutable_json(opts.evidence_root / "pilot-gate.json", gate)
        print(f"global pilot: {gate['body']['status']}")
        return 0 if gate["body"]["passed"] else 3

    if opts.full:
        all_complete = True
        for spec in PROVIDERS:
            client = new_provider_client(spec)
            try:
                corpus = await capture_full_provider_v2(
                    manifest_doc=manifest,
                    root=opts.evidence_root,
                    spec=spec,
                    client=client,
                    source=source,
                )
            finally:
                await client.aclose()
            print(
                f"{spec.provider_id}: {corpus['body']['successes']}/100 "
                f"from {corpus['body']['posts']} POSTs"
            )
            all_complete = all_complete and corpus["body"]["complete"]
        return 0 if all_complete else 3

    for spec in PROVIDERS:
        path = opts.evidence_root / spec.provider_id / "proposals.json"
        corpus = json.loads(path.read_text(encoding="utf-8"))
        policy, proposals, corpus_sha = proposal_docs_from_corpus_v2(
            corpus,
            expected_manifest_sha256=manifest["manifest_sha256"],
            expected_source=source,
            expected_spec=spec,
        )
        result = await run_experiment_v2(
            manifest,
            source_commit=commit,
            source_tree=tree,
            proposal_source=f"provider:{spec.provider_id}:{spec.model_id}",
            proposal_policy=policy,
            proposal_docs=proposals,
            proposal_corpus_sha256=corpus_sha,
        )
        out = opts.evidence_root / spec.provider_id / "treatment-results.json"
        write_immutable_json(out, result)
        print(f"{spec.provider_id}: {result['body']['deterministic_verdict']}")
    return 0


def main() -> None:
    raise SystemExit(asyncio.run(_main()))


if __name__ == "__main__":
    main()
