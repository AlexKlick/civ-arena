"""Watchdog: authorization by the referee's OWN ledger, never adapter self-report.

The diff is a multiset comparison on (entity_type, entity_id, attr, before,
after) between what the referee authorized (ambient manifest + accepted
command receipts) and what the adapter's journal says actually happened.
A mutation that lies about its ``origin`` is still caught — the allowlist is
what the referee requested, not what the mutation claims.

Classification:
- extra mutation whose (entity, attr) IS covered by the manifest but with
  off-manifest values → AMBIENT_DEVIATION (flag; usually an engine-side
  surprise — degrade, don't abort)
- anything else extra → UNAUTHORIZED_ACTION
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from civ_arena.canonical import canonical
from civ_arena.game.adapter import MutationRecord


@dataclass
class Violation:
    kind: str  # "UNAUTHORIZED_ACTION" | "AMBIENT_DEVIATION"
    detail: str
    mutations: list[dict[str, Any]] = field(default_factory=list)

    def to_doc(self) -> dict[str, Any]:
        return {"kind": self.kind, "detail": self.detail, "mutations": self.mutations}


def _mkey(m: MutationRecord) -> tuple[str, str, str, str, str]:
    # before/after can be lists (queues, revealed sets) — canonicalize to
    # hashable text so the multiset diff never crashes on them.
    return (m.entity_type, m.entity_id, m.attr, canonical(m.before), canonical(m.after))


def diff(actual: list[MutationRecord], allowed: list[MutationRecord]) -> list[Violation]:
    remaining = Counter(_mkey(m) for m in allowed)
    covered_attrs = {(m.entity_type, m.entity_id, m.attr) for m in allowed}

    unauthorized: list[MutationRecord] = []
    deviations: list[MutationRecord] = []
    for mutation in actual:
        key = _mkey(mutation)
        if remaining[key] > 0:
            remaining[key] -= 1
            continue
        if (mutation.entity_type, mutation.entity_id, mutation.attr) in covered_attrs:
            deviations.append(mutation)
        else:
            unauthorized.append(mutation)

    violations: list[Violation] = []
    if unauthorized:
        violations.append(Violation(
            kind="UNAUTHORIZED_ACTION",
            detail=f"{len(unauthorized)} uncommanded mutation(s)",
            mutations=[m.to_doc() for m in unauthorized],
        ))
    if deviations:
        violations.append(Violation(
            kind="AMBIENT_DEVIATION",
            detail=f"{len(deviations)} off-manifest mutation(s) on declared "
                   f"ambient attributes",
            mutations=[m.to_doc() for m in deviations],
        ))
    return violations


@dataclass
class WatchdogReport:
    clean: bool
    violations: list[Violation] = field(default_factory=list)

    def to_doc(self) -> dict[str, Any]:
        return {
            "clean": self.clean,
            "violations": [v.to_doc() for v in self.violations],
        }
