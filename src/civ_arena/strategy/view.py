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
GOAL_CAP = 4
SEEN_CAP = 5
LESSON_TAIL = 3
RESOLVED_TAIL = 3
# Budget proof (why the last-resort loop below is unreachable today): the
# worst legal assembly after the RESOLVED/LESSONS stage drops is
# REVIEW (6 item lines + ~4 id-summary lines) x120 + GOALS 4x120 +
# LAST SEEN 5x120 + section headers ~100 = ~2380 <= MEMORY_BUDGET. The
# caps are load-bearing: raise them and re-do this arithmetic.


def _clip(text: str) -> str:
    if len(text) <= LINE_CLIP:
        return text
    return text[: LINE_CLIP - 8] + "...[cut]"


def _quote(text: str) -> str:
    return '"' + text.replace('"', "'") + '"'


def _summary_lines(ids: list[str]) -> list[str]:
    """Pack overflow ids into lines by ACTUAL character width (ids grow
    with churn — g101 is 4 chars, not the 2-3 of a fresh game — so a fixed
    per-line count clips legal ids). Whole ids per line; the clip only
    bites on an absurd single id longer than a line."""
    first = f"...and {len(ids)} more due: "
    cont = "...due, continued: "
    out: list[str] = []
    label, cur = first, ""
    for cid in ids:
        piece = cid if not cur else " " + cid
        if cur and len(label) + len(cur) + len(piece) > LINE_CLIP - 8:
            out.append(_clip(label + cur))
            label, cur = cont, cid
        else:
            cur += piece
    if cur:
        out.append(_clip(label + cur))
    return out


def _review_due(
    store: Any, player_id: int, turn: int,
    full_limit: int = REVIEW_ITEM_CAP,
) -> tuple[list[str], list[str | None], int]:
    """Returns (lines, goal-id-per-full-line, number of full item lines).
    Due claims are interleaved by DEADLINE (goals before predictions on
    ties) so neither kind can starve the other out of the item cap; full
    item lines come first and width-packed id-summary lines for EVERY
    non-full due claim follow, so a due claim is never invisible.
    ``goal_ids[i]`` is the goal id full line i renders (None for
    predictions) — the caller derives the GOALS exclusion from the lines
    that survive budget truncation. ``full_limit`` lets the caller's
    defensive truncation demote full lines back into the summaries."""
    due: list[tuple[int, int, str, str, Any]] = []
    for goal in scoring.due_goals(store, player_id, turn):
        due.append((goal.by_turn, 0, goal.goal_id, "goal", goal))
    for pred in scoring.due_predictions(store, player_id, turn):
        due.append((pred.review_turn, 1, pred.prediction_id, "prediction",
                    pred))
    due.sort(key=lambda t: (t[0], t[1], t[2]))

    lines: list[str] = []
    goal_ids: list[str | None] = []
    for _deadline, _rank, claim_id, kind_word, claim in due[:full_limit]:
        verdict = scoring.verdict(claim, store.facts, player_id, turn)
        # the DISPLAYED value is bound to the same deadline as the verdict:
        # "MISSED (gold=150)" for a goal that had 50 at its deadline would
        # contradict the sticky verdict next to it
        as_of = min(turn, scoring.deadline_turn(claim, turn) or turn)
        if verdict == scoring.SELF_ASSESS:
            remedy = ("judge it: amend (set_goal) or record_lesson"
                      if kind_word == "goal"
                      else "record_lesson your verdict")
            lines.append(_clip(
                f"- {kind_word} {claim_id} {_quote(claim.text)} "
                f"due t{as_of}: SELF-ASSESS — {remedy}"))
        else:
            value = scoring.metric_value(
                store.facts, player_id, claim.metric, as_of)
            lines.append(_clip(
                f"- {kind_word} {claim_id} {_quote(claim.text)} "
                f"due t{as_of}: {verdict.upper()} ({claim.metric}={value})"))
        goal_ids.append(claim_id if kind_word == "goal" else None)
    n_full = len(lines)
    if len(due) > full_limit:
        lines.extend(_summary_lines(
            [entry[2] for entry in due[full_limit:]]))
    return lines, goal_ids, n_full


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


def _assemble(blocks: list[Any]) -> str:
    parts = [f"{name}:\n" + "\n".join(lines)
             for name, lines in blocks if lines]
    return "\n\n".join(parts)


def render_memory(store: Any, player_id: int, turn: int) -> str:
    """The memory view for one player's turn header ('' when the store has
    nothing to say). Identity-free by construction: claim texts are the
    model's own words, ids are claim/entity ids."""
    review = ["REVIEW DUE THIS TURN", []]
    goals = ["GOALS (active)", []]
    seen = ["LAST SEEN (may be stale)", _last_seen(store, player_id)]
    lessons = ["LESSONS", _lessons(store, player_id)]
    resolved = ["RESOLVED", _resolved(store, player_id)]
    blocks: list[list] = [review, goals, seen, lessons, resolved]

    # Budget enforcement is a strictly-shrinking ladder; every rung keeps
    # the assembly consistent (recomputed from n_full/goal_limit, never
    # patched in place): drop RESOLVED, LESSONS, LAST SEEN; trim GOALS;
    # then demote full REVIEW lines back into the id summaries (the
    # summaries carry every non-full due id, so a demoted claim stays
    # visible); then shrink GOALS; then GOALS empty. Unreachable below the
    # last rung at the shipped constants — see the budget proof above.
    n_full = REVIEW_ITEM_CAP
    goal_limit = GOAL_CAP

    def refresh() -> None:
        review_lines, goal_ids, n = _review_due(
            store, player_id, turn, full_limit=n_full)
        review[1] = review_lines
        excluded = {gid for gid in goal_ids[:n] if gid is not None}
        goals[1] = _goals(store, player_id, excluded)[:goal_limit]

    refresh()
    if not any(b[1] for b in blocks):
        return ""

    def fits() -> bool:
        return len(_assemble(blocks)) <= MEMORY_BUDGET

    for victim in (resolved, lessons, seen):
        if fits():
            break
        victim[1] = []
    if not fits():
        goal_limit = 3
        refresh()
    while not fits() and n_full > 0:
        n_full -= 1
        refresh()
    while not fits() and goal_limit > 0:
        goal_limit -= 1
        refresh()
    if not fits():
        goal_limit = 0
        refresh()

    return _assemble(blocks)
