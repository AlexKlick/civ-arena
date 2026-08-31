"""M16b — the LLM proposer: untrusted ranking over the option library.

The model NEVER selects an action and NEVER widens the search's choice
set. It sees a compact, identity-free rendering of the planner's OWN
belief state plus the option menu (with live initiation/termination
booleans), and returns a JSON ranking. ``compile_proposal`` reduces that
to a prior over options that are LEGAL RIGHT NOW (initiation-true, known
id, deduped, order preserved) — everything else the model said is
dropped. On any parse or shape failure the proposal is EMPTY and search
proceeds unprimed: a broken proposer costs a little exploration order,
never a match.

The consumer (search): the prior orders FIRST VISITS under the untried-
first rule — with a budget smaller than the candidate set, the proposer
literally chooses which options get explored at all; with a full budget
its influence fades to visit order. That is the deliberate M16b shape of
"LLM proposes, search decides" — and it is exactly measurable: proposer
ON vs OFF at equal budget is the Experiment-5 arm.

Assumptions/contingencies text the model returns ride the trace artifact
for analysis; nothing downstream consumes them (no dead config knobs).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from civ_arena.game.sim.state import SimState
from civ_arena.planner.options import OPTIONS

OPTION_MENU: dict[str, str] = {
    "expand": "grow to 3+ cities: found with settlers, march settlers to spots",
    "develop": "build the remaining buildings in every city",
    "defend": "respond to nearby foreign military: attack in range, walls, buy warriors",
    "rush": "send the military at known foreign units/cities",
    "scout_frontier": "push scouts toward unexplored tiles",
    "tech_race": "keep research queued, monument/granary economy",
    "economy": "granary/monument focus, buy monuments when rich",
    "fortify_line": "fortify all military, walls production",
}

PROPOSER_SYSTEM = (
    "You are a strategy proposer for a civilization-style planning agent. "
    "You NEVER act; you rank strategies. Reply with ONLY a JSON object: "
    '{"ranked": ["option_id", ...], "assumptions": ["..."], '
    '"contingencies": ["..."]}. ranked lists option_ids from the menu, '
    "best first. No prose outside the JSON."
)


@dataclass
class Proposal:
    """A compiled, LEGAL-now prior. Empty = unprimed search."""

    ranked: list[str] = field(default_factory=list)
    raw_assumptions: list[str] = field(default_factory=list)
    raw_contingencies: list[str] = field(default_factory=list)

    @property
    def top(self) -> str | None:
        return self.ranked[0] if self.ranked else None


def render_view(state: SimState, pid: int) -> str:
    """Compact belief-derived strategy view: the proposer's ONLY world."""
    player = state.player(pid)
    cities = [c for c in state.cities.values() if c["owner"] == pid]
    units: dict[str, int] = {}
    for u in state.units.values():
        if u["owner"] == pid:
            units[u["type"]] = units.get(u["type"], 0) + 1
    foreign_units = sorted(
        (u for u in state.units.values() if u["owner"] != pid),
        key=lambda u: u["unit_id"])
    foreign_cities = sorted(
        (c for c in state.cities.values() if c["owner"] != pid),
        key=lambda c: c["city_id"])
    lines = [
        f"turn={state.turn} gold={player['gold']}",
        f"cities={len(cities)} population={sum(c['population'] for c in cities)}",
        f"techs_done={len(player['researched'])} researching={player['researching'] or 'none'}",
        "units=" + (",".join(f"{t}x{n}" for t, n in sorted(units.items())) or "none"),
        "idle_cities=" + str(sum(1 for c in cities if not c["production_queue"])),
    ]
    if foreign_units:
        sample = ", ".join(
            f"{u['type']}@{u['q']},{u['r']}(hp{u['hp']})" for u in foreign_units[:6])
        lines.append(f"known_foreign_units({len(foreign_units)})={sample}")
    if foreign_cities:
        lines.append("known_foreign_cities=" + ", ".join(
            f"{c['name']}@{c['q']},{c['r']}" for c in foreign_cities[:4]))
    return "\n".join(lines)


def render_menu(state: SimState, pid: int) -> str:
    rows = []
    for oid in sorted(OPTIONS):
        option = OPTIONS[oid]
        rows.append(
            f"{oid}: {OPTION_MENU[oid]} | available_now="
            f"{option.initiation(state, pid)}")
    return "\n".join(rows)


def build_request(state: SimState, pid: int) -> dict[str, Any]:
    """The exact messages payload for the proposer call."""
    return {
        "system": PROPOSER_SYSTEM,
        "messages": [{
            "role": "user",
            "content": (
                f"STRATEGY VIEW\n{render_view(state, pid)}\n\n"
                f"OPTION MENU\n{render_menu(state, pid)}\n\n"
                "Rank the strategies you would pursue now, best first. "
                "Only rank options with available_now=true."),
        }],
    }


def compile_proposal(text: str, state: SimState, pid: int) -> Proposal:
    """Model text -> LEGAL prior. Fail-soft: anything unreadable, non-object,
    or non-list yields the empty proposal; unknown or not-currently-legal
    option ids are dropped, duplicates keep first rank."""
    try:
        doc = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return Proposal()
    if not isinstance(doc, dict):
        return Proposal()
    ranked_raw = doc.get("ranked")
    if not isinstance(ranked_raw, list):
        return Proposal()

    ranked: list[str] = []
    for oid in ranked_raw:
        if (not isinstance(oid, str) or oid in ranked
                or oid not in OPTIONS):
            continue
        if OPTIONS[oid].initiation(state, pid):
            ranked.append(oid)

    def _strings(key: str) -> list[str]:
        val = doc.get(key, [])
        return [x for x in val if isinstance(x, str)][:8] \
            if isinstance(val, list) else []

    return Proposal(ranked=ranked,
                    raw_assumptions=_strings("assumptions"),
                    raw_contingencies=_strings("contingencies"))
