"""render_memory: the strategy view the turn header carries.

A pure function of (store, player_id, turn) — identity-free and
template-pure like every prompt part. Sections have a fixed budget
(MEMORY_BUDGET chars); when over, sections drop in a fixed order (RESOLVED
first, REVIEW DUE never — only item-truncated). All numbers are ints; every
line is clipped. The rendered view IS what get_strategy serves (same store,
same shapes), so a mid-turn read can never disagree with the next header.
"""

from __future__ import annotations

from typing import Any

from civ_arena.strategy import scoring

MEMORY_BUDGET = 2400
LINE_CLIP = 120
REVIEW_ITEM_CAP = 6
GOAL_CAP = 5
SEEN_CAP = 6
LESSON_TAIL = 3
RESOLVED_TAIL = 3


def _clip(text: str) -> str:
    if len(text) <= LINE_CLIP:
        return text
    return text[: LINE_CLIP - 8] + "...[cut]"


def _quote(text: str) -> str:
    return '"' + text.replace('"', "'") + '"'


def _review_due(
    store: Any, player_id: int, turn: int,
) -> tuple[list[str], set[str]]:
    """Returns (lines, goal ids rendered here). The rendered ids drive the
    GOALS dedup: a due goal that did NOT make the item cap must stay
    visible in GOALS, not vanish from both sections."""
    lines: list[str] = []
    rendered: set[str] = set()
    candidates: list[tuple[str, str, Any]] = []
    for goal in scoring.due_goals(store, player_id, turn):
        candidates.append(("goal", goal.goal_id, goal))
    for pred in scoring.due_predictions(store, player_id, turn):
        candidates.append(("prediction", pred.prediction_id, pred))
    for kind_word, _cid, claim in candidates[:REVIEW_ITEM_CAP]:
        verdict = scoring.verdict(claim, store.facts, player_id, turn)
        # the DISPLAYED value is bound to the same deadline as the verdict:
        # "MISSED (gold=150)" for a goal that had 50 at its deadline would
        # contradict the sticky verdict next to it
        as_of = min(turn, scoring.deadline_turn(claim, turn) or turn)
        claim_id = claim.goal_id if kind_word == "goal" \
            else claim.prediction_id
        if verdict == scoring.SELF_ASSESS:
            remedy = ("judge it: amend (set_goal) or record_lesson"
                      if kind_word == "goal"
                      else "record_lesson your verdict")
            lines.append(
                f"- {kind_word} {claim_id} {_quote(claim.text)} "
                f"due t{as_of}: SELF-ASSESS — {remedy}")
        else:
            value = scoring.metric_value(
                store.facts, player_id, claim.metric, as_of)
            lines.append(
                f"- {kind_word} {claim_id} {_quote(claim.text)} "
                f"due t{as_of}: {verdict.upper()} ({claim.metric}={value})")
        if kind_word == "goal":
            rendered.add(claim.goal_id)
    return [_clip(line) for line in lines], rendered


def _goals(store: Any, player_id: int, exclude: set[str]) -> list[str]:
    goals = [g for g in store.active_goals(player_id)
             if g.goal_id not in exclude]
    goals = sorted(goals, key=lambda g: (g.by_turn == 0, g.by_turn, g.goal_id))
    lines = []
    for goal in goals[:GOAL_CAP]:
        head = f"- {goal.goal_id} {_quote(goal.text)}"
        if goal.by_turn != 0:
            head += f" by t{goal.by_turn}"
        if goal.metric != "":
            head += f" ({goal.metric}>={goal.target})"
        lines.append(_clip(head + f" conf {goal.confidence}"))
    return lines


def _last_seen(store: Any, player_id: int) -> list[str]:
    lines = []
    for belief in store.beliefs.view(player_id, limit=SEEN_CAP):
        fields = belief["fields"]
        when = f"(t{belief['last_seen_turn']})"
        if belief["kind"] == "unit":
            lines.append(_clip(
                f"- {belief['entity_id']} {fields.get('type', '?')} @ "
                f"{fields.get('coord', '?')} hp{fields.get('hp_bucket', '?')} "
                f"{when}"))
        else:
            lines.append(_clip(
                f"- {belief['entity_id']} {_quote(str(fields.get('name', '?')))} "
                f"@ {fields.get('coord', '?')} "
                f"pop{fields.get('population', '?')} {when}"))
    return lines


def _lessons(store: Any, player_id: int) -> list[str]:
    lines = []
    for lesson in store.lesson_list(player_id)[-LESSON_TAIL:]:
        about = f" (about {lesson.about})" if lesson.about else ""
        lines.append(_clip(f"- {lesson.lesson_id} {_quote(lesson.text)}{about}"))
    return lines


def _resolved(store: Any, player_id: int) -> list[str]:
    lines = []
    closed = [g for g in store.current_goals(player_id)
              if g.status in ("done", "dropped")][-RESOLVED_TAIL:]
    for goal in closed:
        lines.append(_clip(
            f"- {goal.goal_id} {_quote(goal.text)} {goal.status.upper()}"))
    return lines


def _assemble(blocks: list[tuple[str, list[str]]]) -> str:
    parts = [f"{name}:\n" + "\n".join(lines)
             for name, lines in blocks if lines]
    return "\n\n".join(parts)


def render_memory(store: Any, player_id: int, turn: int) -> str:
    """The memory view for one player's turn header ('' when the store has
    nothing to say). Identity-free by construction: claim texts are the
    model's own words, ids are claim/entity ids."""
    review_lines, rendered_due = _review_due(store, player_id, turn)
    # due goals render once — but only the ones REVIEW DUE actually shows
    # (the item cap truncates): overflow due goals stay visible in GOALS
    review = ("REVIEW DUE THIS TURN", review_lines)
    goals = ("GOALS (active)", _goals(store, player_id, rendered_due))
    seen = ("LAST SEEN (may be stale)", _last_seen(store, player_id))
    lessons = ("LESSONS", _lessons(store, player_id))
    resolved = ("RESOLVED", _resolved(store, player_id))

    blocks = [review, goals, seen, lessons, resolved]
    if not any(lines for _name, lines in blocks):
        return ""

    # fixed drop order when over budget: RESOLVED, LESSONS, LAST SEEN, then
    # GOALS trimmed to 3. REVIEW DUE is never dropped — only item-truncated
    # as the last resort. (With the current per-section caps the worst
    # assembly is ~2840 chars, so only the first two stages can ever fire;
    # the deeper stages are defense-in-depth for future cap changes.)
    drop_order = [resolved, lessons, seen]
    for victim in drop_order:
        if len(_assemble(blocks)) <= MEMORY_BUDGET:
            break
        blocks = [b for b in blocks if b is not victim]
    if len(_assemble(blocks)) > MEMORY_BUDGET:
        blocks = [b if b is not goals
                  else (goals[0], goals[1][:3]) for b in blocks]
    while len(_assemble(blocks)) > MEMORY_BUDGET and len(review[1]) > 1:
        blocks = [b if b is not review
                  else (review[0], review[1][:-1]) for b in blocks]

    return _assemble(blocks)
