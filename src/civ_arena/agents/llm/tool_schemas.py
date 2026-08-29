"""Static tool-schema manifest for the Messages API — the model's only
documentation of the tool surface. Hand-written by policy: descriptions are
content, not boilerplate. A drift meta-test derives the parameter and
required sets from the real registry signatures and fails on any mismatch.
The model NEVER passes idempotency_key: identical repeated actions inside a
turn dedupe automatically by content.
"""

from __future__ import annotations

from typing import Any

TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "name": "get_overview",
        "description": "General match overview visible to you: current turn, "
            "your civilization's standing (gold, research, entities) and "
            "publicly visible information about rivals.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_units",
        "description": "All units currently visible to you: yours in full "
            "detail, foreign units only what your vision observes. Ordered "
            "by unit id.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_cities",
        "description": "All cities currently visible to you: yours in full "
            "detail, foreign cities only while their tile is observed. "
            "Ordered by city id.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_visible_map",
        "description": "The map as you see it: currently observed tiles plus "
            "remembered (previously explored) tiles. Remembered tiles do not "
            "show live changes.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_available_research",
        "description": "Technologies you may research now, with costs.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_available_production",
        "description": "Items a city of yours may produce now, with costs.",
        "input_schema": {
            "type": "object",
            "properties": {
                "city_id": {"type": "string",
                            "description": "id of your city, e.g. \"c1\""},
            },
            "required": ["city_id"],
        },
    },
    {
        "name": "get_strategy",
        "description": "Your current strategy record: goals, predictions, "
            "recent lessons, and the last-known positions of foreign "
            "entities you have previously seen (stale sightings included — "
            "they vanish from live view while unobserved). The same view "
            "your next turn's header summarizes.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "move_unit",
        "description": "Move one of your units to an adjacent tile.",
        "input_schema": {
            "type": "object",
            "properties": {
                "unit_id": {"type": "string",
                            "description": "id of your unit, e.g. \"u3\""},
                "dest": {"type": "string",
                         "description": "destination tile as axial \"q,r\""},
            },
            "required": ["unit_id", "dest"],
        },
    },
    {
        "name": "attack",
        "description": "Attack a visible enemy unit with one of your units.",
        "input_schema": {
            "type": "object",
            "properties": {
                "unit_id": {"type": "string",
                            "description": "id of your attacking unit"},
                "target_id": {"type": "string",
                              "description": "id of the enemy unit"},
            },
            "required": ["unit_id", "target_id"],
        },
    },
    {
        "name": "fortify",
        "description": "Fortify one of your units in place: it defends better "
            "until it acts again.",
        "input_schema": {
            "type": "object",
            "properties": {
                "unit_id": {"type": "string", "description": "id of your unit"},
            },
            "required": ["unit_id"],
        },
    },
    {
        "name": "found_city",
        "description": "Found a city with one of your settler units at its "
            "current position.",
        "input_schema": {
            "type": "object",
            "properties": {
                "unit_id": {"type": "string",
                            "description": "id of your settler unit"},
                "name": {"type": "string",
                         "description": "optional city name"},
            },
            "required": ["unit_id"],
        },
    },
    {
        "name": "set_research",
        "description": "Set your civilization's current research.",
        "input_schema": {
            "type": "object",
            "properties": {
                "tech_id": {"type": "string",
                            "description": "technology id, e.g. \"MINING\""},
            },
            "required": ["tech_id"],
        },
    },
    {
        "name": "set_city_production",
        "description": "Set what one of your cities produces.",
        "input_schema": {
            "type": "object",
            "properties": {
                "city_id": {"type": "string", "description": "id of your city"},
                "item_id": {"type": "string",
                            "description": "item id, e.g. \"WARRIOR\", "
                                           "\"SETTLER\", \"MONUMENT\""},
            },
            "required": ["city_id", "item_id"],
        },
    },
    {
        "name": "purchase",
        "description": "Spend gold to buy an item in one of your cities "
            "immediately.",
        "input_schema": {
            "type": "object",
            "properties": {
                "city_id": {"type": "string", "description": "id of your city"},
                "item_id": {"type": "string", "description": "item to buy"},
            },
            "required": ["city_id", "item_id"],
        },
    },
    {
        "name": "end_turn",
        "description": "Finish your turn. ALWAYS call this when you are done; "
            "a turn without it stalls.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "write_diary",
        "description": "Store one private note for your next turns (plans, "
            "observations, opponent reads). Max 2000 characters, non-empty; "
            "the last write in a turn replaces earlier ones. Only you ever "
            "read it.",
        "input_schema": {
            "type": "object",
            "properties": {
                "text": {"type": "string",
                         "description": "the note to remember",
                         "maxLength": 2000},
            },
            "required": ["text"],
        },
    },
    {
        "name": "set_goal",
        "description": "Commit or revise a goal. With a deadline (by_turn) "
            "and a metric the arena scores it automatically when due; "
            "without both you judge it yourself. Pass goal_id to revise an "
            "existing goal — the id stays stable across revisions. Max 280 "
            "characters.",
        "input_schema": {
            "type": "object",
            "properties": {
                "text": {"type": "string",
                         "description": "the goal, phrased as an outcome",
                         "maxLength": 280},
                "goal_id": {"type": "string", "maxLength": 16,
                            "description": "id of the goal to revise, e.g. "
                                           "\"g1\"; empty to create"},
                "by_turn": {"type": "integer",
                            "description": "deadline turn, 0 for none"},
                "metric": {"type": "string",
                           "description": "one of \"cities\", \"units\", "
                                          "\"techs\", \"gold\", \"population\", "
                                          "or empty for self-assessment"},
                "target": {"type": "integer",
                           "description": "metric value that means the goal "
                                          "is met"},
                "status": {"type": "string",
                           "description": "\"active\", \"done\", or "
                                          "\"dropped\""},
                "confidence": {"type": "integer",
                               "description": "your confidence, 0 to 100"},
            },
            "required": ["text"],
        },
    },
    {
        "name": "record_prediction",
        "description": "Record a testable claim about the future with the "
            "turn it should be judged on. With a metric the arena scores it "
            "at review_turn; otherwise you self-assess when it comes due — "
            "record a lesson about it to close its review. Pass "
            "prediction_id to revise (revising re-opens it).",
        "input_schema": {
            "type": "object",
            "properties": {
                "text": {"type": "string",
                         "description": "the prediction, phrased testably",
                         "maxLength": 280},
                "review_turn": {"type": "integer",
                                "description": "turn to judge the prediction "
                                               "on"},
                "prediction_id": {"type": "string", "maxLength": 16,
                                  "description": "id of the prediction to "
                                                 "revise; empty to create"},
                "subject_id": {"type": "string", "maxLength": 16,
                               "description": "optional entity or claim id "
                                              "this is about, e.g. \"u8\", "
                                              "\"c4\", \"g2\""},
                "metric": {"type": "string",
                           "description": "one of \"cities\", \"units\", "
                                          "\"techs\", \"gold\", \"population\", "
                                          "or empty for self-assessment"},
                "target": {"type": "integer",
                           "description": "metric value that means the "
                                          "prediction held"},
                "confidence": {"type": "integer",
                               "description": "your confidence, 0 to 100"},
            },
            "required": ["text", "review_turn"],
        },
    },
    {
        "name": "record_lesson",
        "description": "Record a durable takeaway — what worked, what "
            "didn't, what to watch for. Attach it to one of your goals or "
            "predictions: a lesson about a due prediction is its verdict "
            "and closes that review. Lessons persist across turns and are "
            "summarized in your header.",
        "input_schema": {
            "type": "object",
            "properties": {
                "text": {"type": "string",
                         "description": "the lesson",
                         "maxLength": 280},
                "about": {"type": "string", "maxLength": 16,
                          "description": "optional goal/prediction id this "
                                         "lesson is about"},
            },
            "required": ["text"],
        },
    },
]
