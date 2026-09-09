"""Placement selection from fresh, player-scoped native production options.

The engine supplies consequence-free legal targets. The controller only selects
among targets also present as currently owned in its projection; this is a
deterministic distance tie-break, not an adjacency or yield optimizer.
"""
from __future__ import annotations

from civ_arena.agents.strategy_directive import coordinate

PRODUCTIVE_KINDS = frozenset({'building', 'district', 'project'})
REPEATABLE_PROJECTS = frozenset({
    'PROJECT_ENHANCE_DISTRICT_CAMPUS', 'PROJECT_ENHANCE_DISTRICT_HOLY_SITE',
    'PROJECT_ENHANCE_DISTRICT_THEATER', 'PROJECT_ENHANCE_DISTRICT_COMMERCIAL_HUB',
    'PROJECT_ENHANCE_DISTRICT_HARBOR', 'PROJECT_ENHANCE_DISTRICT_INDUSTRIAL_ZONE',
    'PROJECT_ENHANCE_DISTRICT_ENCAMPMENT',
})


def placement_choice(row: dict, state: dict, *, player_id: int, city_id: str) -> dict:
    """Validate an exact offered district and retain all rejected target reasons."""
    if row.get('kind') != 'district':
        raise ValueError('placement choice requires a district option')
    placements = row.get('placements')
    if not isinstance(placements, list) or len(placements) > 256:
        raise ValueError('district placements unavailable or exceed bounded size')
    parsed = [coordinate(dest) for dest in placements]
    if len(set(parsed)) != len(parsed):
        raise ValueError('duplicate district placement')
    city = next((city for city in state['get_cities'] if city['city_id'] == city_id
                 and city.get('owner', city.get('owner_id')) == player_id), None)
    if city is None:
        raise ValueError('district placement requires observed owned city')
    cq, cr = coordinate(city['coord'])
    tiles = state.get('get_visible_map', {}).get('tiles')
    if not isinstance(tiles, dict):
        raise ValueError('district placement requires projected terrain')
    candidates, ranked = [], []
    for dest, (q, r) in sorted(zip(placements, parsed, strict=True)):
        tile = tiles.get(dest)
        owner = tile.get('owner_id', tile.get('owner')) if isinstance(tile, dict) else None
        reason = ('not_currently_owned_in_projection' if type(owner) is not int
                  or owner != player_id else
                  'observed_city_tile' if tile.get('city_id', tile.get('city')) else
                  'native_offered_and_currently_owned')
        eligible = reason == 'native_offered_and_currently_owned'
        distance = max(abs(q-cq), abs(r-cr), abs(q+r-cq-cr))
        candidates.append({'dest': dest, 'eligible': eligible, 'reason': reason,
                           'distance_from_city': distance})
        if eligible:
            ranked.append((distance, q, r, dest))
    return {'dest': min(ranked)[-1] if ranked else None, 'candidates': candidates,
            'basis': 'fresh_native_legal_targets_then_projected_owner_then_distance',
            'limits': ['no_adjacency_or_yield_optimization',
                       'native_rechecks_legality_and_consequences_before_submission']}


def production_args(policy: dict, options: list[dict], state: dict, *,
                    player_id: int, city_id: str) -> dict:
    """Build legacy-identical args, with a freshly selected target for a district."""
    item = policy['item_id']
    offered = [row for row in options if row.get('item_id') == item]
    if len(offered) != 1:
        raise ValueError('production selection is not one exact offered item')
    args = {'city_id': city_id, 'item_id': item}
    if offered[0]['kind'] == 'district':
        choice = placement_choice(offered[0], state, player_id=player_id, city_id=city_id)
        if choice['dest'] is None:
            raise ValueError('district selection has no current projected placement')
        args['dest'] = choice['dest']
    return args
