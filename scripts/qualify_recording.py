"""Recording qualification harness — dataset evidence card §6 steps 2-4 + summary.

Turns one captured run directory into ONE machine verdict by running, unchanged,
the capture audit, the viewer, and the exporter, then assembling the counters
the evidence card's result table needs. Targets the DRIVEN dispatch-hotseat
recording path (the shape §6 qualifies: driven seats plus spectator_world
captures); a spectate-phase run fails the audit's row contract.

Stages (each appends human-readable failures to one shared list; a stage never
raises past its boundary except usage/IO errors):

- stage_audit      validate_run.validate + spectator_world row contract
                   (after_seat == [-1] + [0, 1] * rounds), the
                   spectator_world_failed census, the roster-kind census and
                   the run identity (a dirty tree fails).
- stage_viewer     in-process DashboardStore.load — status must be 'completed'
                   and no warning may fall outside the benign size-cap
                   baseline.
- stage_export     export_dataset.export/.write UNCHANGED (output outside the
                   run dir), then ref integrity over the exported samples, the
                   manifest's source-digest binding and the flag histogram.
- stage_summarize  qualification.json (schema 1) and, optionally, a markdown
                   report skeleton whose human cells stay for the operator.

Usage:
    python scripts/qualify_recording.py <run_dir> --rounds N [--allow-fake]
        [--baseline-warnings configs/qual-warning-baseline.json]
        [--export-out PATH] [--coverage PATH] [--report PATH] [--json PATH]

Exit codes:
    0  verdict PASS
    1  any stage failure (verdict FAIL; qualification.json is still written)
    2  usage/IO error: missing run dir, unreadable artifact, malformed
       baseline file, bad --rounds, or an output path inside the run dir

The harness NEVER mutates the run dir: every output defaults to a sibling
"<run>.qual/" directory, and any explicit output inside the run dir is
refused. No HTTP server is started; the viewer runs in-process.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from civ_arena.dashboard import DashboardStore  # noqa: E402
from civ_arena.game.civ6.validate_run import validate  # noqa: E402
from civ_arena.research.export_dataset import export, write  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
DEFAULT_BASELINE = REPO / "configs" / "qual-warning-baseline.json"
ENTITY_CLASS_SOURCE = "roster PLAYERROW kind — named, never inferred"
# dataset evidence card §6: the result table's human cells stay human.
PENDING = "*(pending operator fill)*"
MAX_FAILED_REFS = 10
VIEWER_CHECK = "in-process DashboardStore.load"


class QualificationError(Exception):
    """Usage/IO failure: the harness cannot even read its inputs."""


def diff_warnings(warnings: list, baseline: list[str]) -> tuple[list, list]:
    """Split observed viewer warnings into (baseline_matched, new)."""
    matched = [w for w in warnings if w in baseline]
    new = [w for w in warnings if w not in baseline]
    return matched, new


# -- stage 1: capture audit -----------------------------------------------------


def stage_audit(run_dir: Path, rounds: int, require_live: bool,
                failures: list[str]) -> dict:
    """validate_run + the spectator_world contract + identity, in one pass."""
    result = {"validate": {}, "spectator_world_rows": 0,
              "expected_rows": 1 + 2 * rounds, "after_seat_sequence": [],
              "spectator_world_failed": 0, "roster_kinds_observed": {},
              "entity_class_source": ENTITY_CLASS_SOURCE, "identity": {}}
    try:
        verdict = validate(run_dir, rounds, require_live=require_live)
    except (OSError, ValueError, KeyError, TypeError, IndexError) as exc:
        verdict = {"status": "INCOMPLETE",
                   "errors": [f"{type(exc).__name__}: {exc}"]}
    result["validate"] = verdict
    for message in verdict.get("errors") or []:
        failures.append(f"capture audit: {message}")

    worlds, failed, kinds, identity_events = [], [], Counter(), []
    try:
        with open(run_dir / "events.jsonl", encoding="utf-8") as stream:
            for number, line in enumerate(stream, start=1):
                if not line.strip():
                    continue
                try:
                    record = json.loads(line)
                except ValueError as exc:
                    raise QualificationError(
                        f"events.jsonl line {number} is not valid JSON: {exc}") from exc
                audit = record.get("audit")
                if audit == "spectator_world":
                    worlds.append(record)
                    world = record.get("world")
                    roster = world.get("roster", []) if isinstance(world, dict) else []
                    for row in roster:
                        kind = row.get("kind") if isinstance(row, dict) else None
                        kinds[kind if isinstance(kind, str) else "<absent-kind>"] += 1
                elif audit == "spectator_world_failed":
                    failed.append(record)
                elif audit == "run_identity":
                    identity_events.append(record)
    except OSError as exc:
        raise QualificationError(f"events.jsonl is unreadable: {exc}") from exc

    after_seats = [row.get("after_seat") for row in worlds]
    expected = [-1] + [0, 1] * rounds
    result["spectator_world_rows"] = len(after_seats)
    result["after_seat_sequence"] = after_seats
    result["spectator_world_failed"] = len(failed)
    result["roster_kinds_observed"] = dict(sorted(kinds.items()))
    if failed:
        seats = [row.get("after_seat") for row in failed]
        failures.append(f"spectator_world_failed capture(s) present: {len(failed)} "
                        f"(after_seat={seats}) — the spectator capture never killed "
                        "the match, but the run is not qualified")
    if after_seats != expected:
        failures.append(f"spectator_world row contract violated: observed after_seat "
                        f"{after_seats}, expected {expected}")
    if not worlds:
        failures.append("no spectator_world capture in the log — the run was "
                        "recorded without spectator_capture or predates it")

    try:
        summary = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise QualificationError(f"summary.json is unreadable: {exc}") from exc
    identity = summary.get("identity") or {} if isinstance(summary, dict) else {}
    # fake lives on the run_identity audit event (live_driver phase_dispatch_hotseat);
    # the summary identity carries commit/tree/dirty/config/mod_sha256.
    fake = identity_events[0].get("fake") if identity_events else identity.get("fake")
    result["identity"] = {"commit": identity.get("commit"), "dirty": identity.get("dirty"),
                          "mod_sha256": identity.get("mod_sha256"), "fake": fake}
    if identity.get("dirty") is True:
        failures.append("run recorded on a dirty tree")
    return result


# -- stage 2: viewer ------------------------------------------------------------


def stage_viewer(run_dir: Path, baseline: list[str], failures: list[str]) -> dict:
    """The match room projection, in-process: completed and warning-clean."""
    result = {"status": None, "warnings": [], "baseline_matched": [],
              "new_warnings": [], "check": VIEWER_CHECK}
    try:
        payload = DashboardStore(run_dir.parent).load(run_dir.name)
    except (OSError, ValueError, TypeError, KeyError, AttributeError,
            RecursionError) as exc:
        failures.append(f"viewer load failed: {type(exc).__name__}: {exc}")
        return result
    warnings = payload.get("warnings") or []
    matched, new = diff_warnings(warnings, baseline)
    result.update(status=payload.get("status"), warnings=list(warnings),
                  baseline_matched=matched, new_warnings=new)
    if payload.get("status") != "completed":
        failures.append(f"viewer status is {payload.get('status')!r}, "
                        "expected 'completed'")
    if new:
        failures.append(f"viewer reported {len(new)} warning(s) outside the benign "
                        f"size-cap baseline: {new}")
    return result


# -- stage 3: exporter ----------------------------------------------------------


def _collect_refs(node, path: str, out: list) -> None:
    """Every events.jsonl reference object: exactly the exporter's _ref() shape
    ({"artifact": ..., "seq": ...}) wherever it sits — list-valued *_refs keys
    and the nested observed_interval_start/end singles alike."""
    if isinstance(node, dict):
        if "seq" in node and "artifact" in node:
            out.append((path, node))
            return
        for key in sorted(node):
            _collect_refs(node[key], f"{path}.{key}", out)
    elif isinstance(node, list):
        for index, item in enumerate(node):
            _collect_refs(item, f"{path}[{index}]", out)


def _read_samples(path: Path) -> tuple[bytes, list]:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise QualificationError(f"{path} is unreadable: {exc}") from exc
    samples = []
    for number, line in enumerate(raw.decode().splitlines(), start=1):
        if not line.strip():
            continue
        try:
            samples.append(json.loads(line))
        except ValueError as exc:
            raise QualificationError(f"{path} line {number} is not valid JSON: "
                                     f"{exc}") from exc
    return raw, samples


def _valid_event_seqs(events_bytes: bytes) -> set[int]:
    valid = set()
    for line in events_bytes.decode().splitlines():
        if not line.strip():
            continue
        seq = json.loads(line).get("seq")
        if type(seq) is int:
            valid.add(seq)
    return valid


def check_ref_integrity(exported_path: Path, events_bytes: bytes) -> dict:
    """Walk every exported sample and verify each reference resolves into the
    events.jsonl bytes the export consumed."""
    _, samples = _read_samples(exported_path)
    return check_sample_refs(samples, events_bytes)


def check_sample_refs(samples: list, events_bytes: bytes) -> dict:
    """Ref integrity over already-parsed exported samples (one disk read)."""
    valid = _valid_event_seqs(events_bytes)
    checked, failed = 0, []
    failures = 0
    for index, sample in enumerate(samples, start=1):
        found: list = []
        _collect_refs(sample, f"line {index}", found)
        for path, ref in found:
            checked += 1
            seq = ref.get("seq")
            problem = None
            if type(seq) is not int:
                problem = f"seq {seq!r} is not an integer"
            elif ref.get("artifact") != "events.jsonl":
                problem = f"artifact {ref.get('artifact')!r} is not events.jsonl"
            elif seq not in valid:
                problem = f"seq {seq} is not present in events.jsonl"
            if problem is None:
                continue
            failures += 1
            if len(failed) < MAX_FAILED_REFS:
                label = sample.get("segment_id")
                failed.append(f"{path} sample {label!r} ref {ref!r}: {problem}")
    return {"refs_checked": checked, "failures": failures, "failed_refs": failed}


def _flag_histogram(samples: list) -> dict:
    histogram: dict[str, Counter] = {}
    for sample in samples:
        flags = sample.get("quality_flags") or []
        histogram.setdefault(str(sample.get("sample_class")), Counter()).update(
            str(flag) for flag in flags)
    return {name: dict(sorted(counts.items()))
            for name, counts in sorted(histogram.items())}


def stage_export(run_dir: Path, export_out: Path, failures: list[str]) -> dict:
    """The exporter, unchanged, plus ref integrity and digest binding."""
    empty = {"output": "", "output_sha256": "", "manifest": {}, "sample_counts": {},
             "ref_integrity": {"refs_checked": 0, "failures": 0, "failed_refs": []},
             "flag_histogram": {}}
    try:
        events_bytes = (run_dir / "events.jsonl").read_bytes()
    except OSError as exc:
        raise QualificationError(f"events.jsonl is unreadable: {exc}") from exc
    try:
        samples, manifest = export(run_dir)
    except (OSError, ValueError, KeyError, TypeError, AssertionError) as exc:
        failures.append(f"export failed: {type(exc).__name__}: {exc}")
        return empty
    result = dict(empty, manifest=manifest,
                  sample_counts=manifest.get("sample_counts") or {})
    try:
        write(samples, manifest, export_out)
    except (OSError, ValueError) as exc:
        failures.append(f"export write failed: {type(exc).__name__}: {exc}")
        return result
    exported_bytes, on_disk = _read_samples(export_out)
    result["output"] = str(export_out)
    result["output_sha256"] = hashlib.sha256(exported_bytes).hexdigest()
    source_sha = hashlib.sha256(events_bytes).hexdigest()
    if manifest.get("source", {}).get("events_sha256") != source_sha:
        failures.append("digest binding failed: manifest source.events_sha256 does not "
                        "match the events.jsonl bytes on disk")
    integrity = check_sample_refs(on_disk, events_bytes)
    result["ref_integrity"] = integrity
    if integrity["failures"]:
        failures.append(f"ref integrity: {integrity['failures']} broken reference(s) in "
                        f"{integrity['refs_checked']} checked: {integrity['failed_refs']}")
    result["flag_histogram"] = _flag_histogram(on_disk)
    return result


# -- stage 4: summary -----------------------------------------------------------


def stage_summarize(run_dir: Path, rounds: int, allow_fake: bool, sections: dict,
                    failures: list[str], coverage: dict | None) -> dict:
    """The machine verdict document (qualification.json schema 1)."""
    return {"schema": 1, "run_dir": str(run_dir), "rounds": rounds,
            "generated_at": datetime.now(UTC).isoformat(), "allow_fake": allow_fake,
            "audit": sections["audit"], "viewer": sections["viewer"],
            "export": sections["export"], "coverage": coverage,
            "verdict": "PASS" if not failures else "FAIL", "failures": list(failures)}


def coverage_section(coverage_path: Path) -> dict:
    """Lane-B coverage matrix, read as bytes: path, digest, non-blank row count.
    The file is consumed as data — no lane-B code is imported."""
    try:
        raw = coverage_path.read_bytes()
    except OSError as exc:
        raise QualificationError(f"coverage matrix is unreadable: {exc}") from exc
    return {"matrix_path": str(coverage_path),
            "matrix_sha256": hashlib.sha256(raw).hexdigest(),
            "matrix_rows": sum(1 for line in raw.decode().splitlines() if line.strip())}


def render_report(qualification: dict, export_out: Path, json_path: Path,
                  report_path: Path | None) -> str:
    """Markdown skeleton for evidence card §6: machine cells filled, human
    cells left to the operator."""
    audit, viewer, export_section = (qualification["audit"], qualification["viewer"],
                                     qualification["export"])
    counts = ", ".join(f"{name}={count}" for name, count
                       in sorted((export_section.get("sample_counts") or {}).items()))
    histogram = json.dumps(export_section.get("flag_histogram") or {}, sort_keys=True)
    coverage = qualification["coverage"]
    lines = [
        f"# Recording qualification report — {Path(qualification['run_dir']).name}",
        "",
        f"Generated {qualification['generated_at']} by "
        "scripts/qualify_recording.py (dataset evidence card §6 steps 2-4).",
        "",
        "| Field | Value |",
        "|---|---|",
        f"| Run id | `{Path(qualification['run_dir']).name}` |",
        f"| Mod version | {PENDING} |",
        f"| Samples by class | {counts or 'none'} |",
        f"| Ref-integrity failures | "
        f"{export_section['ref_integrity']['failures']} "
        f"(of {export_section['ref_integrity']['refs_checked']} refs checked) |",
        f"| Flag histogram | `{histogram}` |",
        f"| Allowed uses for THIS recording | {PENDING} |",
        "",
        f"Verdict: **{qualification['verdict']}**",
        "",
        "## Capture audit",
        "",
        f"- validate status: {audit['validate'].get('status')!r} "
        f"(errors: {audit['validate'].get('errors')})",
        f"- spectator_world rows: {audit['spectator_world_rows']} of "
        f"{audit['expected_rows']} expected; after_seat={audit['after_seat_sequence']}",
        f"- spectator_world_failed: {audit['spectator_world_failed']}",
        f"- roster kinds observed: {json.dumps(audit['roster_kinds_observed'],
                                               sort_keys=True)} "
        f"({audit['entity_class_source']})",
        f"- identity: commit={audit['identity'].get('commit')!r} "
        f"dirty={audit['identity'].get('dirty')!r} "
        f"mod_sha256={audit['identity'].get('mod_sha256')!r} "
        f"fake={audit['identity'].get('fake')!r}",
        "",
        "## Viewer",
        "",
        f"- {viewer['check']}; status: {viewer['status']!r}",
        f"- warnings: {json.dumps(viewer['warnings'])}",
        f"- baseline matched: {json.dumps(viewer['baseline_matched'])}",
        f"- new warnings: {json.dumps(viewer['new_warnings'])}",
        "",
        "## Exporter",
        "",
        f"- output: `{export_section['output']}` "
        f"(sha256 {export_section['output_sha256']})",
        f"- sample counts: {json.dumps(export_section.get('sample_counts') or {},
                                       sort_keys=True)}",
        f"- ref integrity: {export_section['ref_integrity']['refs_checked']} checked, "
        f"{export_section['ref_integrity']['failures']} failed",
        f"- flag histogram: `{histogram}`",
        "",
        "## Coverage",
        "",
        (f"- matrix: `{coverage['matrix_path']}` sha256 {coverage['matrix_sha256']} "
         f"rows {coverage['matrix_rows']}" if coverage else "- not provided"),
        "",
        "## Failures",
        "",
    ]
    lines += [f"- {failure}" for failure in qualification["failures"]] \
        or ["- none"]
    lines += ["", "## Operator fill-in",
              "",
              f"- Mod version and allowed uses: {PENDING} (evidence card §5 minus "
              "whatever this recording's flags remove).",
              f"- Machine document: `{json_path}`; exported samples: "
              f"`{export_out}` (manifest sibling).",
              f"- Report path: `{report_path}`." if report_path else ""]
    return "\n".join(line for line in lines if line is not None) + "\n"


# -- CLI ------------------------------------------------------------------------


def _parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="qualify_recording.py", description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Exit codes: 0 verdict PASS; 1 any stage failure (verdict FAIL, "
               "qualification.json still written); 2 usage/IO error (missing run "
               "dir, unreadable artifact, malformed baseline file, bad --rounds, "
               "or an output path inside the run dir).")
    ap.add_argument("run_dir", type=Path, help="captured run directory (read-only)")
    ap.add_argument("--rounds", type=int, required=True,
                    help="requested/completed round count the run was captured for")
    ap.add_argument("--allow-fake", action="store_true",
                    help="pass require_live=False to the capture audit (fake-engine "
                         "rehearsals); a real qualification omits this")
    ap.add_argument("--baseline-warnings", type=Path, default=DEFAULT_BASELINE,
                    help=f"benign size-cap warning baseline "
                         f"(default: {DEFAULT_BASELINE})")
    ap.add_argument("--export-out", type=Path, default=None,
                    help="exported samples path (default: <run>.qual/samples.jsonl "
                         "next to the run dir, never inside it)")
    ap.add_argument("--coverage", type=Path, default=None,
                    help="lane-B coverage matrix file; recorded by path, sha256 and "
                         "row count only")
    ap.add_argument("--report", type=Path, default=None,
                    help="markdown report path (default: "
                         "<run>.qual/qualification-report.md)")
    ap.add_argument("--json", type=Path, default=None,
                    help="machine verdict path (default: <run>.qual/qualification.json)")
    return ap


def _refuse_inside_run(path: Path, run_dir: Path, label: str) -> None:
    if path.resolve().is_relative_to(run_dir.resolve()):
        raise QualificationError(f"--{label} {path} is inside the run dir; the "
                                 "harness never writes into the run it qualifies")


def main(argv: list[str] | None = None) -> int:
    opts = _parser().parse_args(argv)
    failures: list[str] = []
    try:
        run_dir = opts.run_dir
        if not run_dir.is_dir():
            raise QualificationError(f"run dir does not exist: {run_dir}")
        for name in ("events.jsonl", "summary.json"):
            artifact = run_dir / name
            if not artifact.is_file():
                raise QualificationError(f"missing run artifact: {artifact}")
        if opts.rounds < 1:
            raise QualificationError(f"--rounds must be >= 1, got {opts.rounds}")
        try:
            baseline = json.loads(opts.baseline_warnings.read_text(encoding="utf-8"))
        except OSError as exc:
            raise QualificationError(f"baseline file is unreadable: {exc}") from exc
        except ValueError as exc:
            raise QualificationError(f"baseline file is not valid JSON: {exc}") from exc
        if not isinstance(baseline, list) or not all(
                isinstance(item, str) for item in baseline):
            raise QualificationError("baseline file must be a JSON array of strings")
        coverage = None
        if opts.coverage is not None:
            coverage = coverage_section(opts.coverage)
        out_dir = run_dir.parent / f"{run_dir.name}.qual"
        export_out = opts.export_out or out_dir / "samples.jsonl"
        json_path = opts.json or out_dir / "qualification.json"
        report_path = opts.report or out_dir / "qualification-report.md"
        for path, label in ((export_out, "export-out"), (json_path, "json"),
                            (report_path, "report")):
            _refuse_inside_run(path, run_dir, label)
    except QualificationError as exc:
        print(f"qualification: {exc}", file=sys.stderr)
        return 2

    audit = stage_audit(run_dir, opts.rounds, require_live=not opts.allow_fake,
                        failures=failures)
    viewer = stage_viewer(run_dir, baseline, failures=failures)
    export_section = stage_export(run_dir, export_out, failures=failures)
    qualification = stage_summarize(run_dir, opts.rounds, opts.allow_fake,
                                    {"audit": audit, "viewer": viewer,
                                     "export": export_section},
                                    failures, coverage)
    json_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(json.dumps(qualification, indent=2, sort_keys=True) + "\n",
                         encoding="utf-8")
    report_path.write_text(render_report(qualification, export_out, json_path,
                                         report_path), encoding="utf-8")
    print(f"qualification: {qualification['verdict']} — {len(failures)} failure(s); "
          f"verdict {json_path}")
    for failure in failures:
        print(f"  - {failure}")
    return 0 if qualification["verdict"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
