"""StrategyStore: claims + beliefs + facts, all rebuilt from the event log.

Same trust-root discipline as DiaryStore: the event log is the only durable
store, and ``from_log`` rebuilds over the truncated prefix on resume. The
``apply_*`` methods are the SINGLE validation+mutation path — the referee
calls them live and ``from_log`` calls them on the same raw args, so a
rebuilt store equals the live one by construction, never by imitation.

Claim amendment is id-stable: revising goal g2 appends revision 2 of g2 and
closes revision 1 (``valid_to_turn``), so references from predictions'
``subject_id`` and lessons' ``about`` survive amendments. The store is NOT
game state and sits deliberately outside the checkpoint content hash.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any

from civ_arena.strategy.beliefs import BeliefStore
from civ_arena.strategy.claims import (
    GOAL_STATUSES,
    MAX_CLAIM_CHARS,
    MAX_GOALS_PER_PLAYER,
    MAX_LESSONS_PER_PLAYER,
    MAX_PREDICTIONS_PER_PLAYER,
    MAX_SUBJECT_CHARS,
    METRICS,
    Goal,
    Lesson,
    Prediction,
)
from civ_arena.strategy.facts import Facts

CLAIM_TOOLS = frozenset({"set_goal", "record_prediction", "record_lesson"})
OBSERVATION_TOOLS = frozenset({"get_units", "get_cities", "get_overview"})


def _is_int(value: Any) -> bool:
    # bools ARE ints in Python; the runtime rejects them and so must we —
    # a bare True must never land in a claim as confidence/by_turn/target
    return isinstance(value, int) and not isinstance(value, bool)


def _bad_text(value: Any) -> str | None:
    if not isinstance(value, str):
        return "text must be a string"
    if not 1 <= len(value.strip()) <= MAX_CLAIM_CHARS or len(value) > MAX_CLAIM_CHARS:
        return (f"text must be 1..{MAX_CLAIM_CHARS} characters "
                f"(raw length included)")
    return None


@dataclass
class StrategyStore:
    # player_id -> claim_id -> revision history (last = current authority)
    goals: dict[int, dict[str, list[Goal]]] = field(default_factory=dict)
    predictions: dict[int, dict[str, list[Prediction]]] = field(default_factory=dict)
    # lessons never amend: one flat history, id order = creation order
    lessons: dict[int, list[Lesson]] = field(default_factory=dict)
    beliefs: BeliefStore = field(default_factory=BeliefStore)
    facts: Facts = field(default_factory=Facts)

    # ------------------------------------------------------------ read paths
    def current_goals(self, player_id: int) -> list[Goal]:
        return [
            hist[-1] for _, hist in sorted(self.goals.get(player_id, {}).items())
        ]

    def active_goals(self, player_id: int) -> list[Goal]:
        return [g for g in self.current_goals(player_id) if g.status == "active"]

    def current_predictions(self, player_id: int) -> list[Prediction]:
        return [
            hist[-1] for _, hist in
            sorted(self.predictions.get(player_id, {}).items())
        ]

    def open_predictions(self, player_id: int) -> list[Prediction]:
        """Not yet superseded and not yet past review."""
        return [p for p in self.current_predictions(player_id)
                if p.valid_to_turn == 0]

    def lesson_list(self, player_id: int) -> list[Lesson]:
        return list(self.lessons.get(player_id, []))

    def own_claim_ids(self, player_id: int) -> set[str]:
        return (set(self.goals.get(player_id, {}))
                | set(self.predictions.get(player_id, {})))

    # ---------------------------------------------------------- claim writes
    def apply_goal(
        self, player_id: int, args: dict[str, Any], turn: int, seq: int,
    ) -> dict[str, Any]:
        """Validate + apply one set_goal call. Returns the result doc."""
        if bad := _bad_text(args.get("text")):
            return self._reject("set_goal", bad)
        goal_id = args.get("goal_id", "")
        if goal_id is None:
            goal_id = ""
        if not isinstance(goal_id, str):
            return self._reject("set_goal", "goal_id must be a string")
        by_turn = args.get("by_turn", 0)
        if not _is_int(by_turn) or by_turn < 0:
            return self._reject("set_goal", "by_turn must be an integer >= 0")
        if by_turn != 0 and by_turn < turn:
            # a deadline in the past is untestable at authorship; due-now is
            # expressed as by_turn == current turn
            return self._reject("set_goal", "by_turn must be 0 or >= current turn")
        metric = args.get("metric", "")
        if metric is None:
            metric = ""
        if not isinstance(metric, str) or metric not in (METRICS | {""}):
            return self._reject(
                "set_goal", f"metric must be one of {sorted(METRICS)} or empty")
        target = args.get("target", 0)
        if not _is_int(target) or target < 0:
            return self._reject("set_goal", "target must be an integer >= 0")
        status = args.get("status", "active")
        if not isinstance(status, str) or status not in GOAL_STATUSES:
            return self._reject(
                "set_goal", f"status must be one of {sorted(GOAL_STATUSES)}")
        confidence = args.get("confidence", 50)
        if not _is_int(confidence) or not 0 <= confidence <= 100:
            return self._reject("set_goal", "confidence must be an integer 0..100")

        histories = self.goals.get(player_id)
        if goal_id == "":
            living = self._living_goals(histories)
            if living >= MAX_GOALS_PER_PLAYER:
                return self._reject(
                    "set_goal",
                    f"at most {MAX_GOALS_PER_PLAYER} undropped goals — "
                    "drop or amend one")
            if histories is None:
                histories = self.goals[player_id] = {}
            goal_id = self._next_id(histories, "g")
            record = Goal(
                goal_id=goal_id, text=args["text"], by_turn=by_turn,
                metric=metric, target=target, status=status,
                confidence=confidence, created_turn=turn, created_seq=seq,
                revision=1, valid_from_turn=turn, valid_to_turn=0,
            )
            histories[goal_id] = [record]
            return {
                "status": "accepted", "tool": "set_goal", "goal_id": goal_id,
                "revision": 1,
            }
        history = (histories or {}).get(goal_id)
        if not history:
            return self._reject("set_goal", f"unknown goal_id {goal_id!r}")
        prior = history[-1]
        history[-1] = replace(
            prior,
            valid_to_turn=max(prior.valid_from_turn, turn - 1),
        )
        record = Goal(
            goal_id=goal_id, text=args["text"], by_turn=by_turn,
            metric=metric, target=target, status=status,
            confidence=confidence, created_turn=turn, created_seq=seq,
            revision=prior.revision + 1, valid_from_turn=turn, valid_to_turn=0,
        )
        history.append(record)
        return {
            "status": "accepted", "tool": "set_goal", "goal_id": goal_id,
            "revision": record.revision,
        }

    def apply_prediction(
        self, player_id: int, args: dict[str, Any], turn: int, seq: int,
    ) -> dict[str, Any]:
        """Validate + apply one record_prediction call."""
        if bad := _bad_text(args.get("text")):
            return self._reject("record_prediction", bad)
        prediction_id = args.get("prediction_id", "")
        if prediction_id is None:
            prediction_id = ""
        if not isinstance(prediction_id, str):
            return self._reject("record_prediction",
                                "prediction_id must be a string")
        review_turn = args.get("review_turn", 0)
        if not _is_int(review_turn) or review_turn < turn:
            # a claim about the past is untestable at authorship
            return self._reject(
                "record_prediction", "review_turn must be >= current turn")
        subject_id = args.get("subject_id", "")
        if subject_id is None:
            subject_id = ""
        if not isinstance(subject_id, str) or len(subject_id) > MAX_SUBJECT_CHARS:
            return self._reject(
                "record_prediction",
                f"subject_id must be a string of at most {MAX_SUBJECT_CHARS} chars")
        metric = args.get("metric", "")
        if metric is None:
            metric = ""
        if not isinstance(metric, str) or metric not in (METRICS | {""}):
            return self._reject(
                "record_prediction",
                f"metric must be one of {sorted(METRICS)} or empty")
        target = args.get("target", 0)
        if not _is_int(target) or target < 0:
            return self._reject("record_prediction",
                                "target must be an integer >= 0")
        confidence = args.get("confidence", 50)
        if not _is_int(confidence) or not 0 <= confidence <= 100:
            return self._reject("record_prediction",
                                "confidence must be an integer 0..100")

        histories = self.predictions.get(player_id)
        if prediction_id == "":
            if histories is not None \
                    and len(histories) >= MAX_PREDICTIONS_PER_PLAYER:
                return self._reject(
                    "record_prediction",
                    f"at most {MAX_PREDICTIONS_PER_PLAYER} predictions")
            if histories is None:
                histories = self.predictions[player_id] = {}
            prediction_id = self._next_id(histories, "p")
            record = Prediction(
                prediction_id=prediction_id, text=args["text"],
                review_turn=review_turn, subject_id=subject_id,
                metric=metric, target=target, confidence=confidence,
                created_turn=turn, created_seq=seq, revision=1,
                valid_from_turn=turn, valid_to_turn=0,
            )
            histories[prediction_id] = [record]
            return {
                "status": "accepted", "tool": "record_prediction",
                "prediction_id": prediction_id, "revision": 1,
            }
        history = (histories or {}).get(prediction_id)
        if not history:
            return self._reject("record_prediction",
                                f"unknown prediction_id {prediction_id!r}")
        prior = history[-1]
        history[-1] = replace(
            prior, valid_to_turn=max(prior.valid_from_turn, turn - 1))
        record = Prediction(
            prediction_id=prediction_id, text=args["text"],
            review_turn=review_turn, subject_id=subject_id,
            metric=metric, target=target, confidence=confidence,
            created_turn=turn, created_seq=seq, revision=prior.revision + 1,
            valid_from_turn=turn, valid_to_turn=0,
        )
        history.append(record)
        return {
            "status": "accepted", "tool": "record_prediction",
            "prediction_id": prediction_id, "revision": record.revision,
        }

    def apply_lesson(
        self, player_id: int, args: dict[str, Any], turn: int, seq: int,
    ) -> dict[str, Any]:
        """Validate + apply one record_lesson call. Lessons never amend."""
        if bad := _bad_text(args.get("text")):
            return self._reject("record_lesson", bad)
        about = args.get("about", "")
        if about is None:
            about = ""
        if not isinstance(about, str) or len(about) > MAX_SUBJECT_CHARS:
            return self._reject(
                "record_lesson",
                f"about must be a string of at most {MAX_SUBJECT_CHARS} chars")
        if about != "" and about not in self.own_claim_ids(player_id):
            return self._reject("record_lesson",
                                f"about must be empty or one of your own "
                                f"goal/prediction ids (got {about!r})")
        log = self.lessons.get(player_id)
        if log is not None and len(log) >= MAX_LESSONS_PER_PLAYER:
            return self._reject(
                "record_lesson", f"at most {MAX_LESSONS_PER_PLAYER} lessons")
        if log is None:
            log = self.lessons[player_id] = []
        lesson_id = self._next_flat_id(log, "l")
        log.append(Lesson(
            lesson_id=lesson_id, text=args["text"], about=about,
            created_turn=turn, created_seq=seq,
        ))
        doc: dict[str, Any] = {
            "status": "accepted", "tool": "record_lesson",
            "lesson_id": lesson_id,
        }
        # A lesson about a DUE prediction is its verdict: recording it
        # closes the review (the instructed flow — "record_lesson your
        # verdict" — actually ends it). Amending the prediction re-opens
        # it with the new revision.
        resolved = self._resolve_due_prediction(player_id, about, turn)
        if resolved is not None:
            doc["resolved"] = resolved
        return doc

    def _resolve_due_prediction(
        self, player_id: int, about: str, turn: int,
    ) -> str | None:
        histories = self.predictions.get(player_id)
        if not about or histories is None:
            return None
        history = histories.get(about)
        if not history:
            return None
        latest = history[-1]
        if latest.valid_to_turn != 0 or latest.review_turn > turn:
            return None
        history[-1] = replace(
            latest, valid_to_turn=max(latest.valid_from_turn, turn - 1))
        return about

    def apply_claim(
        self, tool: str, player_id: int, args: dict[str, Any],
        turn: int, seq: int,
    ) -> dict[str, Any]:
        """Dispatch one claim-tool call to its apply_* path (the referee's
        single entry point; from_log uses the same one)."""
        if tool == "set_goal":
            return self.apply_goal(player_id, args, turn, seq)
        if tool == "record_prediction":
            return self.apply_prediction(player_id, args, turn, seq)
        if tool == "record_lesson":
            return self.apply_lesson(player_id, args, turn, seq)
        return self._reject(tool, f"unknown claim tool {tool!r}")

    def view_for(self, player_id: int) -> dict[str, Any]:
        """The agent-readable strategy view: exactly what get_strategy returns
        and (M11d) the turn header renders — one shape, no disagreement
        possible between a mid-turn read and the next turn's summary."""
        return {
            "goals": [g.to_doc() for g in self.current_goals(player_id)],
            "predictions": [p.to_doc()
                            for p in self.current_predictions(player_id)],
            "lessons": [lesson.to_doc()
                        for lesson in self.lesson_list(player_id)[-8:]],
            "beliefs": self.beliefs.view(player_id),
        }

    # ----------------------------------------------------- observation feed
    def note_observation(
        self, player_id: int, turn: int, seq: int,
        tool: str, digest: dict[str, Any],
    ) -> None:
        """Fold one observation digest into beliefs + facts. Tolerates absent
        keys (older digests, partial tool coverage) by design."""
        if not isinstance(digest, dict):
            return
        if tool == "get_units":
            self.beliefs.see(
                player_id, turn, seq,
                foreign_units=digest.get("foreign_units") or [])
            if _is_int(digest.get("own_units")):
                self.facts.note(player_id, turn, {"own_units": digest["own_units"]})
        elif tool == "get_cities":
            self.beliefs.see(
                player_id, turn, seq,
                foreign_cities=digest.get("foreign_cities") or [])
            sample: dict[str, Any] = {}
            if _is_int(digest.get("own_cities")):
                sample["own_cities"] = digest["own_cities"]
            if _is_int(digest.get("own_population")):
                sample["own_population"] = digest["own_population"]
            if sample:
                self.facts.note(player_id, turn, sample)
        elif tool == "get_overview":
            sample = {}
            if _is_int(digest.get("gold")):
                sample["gold"] = digest["gold"]
            if _is_int(digest.get("techs")):
                sample["techs"] = digest["techs"]
            if isinstance(digest.get("researching"), str):
                sample["researching"] = digest["researching"]
            if sample:
                self.facts.note(player_id, turn, sample)

    # ------------------------------------------------------------- rebuild
    @staticmethod
    def _namespaced(rec: dict[str, Any]) -> bool:
        """Typed namespace check. The log writer always emits these fields
        with these types on agent-facing records, so a record missing them —
        or carrying None where identity belongs — is not one of ours: an
        equal-None pair must never authorize a claim or inject a digest."""
        return (
            isinstance(rec.get("player_id"), int)
            and isinstance(rec.get("turn"), int)
            and isinstance(rec.get("seq"), int)
            and isinstance(rec.get("agent_id"), str)
            and isinstance(rec.get("match_id"), str)
            and isinstance(rec.get("game_instance_id"), str)
            and rec.get("visibility_scope") == "private_player"
            and rec.get("phase_player_id") == rec.get("player_id")
        )

    @classmethod
    def from_log(cls, records: list[dict[str, Any]]) -> StrategyStore:
        """Rebuild from the event log prefix, the DiaryStore pattern.

        Claims: every ACCEPTED claim TOOL_CALL whose immediately following
        TOOL_RESULT carries the same namespace identity (adjacency alone
        would let a foreign record authorize the preceding claim). The SAME
        ``apply_*`` path runs on the same raw args, so rebuild == live.
        Observations: the ``observed`` digest rides on the TOOL_RESULT
        itself, so no pairing is needed — the record's own (typed, privately
        scoped) namespace identifies it. Pre-M11 records have no digest and
        rebuild to an empty-but-valid store.
        """
        store = cls()
        for i, rec in enumerate(records):
            kind = rec.get("kind")
            if kind == "TOOL_RESULT" and rec.get("tool") in OBSERVATION_TOOLS \
                    and rec.get("status") == "accepted":
                observed = rec.get("observed")
                if isinstance(observed, dict) and cls._namespaced(rec):
                    store.note_observation(
                        rec["player_id"], rec["turn"], rec["seq"],
                        rec["tool"], observed)
                continue
            if kind != "TOOL_CALL" or rec.get("tool") not in CLAIM_TOOLS:
                continue
            if not cls._namespaced(rec):
                continue
            nxt = records[i + 1] if i + 1 < len(records) else None
            if nxt is None or nxt.get("kind") != "TOOL_RESULT" \
                    or nxt.get("tool") != rec.get("tool") \
                    or nxt.get("status") != "accepted" \
                    or nxt.get("player_id") != rec.get("player_id") \
                    or nxt.get("agent_id") != rec.get("agent_id") \
                    or nxt.get("turn") != rec.get("turn") \
                    or nxt.get("match_id") != rec.get("match_id") \
                    or nxt.get("game_instance_id") != rec.get("game_instance_id"):
                continue
            pid = rec.get("player_id")
            args = rec.get("args") or {}
            turn = rec.get("turn")
            if not isinstance(args, dict):
                continue
            if rec["tool"] == "set_goal":
                store.apply_goal(pid, args, turn, rec.get("seq", 0))
            elif rec["tool"] == "record_prediction":
                store.apply_prediction(pid, args, turn, rec.get("seq", 0))
            else:
                store.apply_lesson(pid, args, turn, rec.get("seq", 0))
        return store

    # ------------------------------------------------------------ internals
    @staticmethod
    def _living_goals(histories: dict[str, list[Goal]] | None) -> int:
        """Goals that still consume a cap slot: everything not DROPPED (a
        dropped goal frees its slot; its id is never reused)."""
        if not histories:
            return 0
        return sum(1 for h in histories.values() if h[-1].status != "dropped")

    @staticmethod
    def _reject(tool: str, reason: str) -> dict[str, Any]:
        return {
            "status": "rejected", "rejection": "args_invalid",
            "tool": tool, "reason": reason,
        }

    @staticmethod
    def _next_id(histories: dict[str, list[Any]], prefix: str) -> str:
        nums = [
            int(k[len(prefix):]) for k in histories
            if k.startswith(prefix) and k[len(prefix):].isdigit()
        ]
        return f"{prefix}{max(nums, default=0) + 1}"

    @staticmethod
    def _next_flat_id(records: list[Any], prefix: str) -> str:
        nums = [
            int(r.lesson_id[len(prefix):]) for r in records
            if r.lesson_id.startswith(prefix) and r.lesson_id[len(prefix):].isdigit()
        ]
        return f"{prefix}{max(nums, default=0) + 1}"
