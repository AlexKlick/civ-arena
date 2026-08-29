"""Typed strategy claims: goals, predictions, lessons.

The authored half of the strategy plane. Claims are model-authored through
the claim tools (the write_diary precedent: a validated tool that is NOT a
game action), immutable once written — amendment appends a new revision of
the SAME id rather than minting a new one, so references (a prediction's
``subject_id``, a lesson's ``about``) survive amendments.

Canonical-safe by construction: ints and strings only (confidence is an
int 0..100, never a float), turns are the time axis, no wall-clock. The
Graphiti-M12 projection (docs/strategy-lane.md) derives its node ids and
SUPERSEDES edges from these records; nothing here depends on it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# closed vocabularies — every violation is an args_invalid rejection
METRICS = frozenset({"cities", "units", "techs", "gold", "population"})
GOAL_STATUSES = frozenset({"active", "done", "dropped"})

MAX_CLAIM_CHARS = 280
MAX_GOALS_PER_PLAYER = 32
MAX_PREDICTIONS_PER_PLAYER = 64
MAX_LESSONS_PER_PLAYER = 64
MAX_SUBJECT_CHARS = 16


@dataclass(frozen=True)
class Goal:
    """A committed objective. A goal with a deadline and a metric IS the
    decision record: the arena scores it automatically at its deadline."""

    goal_id: str
    text: str
    by_turn: int  # 0 = no deadline; else >= the turn it was set on
    metric: str  # "" = self-assessed only, else a METRICS value
    target: int  # meaningful only with a metric
    status: str  # GOAL_STATUSES
    confidence: int  # 0..100
    created_turn: int
    created_seq: int  # provenance: seq of the authoring TOOL_CALL
    revision: int  # 1-based; +1 per amendment
    valid_from_turn: int
    valid_to_turn: int  # 0 = open (current authority)

    def to_doc(self) -> dict[str, Any]:
        return {
            "goal_id": self.goal_id, "text": self.text, "by_turn": self.by_turn,
            "metric": self.metric, "target": self.target, "status": self.status,
            "confidence": self.confidence, "created_turn": self.created_turn,
            "created_seq": self.created_seq, "revision": self.revision,
            "valid_from_turn": self.valid_from_turn, "valid_to_turn": self.valid_to_turn,
        }

    @classmethod
    def from_doc(cls, doc: dict[str, Any]) -> Goal:
        return cls(
            goal_id=doc["goal_id"], text=doc["text"], by_turn=doc["by_turn"],
            metric=doc["metric"], target=doc["target"], status=doc["status"],
            confidence=doc["confidence"], created_turn=doc["created_turn"],
            created_seq=doc["created_seq"], revision=doc["revision"],
            valid_from_turn=doc["valid_from_turn"], valid_to_turn=doc["valid_to_turn"],
        )


@dataclass(frozen=True)
class Prediction:
    """A testable claim about the future, scored at its review turn."""

    prediction_id: str
    text: str
    review_turn: int  # >= the turn it was made on
    subject_id: str  # "" or an entity/claim id, e.g. "u8", "c4", "g2"
    metric: str  # "" = self-assessed only
    target: int
    confidence: int
    created_turn: int
    created_seq: int
    revision: int
    valid_from_turn: int
    valid_to_turn: int

    def to_doc(self) -> dict[str, Any]:
        return {
            "prediction_id": self.prediction_id, "text": self.text,
            "review_turn": self.review_turn, "subject_id": self.subject_id,
            "metric": self.metric, "target": self.target,
            "confidence": self.confidence, "created_turn": self.created_turn,
            "created_seq": self.created_seq, "revision": self.revision,
            "valid_from_turn": self.valid_from_turn, "valid_to_turn": self.valid_to_turn,
        }

    @classmethod
    def from_doc(cls, doc: dict[str, Any]) -> Prediction:
        return cls(
            prediction_id=doc["prediction_id"], text=doc["text"],
            review_turn=doc["review_turn"], subject_id=doc["subject_id"],
            metric=doc["metric"], target=doc["target"],
            confidence=doc["confidence"], created_turn=doc["created_turn"],
            created_seq=doc["created_seq"], revision=doc["revision"],
            valid_from_turn=doc["valid_from_turn"],
            valid_to_turn=doc["valid_to_turn"],
        )


@dataclass(frozen=True)
class Lesson:
    """A durable takeaway, optionally attached to the claim it is about."""

    lesson_id: str
    text: str
    about: str  # "" or an own goal/prediction id
    created_turn: int
    created_seq: int

    def to_doc(self) -> dict[str, Any]:
        return {
            "lesson_id": self.lesson_id, "text": self.text, "about": self.about,
            "created_turn": self.created_turn, "created_seq": self.created_seq,
        }

    @classmethod
    def from_doc(cls, doc: dict[str, Any]) -> Lesson:
        return cls(
            lesson_id=doc["lesson_id"], text=doc["text"], about=doc["about"],
            created_turn=doc["created_turn"], created_seq=doc["created_seq"],
        )
