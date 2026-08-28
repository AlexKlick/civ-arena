"""Canonicalization + hashing rules — the determinism foundation."""

from __future__ import annotations

import random

import pytest

from civ_arena.canonical import (
    CanonicalError,
    args_digest,
    assert_schema,
    canonical,
    checkpoint_hash,
    log_prefix_hash,
    rng_from_doc,
    rng_to_doc,
    state_hash,
)


def test_canonical_stable_across_key_order():
    a = {"b": [1, 2], "a": {"x": 1, "y": "z"}}
    b = {"a": {"y": "z", "x": 1}, "b": [1, 2]}
    assert canonical(a) == canonical(b)


def test_floats_rejected():
    with pytest.raises(CanonicalError):
        canonical({"hp": 97.5})


def test_nan_rejected():
    with pytest.raises(CanonicalError):
        canonical({"x": float("nan")})


def test_tuples_and_sets_rejected():
    with pytest.raises(CanonicalError):
        canonical({"coord": (1, 2)})
    with pytest.raises(CanonicalError):
        canonical({"tiles": {"a", "b"}})


def test_non_str_keys_rejected():
    with pytest.raises(CanonicalError):
        canonical({1: "one"})


def test_nested_rejection_reports_path():
    with pytest.raises(CanonicalError, match=r"\$\.units\.u1\.hp"):
        canonical({"units": {"u1": {"hp": 3.5}}})


def test_schema_version_enforced():
    assert_schema(1)
    with pytest.raises(CanonicalError):
        assert_schema(2)


def test_state_hash_deterministic_and_sensitive():
    doc_a = {"turn": 3, "units": {"u1": {"hp": 100}}}
    doc_b = {"units": {"u1": {"hp": 100}}, "turn": 3}
    doc_c = {"turn": 3, "units": {"u1": {"hp": 99}}}
    assert state_hash(doc_a) == state_hash(doc_b)
    assert state_hash(doc_a) != state_hash(doc_c)


def test_checkpoint_hash_covers_rng_and_coordinator():
    sim = {"turn": 5}
    rng_a = {"sim": [3, [0, 1, 2], 0], "agent-0": [3, [9], 0]}
    rng_b = {"sim": [3, [0, 1, 3], 0], "agent-0": [3, [9], 0]}
    assert checkpoint_hash(sim, rng_a, {"violations": 0}) == checkpoint_hash(
        sim, rng_a, {"violations": 0}
    )
    assert checkpoint_hash(sim, rng_a, {"violations": 0}) != checkpoint_hash(
        sim, rng_b, {"violations": 0}
    )
    assert checkpoint_hash(sim, rng_a, {"violations": 0}) != checkpoint_hash(
        sim, rng_a, {"violations": 1}
    )


def test_args_digest_stable():
    assert args_digest({"a": 1, "b": 2}) == args_digest({"b": 2, "a": 1})
    assert args_digest({"a": 1}) != args_digest({"a": 2})


def test_log_prefix_hash_ignores_envelope_fields():
    base = {"seq": 0, "kind": "TOOL_CALL", "args": {"x": 1}, "ts": "2026-01-01T00:00:00"}
    variant = dict(base)
    variant["ts"] = "2027-12-31T23:59:59"
    variant["duration_ms"] = 17
    assert log_prefix_hash([base]) == log_prefix_hash([variant])
    other = dict(base)
    other["args"] = {"x": 2}
    assert log_prefix_hash([base]) != log_prefix_hash([other])


def test_rng_roundtrip_preserves_sequence():
    rng = random.Random(42)
    _first = [rng.randint(0, 100) for _ in range(10)]
    doc = rng_to_doc(rng)
    expected_next = [rng.randint(0, 100) for _ in range(10)]  # the true continuation

    restored = rng_from_doc(doc)
    assert [restored.randint(0, 100) for _ in range(10)] == expected_next

    fresh = random.Random(0)
    fresh.setstate((doc[0], tuple(doc[1]), doc[2] or None))
    assert [fresh.randint(0, 100) for _ in range(10)] == expected_next


def test_rng_doc_is_canonicalizable():
    doc = rng_to_doc(random.Random(7))
    # must not raise
    canonical(doc)
