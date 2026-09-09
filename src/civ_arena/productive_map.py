"""Closed historical production annotations from paired authoritative tool events."""

from __future__ import annotations

import re

from civ_arena.canonical import args_digest

IDENTITY = (
    "match_id",
    "game_instance_id",
    "player_id",
    "agent_id",
    "turn",
    "phase_player_id",
    "visibility_scope",
    "tool",
    "idempotency_key",
)
RECEIPT_FIELDS = {
    "kind",
    "item_id",
    "production_hash",
    "plot_index",
    "district_index",
    "city_id",
    "dest",
    "verification",
    "observed",
    "completion_proven",
    "coverage",
}
STATE_FIELDS = {
    "production_hash",
    "plot_index",
    "district_index",
    "owner_id",
    "belongs_to_city",
    "consequence_free",
}
TYPE = re.compile(r"(DISTRICT|PROJECT)_[A-Z][A-Z0-9_]{0,63}")


def valid_receipt(call, result, player):
    """Admission only. Any uncertainty returns None, never a speculative marker."""
    from civ_arena.minimap import coord

    args = call.get("args")
    if not isinstance(args, dict) or set(args) - {"city_id", "item_id", "dest"}:
        return None
    item = args.get("item_id")
    if not isinstance(item, str) or TYPE.fullmatch(item) is None:
        return None
    kind = "district" if item.startswith("DISTRICT_") else "project"
    city = args.get("city_id")
    if not isinstance(city, str) or re.fullmatch(rf"c{player}:(0|[1-9]\d{{0,9}})", city) is None:
        return None
    if call.get("args_digest") != args_digest(args):
        return None
    if kind == "district":
        try:
            coord(args.get("dest"))
        except ValueError:
            return None
    elif "dest" in args:
        return None
    if (
        result.get("status") != "accepted"
        or result.get("duplicate") is not False
        or result.get("rejection") is not None
    ):
        return None
    value = result.get("result_doc")
    if not isinstance(value, dict) or value.get("tool") != "set_city_production":
        return None
    receipt = value.get("production_readback")
    if not isinstance(receipt, dict) or set(receipt) != RECEIPT_FIELDS:
        return None
    expected = {
        "kind": kind,
        "item_id": item,
        "city_id": city,
        "dest": args.get("dest"),
        "verification": "subsequent_exact_queue_and_placement_read",
        "coverage": "separate_observation_not_mod_digest_or_mutation_ledger",
    }
    if (
        any(receipt.get(k) != v for k, v in expected.items())
        or receipt.get("completion_proven") is not False
    ):
        return None
    observed = receipt.get("observed")
    if not isinstance(observed, dict) or set(observed) != STATE_FIELDS:
        return None
    for field, lower, upper in (
        ("production_hash", -(2**53) + 1, 2**53 - 1),
        ("plot_index", -1, 10000000),
        ("district_index", -1, 1000000),
    ):
        number = receipt[field]
        if (
            type(number) is not int
            or not lower <= number <= upper
            or type(observed[field]) is not int
            or observed[field] != number
        ):
            return None
    if receipt["production_hash"] == 0:
        return None
    if kind == "district":
        if (
            receipt["plot_index"] < 0
            or receipt["district_index"] < 0
            or type(observed["owner_id"]) is not int
            or observed["owner_id"] != player
            or observed["belongs_to_city"] is not True
            or observed["consequence_free"] is not True
        ):
            return None
    elif (
        receipt["plot_index"] != -1
        or receipt["district_index"] != -1
        or type(observed["owner_id"]) is not int
        or observed["owner_id"] != -1
        or observed["belongs_to_city"] is not False
        or observed["consequence_free"] is not False
    ):
        return None
    return {
        "kind": kind,
        "city_id": city,
        "item_id": item,
        "coord": args.get("dest"),
        "label": "placement observed" if kind == "district" else "queued",
        "completion_proven": False,
        "coverage": receipt["coverage"],
    }


def project(events, player):
    """Filter private scope before joining; only adjacent exact-identity pairs qualify.

    Referee emits the two records synchronously. Missing/mismatched results are
    journal entries with no geometry. Original record digests retain custody.
    """
    from civ_arena.minimap import digest, integer

    selected = {
        e["seq"]: e for e in events if type(e.get("player_id")) is int and e["player_id"] == player
    }
    output = []
    for seq, call in sorted(selected.items()):
        if call.get("kind") != "TOOL_CALL" or call.get("tool") != "set_city_production":
            continue
        args = call.get("args", {})
        item = args.get("item_id") if isinstance(args, dict) else None
        if not isinstance(item, str) or TYPE.fullmatch(item) is None:
            continue
        integer(call["turn"], "production call turn", 1)
        result = selected.get(seq + 1)
        joined = bool(
            result
            and result.get("kind") == "TOOL_RESULT"
            and all(
                type(call.get(k)) is type(result.get(k)) and call.get(k) == result.get(k)
                for k in IDENTITY
            )
        )
        valid_identity = (
            call.get("visibility_scope") == "private_player"
            and type(call.get("phase_player_id")) is int
            and call["phase_player_id"] == player
            and isinstance(call.get("agent_id"), str)
            and isinstance(call.get("idempotency_key"), str)
            and bool(call["idempotency_key"])
        )
        admission = valid_receipt(call, result, player) if joined and valid_identity else None
        status = "unconfirmed"
        if joined:
            status = (
                "rejected"
                if result.get("status") == "rejected"
                else "duplicate"
                if result.get("duplicate") is True
                else "accepted_without_verified_readback"
                if result.get("status") == "accepted"
                else "ambiguous"
            )
        if admission:
            status = "placement_observed" if admission["kind"] == "district" else "queue_observed"
        output.append(
            {
                "status": status,
                "item_id": item,
                "city_id": args.get("city_id")
                if isinstance(args.get("city_id"), str) and len(args["city_id"]) <= 128
                else None,
                "admission": admission,
                "call": {
                    "seq": seq,
                    "turn": call["turn"],
                    "player_id": player,
                    "event_sha256": digest(call),
                    "ts": call.get("ts"),
                },
                "result": {
                    "seq": result["seq"],
                    "turn": result["turn"],
                    "player_id": player,
                    "event_sha256": digest(result),
                    "ts": result.get("ts"),
                }
                if joined
                else None,
            }
        )
    if len(output) > 10000:
        raise ValueError("productive receipt limit")
    return output
