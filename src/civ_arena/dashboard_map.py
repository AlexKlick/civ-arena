"""Read-only dashboard bridge from complete event prefixes to observed maps."""

from __future__ import annotations

import json

from civ_arena import minimap


def decode(raw):
    def reject_constant(value):
        raise ValueError("non-finite map JSON number")

    return json.loads(raw, object_pairs_hook=minimap.unique_object, parse_constant=reject_constant)


CONTEXT_MARKER = "\nController context (projected observations, axial coordinates):\n"


def materialize(raw: bytes, *, player: int | None, spectator: bool, turn: int, redactor) -> dict:
    if type(turn) is not int or not 1 <= turn <= 10**9:
        raise ValueError("invalid map turn")
    if len(raw) > minimap.MAX_BYTES:
        raise ValueError("map event log exceeds read limit")
    partial_tail = bool(raw and not raw.endswith(b"\n"))
    lines = raw.splitlines(keepends=True)
    if partial_tail:
        lines = lines[:-1]
    if len(lines) > 100000:
        raise ValueError("map event count exceeds limit")
    events = []
    for line in lines:
        event = decode(line)
        if (
            not isinstance(event, dict)
            or type(event.get("seq")) is not int
            or event["seq"] != len(events)
        ):
            raise ValueError("map event sequence is invalid")
        events.append(event)
    if not events or events[0].get("kind") != "MATCH_START":
        raise ValueError("map has no initial match record")
    # A later engine turn closes the causal prefix, even if subsequent rows
    # carry stale turn labels. Never select an old-turn packet beyond it.
    cutoff = next(
        (e["seq"] for e in events if type(e.get("turn")) is int and e["turn"] > turn), len(events)
    )
    events = events[:cutoff]
    packets = []
    for event in events:
        if event.get("kind") != "HEARTBEAT" or event.get("audit") != "strategy_request":
            continue
        if not spectator and event.get("player_id") != player:
            continue
        payload = decode(event["strategy_payload_json"])
        context = payload["user_context"]
        if not isinstance(context, str) or context.count(CONTEXT_MARKER) != 1:
            raise ValueError("map request context is unavailable")
        _, state = context.split(CONTEXT_MARKER)
        state = decode(state)
        packets.append(
            {
                "event": {
                    key: event.get(key)
                    for key in ("seq", "player_id", "turn", "ts", "visibility_scope")
                },
                "request": payload,
                "projected_state": state,
            }
        )
    if not packets:
        raise ValueError("no retained model observation packets at selected turn")
    bundle = minimap.build(packets, events, player=player, spectator=spectator)
    source_digest = bundle["digest"]

    # Redact only after raw context/event custody validation. Keep every list
    # row and dict field: the dashboard's generic depth/list caps lose actors.
    def clean(value):
        if isinstance(value, str):
            return redactor.text(value)
        if isinstance(value, dict):
            return {
                redactor.text(key): ("[redacted]" if redactor.sensitive(key) else clean(item))
                for key, item in value.items()
            }
        if isinstance(value, list):
            return [clean(item) for item in value]
        return value

    display = clean(bundle)
    display.pop("digest")
    # Redacting a coordinate must not silently relocate/drop an observation.
    for seat in display["seats"]:
        for snapshot in seat["snapshots"]:
            for row in snapshot["terrain"] + snapshot["actors"]:
                minimap.coord(row["observation"]["coord"])
            graph = snapshot["graph"]
            if graph is not None:
                for decision in graph["value"].get("decisions", []):
                    minimap.coord(decision["origin"])
                    for candidate in decision.get("candidates", []):
                        minimap.coord(candidate["dest"])
                    selected = decision.get("selected")
                    if selected and "dest" in selected.get("args", {}):
                        minimap.coord(selected["args"]["dest"])
    display["dashboard_source"] = {
        "requested_turn": turn,
        "partial_trailing_record_ignored": partial_tail,
        "raw_player_scoped_bundle_sha256": source_digest,
        "redaction": "Display derivative; original source hashes refer to raw validated records.",
        "refresh": "Captured when this map page was loaded; refresh explicitly for later receipts.",
    }
    display["limits"].append(
        "Dashboard map snapshot: turn cutoff uses an event-sequence prefix. "
        "It does not refresh with the two-second action-journal polling."
    )
    if partial_tail:
        display["limits"].append(
            "An incomplete trailing event was ignored during this read. "
            "Only complete preceding event records contribute observations."
        )
    display["digest"] = minimap.digest(display)
    return display
