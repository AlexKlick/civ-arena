"""Pure, bounded voluntary-agreement reducer and recipient projection.

ServerContext and completed-seat inputs are trusted owner inputs, never model
arguments. This module checks their structure; it cannot certify a live lease or
engine turn. The future facade must perform those checks before calling it.
No disk writes, observations, automatic game enforcement, or compliance verdicts.
"""
from __future__ import annotations

import copy
import json
import re
from dataclasses import asdict, dataclass
from typing import Any

from civ_arena.canonical import args_digest, canonical

MAX_COMMAND_CHARS = 4096
MAX_CONSENT_CHARS = 1600
MAX_RECORDS = 10000
MAX_OPERATIONS_PER_TURN = 4
MAX_OFFERS_PER_TURN = 2
MAX_OPEN_OFFERS = 8
MAX_ACTIVE_TREATIES = 8
MAX_PROJECTED_MESSAGES = 16
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}\Z")


class ProtocolError(ValueError):
    """Categorical rejection; caller data is not copied into error text."""


@dataclass(frozen=True)
class ServerContext:
    match_id: str
    author: int
    turn: int
    lease_id: str


def _require(value: bool, reason: str) -> None:
    if not value:
        raise ProtocolError(reason)


def _integer(value: Any, low: int = 0, high: int = 1000000) -> bool:
    return type(value) is int and low <= value <= high


def _text(value: Any, limit: int) -> bool:
    return isinstance(value, str) and 0 < len(value) <= limit and bool(value.strip())


def _identifier(value: Any) -> bool:
    return isinstance(value, str) and _ID.fullmatch(value) is not None


def _fields(doc: Any, required: set[str], optional: set[str] = frozenset()) -> None:
    _require(type(doc) is dict and required <= set(doc) <= required | optional,
             "invalid_fields")


def _bounded_json(value: Any, depth: int = 0, count: list[int] | None = None) -> None:
    count = [0] if count is None else count
    count[0] += 1
    _require(depth <= 12 and count[0] <= 256, "payload_complexity")
    _require(type(value) in (dict, list, str, int, bool, type(None)), "invalid_json_type")
    if isinstance(value, str):
        try:
            value.encode("utf-8")
        except UnicodeError:
            raise ProtocolError("invalid_unicode") from None
        _require(len(value) <= MAX_COMMAND_CHARS, "payload_size")
    elif isinstance(value, dict):
        for key, item in value.items():
            _require(type(key) is str and len(key) <= 64, "invalid_key")
            _bounded_json(item, depth + 1, count)
    elif isinstance(value, list):
        for item in value:
            _bounded_json(item, depth + 1, count)


def parse_command(raw: str | dict) -> dict:
    """Strict raw JSON parser; dict callers must already reject duplicate JSON keys."""
    def pairs(items: list[tuple]) -> dict:
        doc = {}
        for key, value in items:
            _require(key not in doc, "duplicate_json_key")
            doc[key] = value
        return doc

    def constant(_value: str) -> None:
        raise ProtocolError("invalid_json_number")

    if isinstance(raw, str):
        _require(len(raw) <= MAX_COMMAND_CHARS, "payload_size")
        try:
            raw = json.loads(raw, object_pairs_hook=pairs, parse_constant=constant)
        except (json.JSONDecodeError, RecursionError):
            raise ProtocolError("invalid_json") from None
    _bounded_json(raw)
    _require(type(raw) is dict, "invalid_command")
    _require(len(canonical(raw)) <= MAX_COMMAND_CHARS, "payload_size")
    return copy.deepcopy(raw)


class DiplomacyStore:
    """Apply atomically in memory; expose copies; replay exact owner-supplied inputs.

    The future durable caller must persist prepared accepted records before ack.
    This offline reducer alone does not supply that persistence boundary.
    """

    def __init__(self, match_id: str, players: tuple[int, ...] = (0, 1)):
        _require(_identifier(match_id), "invalid_match")
        _require(type(players) is tuple and 2 <= len(players) <= 8
                 and all(_integer(p, 0, 63) for p in players)
                 and list(players) == sorted(set(players)), "invalid_roster")
        self.match_id, self.players = match_id, players
        # Fixed equitable partitions never borrow unused capacity from other
        # private channels. The sum stays <= MAX_RECORDS without consulting a
        # shared admission counter. Public lifecycle work has reserved capacity.
        buckets = ["public", "round_boundary", *[f"begin:{p}" for p in players],
                   *[f"private:{p}-{q}" for p in players for q in players if p < q]]
        _require(_integer(MAX_RECORDS, len(buckets)), "invalid_record_capacity")
        quota = MAX_RECORDS // len(buckets)
        self._record_limits = dict.fromkeys(buckets, quota)
        self._record_counts = dict.fromkeys(buckets, 0)
        self._round = 0
        self._offers: dict[str, dict] = {}
        self._views: dict[str, list[dict]] = {str(p): [] for p in players}
        self._pending: dict[str, list[dict]] = {str(p): [] for p in players}
        self._begun: dict[str, str] = {}
        self._dedupe: dict[str, dict] = {}
        self._counters: dict[str, int] = {}
        self._costs: dict[str, dict] = {}
        self._records: list[dict] = []

    def _ctx(self, ctx: ServerContext) -> None:
        _require(type(ctx) is ServerContext and ctx.match_id == self.match_id
                 and type(ctx.author) is int and ctx.author in self.players
                 and _integer(ctx.turn, 1) and ctx.turn == self._round + 1
                 and _identifier(ctx.lease_id), "invalid_server_context")

    def _record_bucket(self, kind: str, inputs: dict, result: dict) -> str:
        if kind == "round_boundary":
            return "round_boundary"
        if kind == "begin_turn":
            return f"begin:{inputs['context']['author']}"
        command = inputs["command"]
        if command["op"] == "message":
            if command["audience"]["kind"] == "public":
                return "public"
            readers = sorted([inputs["context"]["author"],
                              *command["audience"]["recipients"]])
        else:
            readers = result["offer"]["consent"]["participants"]
        return "private:" + "-".join(map(str, readers))

    def _record(self, kind: str, inputs: dict, result: dict) -> dict:
        bucket = self._record_bucket(kind, inputs, result)
        _require(self._record_counts[bucket] < self._record_limits[bucket], "record_limit")
        self._record_counts[bucket] += 1
        record = {"seq": len(self._records), "kind": kind,
                  "inputs": copy.deepcopy(inputs), "result": copy.deepcopy(result)}
        self._records.append(record)
        return copy.deepcopy(result)

    def _commit(self, draft: DiplomacyStore) -> None:
        self.__dict__ = draft.__dict__

    def begin_turn(self, ctx: ServerContext) -> dict:
        """Deliver once at this trusted own-lease boundary; no model-supplied identity."""
        self._ctx(ctx)
        key = f"{ctx.author}:{ctx.turn}"
        if key in self._begun:
            _require(self._begun[key] == ctx.lease_id, "lease_changed")
            return {"status": "accepted", "delivered": 0}
        draft = copy.deepcopy(self)
        pid = str(ctx.author)
        delivered = len(draft._pending[pid])
        draft._views[pid].extend(draft._pending[pid])
        draft._pending[pid] = []
        draft._begun[key] = ctx.lease_id
        result = draft._record("begin_turn", {"context": asdict(ctx)},
                               {"status": "accepted", "delivered": delivered})
        self._commit(draft)
        return result

    def apply(self, ctx: ServerContext, raw: str | dict) -> dict:
        self._ctx(ctx)
        _require(self._begun.get(f"{ctx.author}:{ctx.turn}") == ctx.lease_id,
                 "turn_not_begun")
        command = parse_command(raw)
        _require(_identifier(command.get("command_key")), "invalid_command_key")
        key = f"{ctx.author}:{command['command_key']}"
        digest = args_digest(command)
        if key in self._dedupe:
            entry = self._dedupe[key]
            _require(entry["command_digest"] == digest, "command_key_conflict")
            return copy.deepcopy(entry["result"])
        draft = copy.deepcopy(self)
        cost = draft._costs.setdefault(f"{ctx.author}:{ctx.turn}", {"operations": 0, "offers": 0})
        _require(cost["operations"] < MAX_OPERATIONS_PER_TURN, "operation_limit")
        if command.get("op") in ("offer", "counteroffer"):
            _require(cost["offers"] < MAX_OFFERS_PER_TURN, "offer_limit")
            cost["offers"] += 1
        result = draft._apply(ctx, command)
        cost["operations"] += 1
        draft._dedupe[key] = {"command_digest": digest, "result": copy.deepcopy(result)}
        draft._record("command", {"context": asdict(ctx), "command": command}, result)
        self._commit(draft)
        return copy.deepcopy(result)

    def _audience(self, value: dict, author: int, *, private: bool = False) -> list[int]:
        _fields(value, {"kind", "recipients"})
        recipients = value["recipients"]
        _require(type(recipients) is list and all(type(p) is int for p in recipients)
                 and recipients == sorted(set(recipients)) and author not in recipients
                 and all(p in self.players for p in recipients), "invalid_audience")
        if value["kind"] == "public" and not private:
            _require(recipients == [p for p in self.players if p != author], "invalid_audience")
        else:
            _require(value["kind"] == "private" and len(recipients) == 1, "invalid_audience")
        return sorted([author, *recipients])

    def _id(self, kind: str, readers: list[int], public: bool = False) -> str:
        channel = "public" if public else "-".join(map(str, readers))
        key = f"{kind}:{channel}"
        self._counters[key] = self._counters.get(key, 0) + 1
        return f"{key}:{self._counters[key]}"

    def _notify(self, readers: list[int], author: int | None, item: dict) -> None:
        for pid in readers:
            target = self._views if pid == author else self._pending
            target[str(pid)].append(copy.deepcopy(item))

    def _validate_terms(self, terms: dict, parties: list[int]) -> None:
        _fields(terms, {"version", "enforcement", "activation", "duration_rounds", "obligations"})
        _require(type(terms["version"]) is int and terms["version"] == 1
                 and terms["enforcement"] == "voluntary"
                 and terms["activation"] == "next_complete_round_boundary"
                 and _integer(terms["duration_rounds"], 1, 60), "unsupported_terms")
        obligations = terms["obligations"]
        _require(type(obligations) is list and 1 <= len(obligations) <= 8, "invalid_obligations")
        ids = set()
        for obligation in obligations:
            _require(type(obligation) is dict and _identifier(obligation.get("id"))
                     and obligation["id"] not in ids, "invalid_obligation_id")
            ids.add(obligation["id"])
            base = {"id", "actor", "kind", "condition", "evidence_policy"}
            kind = obligation.get("kind")
            if kind == "refrain_from_attack":
                _fields(obligation, base | {"beneficiary"})
                _require(type(obligation["beneficiary"]) is int
                         and obligation["beneficiary"] in parties
                         and obligation["beneficiary"] != obligation["actor"]
                         and obligation["evidence_policy"] == "recipient_visible_only",
                         "invalid_obligation")
            elif kind == "send_report":
                _fields(obligation, base | {"topic", "to", "due_after_activation_rounds"})
                _require(_identifier(obligation["topic"])
                         and type(obligation["to"]) is list
                         and obligation["to"] == [p for p in parties if p != obligation["actor"]]
                         and all(type(p) is int for p in obligation["to"])
                         and _integer(obligation["due_after_activation_rounds"], 1,
                                      terms["duration_rounds"])
                         and obligation["evidence_policy"] == "protocol_delivery",
                         "invalid_obligation")
            elif kind == "statement":
                _fields(obligation, base | {"text"})
                _require(_text(obligation["text"], 600)
                         and obligation["evidence_policy"] == "unadjudicated",
                         "invalid_obligation")
            else:
                raise ProtocolError("unsupported_obligation")
            _require(type(obligation["actor"]) is int and obligation["actor"] in parties,
                     "invalid_obligation_actor")
            _fields(obligation["condition"], {"kind"})
            _require(obligation["condition"]["kind"] == "always", "unsupported_condition")

    def _consent(self, ctx: ServerContext, command: dict) -> tuple[dict, str]:
        readers = self._audience(command["audience"], ctx.author, private=True)
        parties = command["participants"]
        _require(type(parties) is list and all(type(p) is int for p in parties)
                 and parties == readers, "invalid_participants")
        expiry = command["expires_after_round"]
        _require(_integer(expiry, self._round + 2, self._round + 60), "invalid_expiry")
        self._validate_terms(command["terms"], parties)
        # Canonical audience is reader membership, independent of which party proposes.
        consent = {"protocol_version": 1, "match_id": self.match_id, "participants": parties,
                   "audience": {"kind": "private", "readers": readers},
                   "expires_after_round": expiry, "terms": command["terms"]}
        _require(len(canonical(consent)) <= MAX_CONSENT_CHARS, "consent_size")
        return consent, args_digest(consent)

    def _visible_offer(self, author: int, oid: str) -> dict | None:
        for notice in reversed(self._views[str(author)]):
            if notice.get("kind") == "offer" and notice["offer"]["offer_id"] == oid:
                return notice["offer"]
        return None

    def _lookup(self, ctx: ServerContext, command: dict) -> dict:
        oid = command.get("offer_id")
        _require(isinstance(oid, str), "resource_unavailable")
        offer = self._offers.get(oid)
        visible = self._visible_offer(ctx.author, oid)
        _require(offer is not None and visible is not None
                 and ctx.author in offer["consent"]["participants"], "resource_unavailable")
        _require(type(command.get("revision")) is int
                 and command["revision"] == offer["revision"] == visible["revision"]
                 and command.get("terms_hash") == offer["terms_hash"]
                 and offer["status"] == "open", "offer_unavailable")
        return offer

    def _announce(self, offer: dict, author: int | None) -> None:
        self._notify(offer["consent"]["participants"], author,
                     {"kind": "offer", "offer": offer})

    def _apply(self, ctx: ServerContext, command: dict) -> dict:
        op = command.get("op")
        common = {"op", "command_key"}
        reference = {"offer_id", "revision", "terms_hash"}
        proposal = {"audience", "participants", "expires_after_round", "terms"}
        if op == "message":
            _fields(command, common | {"audience", "text"}, {"topic"})
            _require(_text(command["text"], 600), "invalid_message")
            _require("topic" not in command or _identifier(command["topic"]), "invalid_topic")
            readers = self._audience(command["audience"], ctx.author)
            mid = self._id("message", readers, command["audience"]["kind"] == "public")
            message = {"kind": "message", "message_id": mid, "author": ctx.author,
                       "turn": ctx.turn, "audience": command["audience"]["kind"],
                       "readers": readers, "text": command["text"],
                       "topic": command.get("topic"), "truth": "unverified_statement"}
            self._notify(readers, ctx.author, message)
            return {"status": "accepted", "message_id": mid}
        if op in ("offer", "counteroffer"):
            _fields(command, common | proposal | (reference if op == "counteroffer" else set()))
            old = self._lookup(ctx, command) if op == "counteroffer" else None
            consent, digest = self._consent(ctx, command)
            if old:
                _require(consent["participants"] == old["consent"]["participants"],
                         "participants_changed")
            else:
                # Channel-local capacity cannot reveal another private thread's load.
                count = sum(o["status"] == "open"
                            and o["consent"]["participants"] == consent["participants"]
                            for o in self._offers.values())
                _require(count < MAX_OPEN_OFFERS, "open_offer_limit")
            oid = old["offer_id"] if old else self._id("offer", consent["participants"])
            offer = {"offer_id": oid, "revision": old["revision"] + 1 if old else 1,
                     "terms_hash": digest, "consent": consent, "proposer": ctx.author,
                     "accepted_by": [ctx.author], "status": "open",
                     "first_round": None, "last_round": None,
                     "compliance": "unassessed"}
            self._offers[oid] = offer
            self._announce(offer, ctx.author)
            return {"status": "accepted", "offer": copy.deepcopy(offer)}
        if op in ("accept", "reject", "withdraw"):
            _fields(command, common | reference)
            offer = self._lookup(ctx, command)
            if op == "accept":
                _require(ctx.author not in offer["accepted_by"], "already_consented")
                count = sum(o["status"] in ("agreed", "active")
                            and o["consent"]["participants"] == offer["consent"]["participants"]
                            for o in self._offers.values())
                _require(count < MAX_ACTIVE_TREATIES, "active_treaty_limit")
                offer["accepted_by"] = sorted([*offer["accepted_by"], ctx.author])
                offer["status"] = "agreed"
            elif op == "withdraw":
                _require(ctx.author == offer["proposer"], "not_proposer")
                offer["status"] = "withdrawn"
            else:
                _require(ctx.author != offer["proposer"], "not_counterparty")
                offer["status"] = "rejected"
            self._announce(offer, ctx.author)
            return {"status": "accepted", "offer": copy.deepcopy(offer)}
        raise ProtocolError("unknown_operation")

    def advance_round(self, completed_seats: list[list[int]]) -> dict:
        """Trusted owner supplies released seats; only order/identity is checked here."""
        expected = [[self._round + 1, p] for p in self.players]
        _require(type(completed_seats) is list
                 and all(type(pair) is list and len(pair) == 2
                         and all(type(x) is int for x in pair) for pair in completed_seats)
                 and completed_seats == expected, "incomplete_round")
        _require(all(f"{p}:{self._round + 1}" in self._begun for p in self.players),
                 "turn_not_begun")
        draft = copy.deepcopy(self)
        draft._round += 1
        changed = []
        for oid in sorted(draft._offers):
            offer = draft._offers[oid]
            if offer["status"] == "agreed":
                offer["status"] = "active"
                offer["first_round"] = draft._round + 1
                offer["last_round"] = draft._round + offer["consent"]["terms"]["duration_rounds"]
            elif offer["status"] == "open" and (
                    offer["consent"]["expires_after_round"] <= draft._round):
                offer["status"] = "expired"
            elif offer["status"] == "active" and offer["last_round"] <= draft._round:
                offer["status"] = "elapsed"
            else:
                continue
            changed.append(oid)
            draft._announce(offer, None)
        result = draft._record("round_boundary", {"completed_seats": completed_seats},
                               {"status": "accepted", "round": draft._round,
                                "changed_offers": changed})
        self._commit(draft)
        return result

    def project(self, player: int) -> dict:
        """Only this recipient's delivered data; no global seq/digest/counts."""
        _require(type(player) is int and player in self.players, "resource_unavailable")
        notices = self._views[str(player)]
        messages, offers = [], {}
        for notice in notices:
            if notice["kind"] == "message":
                messages.append(notice)
            else:
                offers[notice["offer"]["offer_id"]] = notice["offer"]
        return copy.deepcopy({"protocol_version": 1, "match_id": self.match_id,
                              "completed_round": self._round, "cursor": len(notices),
                              "messages": messages[-MAX_PROJECTED_MESSAGES:],
                              "omitted_messages": max(0, len(messages) - MAX_PROJECTED_MESSAGES),
                              "offers": [offers[k] for k in sorted(offers)]})

    def records(self) -> list[dict]:
        """Operator-only protocol journal; not an agent-facing projection."""
        return copy.deepcopy(self._records)

    def state_digest(self) -> str:
        """Operator-only deterministic state digest, separate from engine hashes."""
        return args_digest({"schema": 1, "match_id": self.match_id, "players": list(self.players),
                            "round": self._round, "offers": self._offers, "views": self._views,
                            "pending": self._pending, "begun": self._begun,
                            "dedupe": self._dedupe, "counters": self._counters,
                            "costs": self._costs, "record_limits": self._record_limits,
                            "record_counts": self._record_counts})

    @classmethod
    def from_records(cls, match_id: str, players: tuple[int, ...], records: list[dict]
                     ) -> DiplomacyStore:
        """Re-execute and compare every complete input/result; no model or game calls."""
        _require(type(records) is list and len(records) <= MAX_RECORDS, "invalid_records")
        store = cls(match_id, players)
        for seq, record in enumerate(records):
            _fields(record, {"seq", "kind", "inputs", "result"})
            _require(type(record["seq"]) is int and record["seq"] == seq, "record_sequence")
            inputs, kind = record["inputs"], record["kind"]
            if kind in ("begin_turn", "command"):
                _fields(inputs, {"context"} | ({"command"} if kind == "command" else set()))
                _fields(inputs["context"], {"match_id", "author", "turn", "lease_id"})
                ctx = ServerContext(**inputs["context"])
                if kind == "begin_turn":
                    store.begin_turn(ctx)
                else:
                    store.apply(ctx, inputs["command"])
            elif kind == "round_boundary":
                _fields(inputs, {"completed_seats"})
                store.advance_round(inputs["completed_seats"])
            else:
                raise ProtocolError("unknown_record_kind")
            try:
                equal = (len(store._records) == seq + 1
                         and canonical(store._records[-1]) == canonical(record))
            except (TypeError, RecursionError):
                raise ProtocolError("invalid_record_json") from None
            _require(equal, "record_result_mismatch")
        return store
