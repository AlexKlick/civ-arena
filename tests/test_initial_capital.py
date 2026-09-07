"""Cityless opening continuation; projected fixtures only, no provider or native IO."""
import asyncio
import json
from dataclasses import replace

import pytest

from civ_arena.arena.referee import MatchAborted
from fakes import use
from test_adaptive_context import harness
from test_growth_integration import GrowthFacade, setup
from test_growth_policy import city, unit
from test_strategic_controller import advance


class CapitalFacade(GrowthFacade):
    def __init__(self):
        super().__init__()
        self.units = [u for u in self.units if u['unit_id'] != 'b_escort']
        self.cities = []
        self.found_status = 'accepted'

    async def found_city(self, unit_id, **kwargs):
        if self.found_status == 'rejected':
            self.calls.append(('found_city', unit_id))
            return {'status': 'rejected', 'rejection': 'illegal_site'}
        return await super().found_city(unit_id, **kwargs)


def capital_setup(*, frozen=False, directive=None, enabled=True):
    directive = directive if directive is not None else {
        'tactical_overrides': [{'unit_id': 'settler', 'action': 'move', 'dest': '1,0'}]}
    ctl, rt, model, _, records = setup(frozen=frozen, directive=directive)
    model.script.append([use('submit_directive', {'version': 1})])
    ctl.growth_autopilot = enabled
    return ctl, rt, model, CapitalFacade(), records


def reports(records):
    return [r for r in records if r['audit'] == 'strategy_initial_capital']


async def test_model_relocation_then_quiet_founding_without_catalog_or_second_request():
    ctl, rt, model, f, records = capital_setup(frozen=True)
    for u in f.units:
        u['movement'] = 0
    await advance(ctl, rt, f, 1)
    assert ctl._capital.mission['site'] == '1,0'
    assert ctl._capital.mission['relocation_observed_turn'] == 1
    assert ctl._growth.summary()['observed_capabilities'] == []
    assert not [c for c in f.calls if isinstance(c, tuple) and c[0] == 'found_city']
    await advance(ctl, rt, f, 2)
    assert len(f.cities) == 1
    assert ctl._capital.completion['completed_turn'] == 2
    assert ctl._capital.completion['completion_basis'].startswith('accepted_founder_consumed')
    assert model.posts_sent == 1
    assert [len(r['execution']) for r in reports(records)] == [0, 1]
    assert reports(records)[1]['execution'][0]['movement_authority'].startswith('untouched')
    assert ctl.directive['tactical_overrides'] == []
    metadata = json.loads(model.requests[0]['messages'][0]['content'].split('\n', 1)[0])
    assert metadata['growth']['initial_capital']['observed_initial_settler_ids'] == ['settler']


@pytest.mark.parametrize('captured', [False, True])
async def test_nonzero_player_binding_and_captured_actor(captured):
    ctl, rt, _, f, records = capital_setup()
    rt.profile = replace(rt.profile, player_id=1)
    for u in f.units:
        u['owner_id'] = 1
    original = f.found_city
    async def found(*args, **kwargs):
        result = await original(*args, **kwargs)
        for c in f.cities:
            c['owner_id'] = 1
        return result
    f.found_city = found
    await advance(ctl, rt, f, 1)
    if captured:
        next(u for u in f.units if u['unit_id'] == 'settler')['owner_id'] = 0
    if captured:
        with pytest.raises(MatchAborted, match='capital_unresolved'):
            await advance(ctl, rt, f, 2)
    else:
        await advance(ctl, rt, f, 2)
    assert ctl._capital.player_id == 1
    assert bool(reports(records)[-1]['execution']) is not captured
    assert bool(ctl._capital.completion) is not captured


async def test_adaptive_capital_continuation_uses_one_count_and_generation(monkeypatch):
    directive = {'tactical_overrides': [{'unit_id': 'settler', 'action': 'move', 'dest': '1,0'}]}
    rt, ctl, requests, _, ledger, kinds = harness(
        monkeypatch, replies=[[use('submit_directive', directive)]])
    rt.profile = replace(rt.profile, player_id=0)
    ctl.growth_autopilot = True
    f = CapitalFacade()
    try:
        await advance(ctl, rt, f, 1)
        await advance(ctl, rt, f, 2)
        assert ctl._capital.completion['completed_turn'] == 2
        assert kinds == ['count_tokens', 'generation'] and ledger == [1, 2]
        assert requests[0][1] == {k: v for k, v in requests[1][1].items() if k != 'max_tokens'}
    finally:
        await rt.client.aclose()


@pytest.mark.parametrize('status', ['accepted', 'rejected'])
async def test_unconfirmed_or_rejected_relocation_never_causes_founding(status):
    ctl, rt, model, f, records = capital_setup()
    f.move_changes = False
    f.move_status = status
    await advance(ctl, rt, f, 1)
    with pytest.raises(MatchAborted, match='capital_resolution_required'):
        await advance(ctl, rt, f, 2)
    assert not any(r['execution'] for r in reports(records))
    assert model.posts_sent == 3
    assert not [c for c in f.calls if isinstance(c, tuple) and c[0] == 'found_city']


async def test_delayed_relocation_observation_enables_next_turn_found_once():
    ctl, rt, _, f, _ = capital_setup()
    f.move_changes = False
    await advance(ctl, rt, f, 1)
    next(u for u in f.units if u['unit_id'] == 'settler')['coord'] = '1,0'
    await advance(ctl, rt, f, 2)
    assert ctl._capital.completion['completed_turn'] == 2


@pytest.mark.parametrize('mutation', ['unknown', 'water', 'foreign_owner', 'owner_absent',
                                      'barbarian', 'nonbarbarian', 'near_city'])
async def test_current_site_guard_pauses_and_one_review_does_not_move_site(mutation):
    ctl, rt, model, f, records = capital_setup()
    await advance(ctl, rt, f, 1)
    if mutation == 'unknown':
        del f.tiles['1,0']
    elif mutation == 'water':
        f.tiles['1,0']['terrain'] = 'COAST'
    elif mutation == 'foreign_owner':
        f.tiles['1,0']['owner_id'] = 1
    elif mutation == 'owner_absent':
        del f.tiles['1,0']['owner_id']
    elif mutation in {'barbarian', 'nonbarbarian'}:
        f.units.append(unit('foreign', owner=1, coord='2,0',
                            is_barbarian=mutation == 'barbarian'))
    else:
        f.cities.append({**city('other_city', coord='3,0'), 'owner_id': 1})
    for turn in range(2, 4):
        await advance(ctl, rt, f, turn)
    with pytest.raises(MatchAborted, match='capital_unresolved'):
        await advance(ctl, rt, f, 4)
    assert not any(r['execution'] for r in reports(records))
    assert ctl._capital.mission['site'] == '1,0'
    assert sum('initial_capital_choice_or_progress_review' in r.get('reasons', [])
               for r in records if r['audit'] == 'strategy_execution') <= 1
    # Contacts may also trigger their existing model review, separately.
    assert model.posts_sent <= 3


async def test_guard_hold_clears_only_with_observed_site_and_does_not_relocate():
    ctl, rt, _, f, _ = capital_setup()
    await advance(ctl, rt, f, 1)
    f.tiles['1,0']['owner_id'] = 1
    await advance(ctl, rt, f, 2)
    assert not f.cities
    f.tiles['1,0']['owner_id'] = -1
    await advance(ctl, rt, f, 3)
    assert f.cities[0]['coord'] == '1,0'


async def test_health_recovery_priority_and_observed_ninety_percent_resume():
    ctl, rt, _, f, records = capital_setup()
    await advance(ctl, rt, f, 1)
    settler = next(u for u in f.units if u['unit_id'] == 'settler')
    settler['hp'] = 35
    await advance(ctl, rt, f, 2)
    settler['hp'] = 80
    await advance(ctl, rt, f, 3)
    assert not any(r['execution'] for r in reports(records))
    settler['hp'] = 90
    await advance(ctl, rt, f, 4)
    assert ctl._capital.completion['completed_turn'] == 4


@pytest.mark.parametrize('frozen', [False, True])
async def test_zero_movement_needs_existing_trusted_frozen_allowance(frozen):
    ctl, rt, _, f, records = capital_setup(frozen=frozen)
    await advance(ctl, rt, f, 1)
    next(u for u in f.units if u['unit_id'] == 'settler')['movement'] = 0
    await advance(ctl, rt, f, 2)
    assert bool(reports(records)[-1]['execution']) is frozen


async def test_unjustified_current_tactical_hold_cannot_silently_defer_capital():
    ctl, rt, _, f, records = capital_setup(frozen=True)
    await advance(ctl, rt, f, 1)
    ctl.directive['tactical_overrides'] = [{'unit_id': 'settler', 'action': 'hold'}]
    with pytest.raises(MatchAborted, match='capital_hold_requires_observed_guard'):
        await advance(ctl, rt, f, 2)
    assert not any(r['execution'] for r in reports(records))


async def test_later_explicit_relocation_retires_and_retargets_capital_plan():
    ctl, rt, _, f, records = capital_setup()
    await advance(ctl, rt, f, 1)
    ctl.directive['tactical_overrides'] = [{'unit_id': 'settler', 'action': 'move', 'dest': '2,0'}]
    await advance(ctl, rt, f, 2)
    await advance(ctl, rt, f, 3)
    assert ctl._capital.mission['site'] == '2,0'
    assert ctl._capital.completion['completed_turn'] == 3
    assert ctl._capital.summary()['retired_intents'][0]['mission']['site'] == '1,0'


@pytest.mark.parametrize('accepted', [False, True])
async def test_rejected_or_ambiguous_found_attempt_never_repeats(accepted):
    ctl, rt, model, f, records = capital_setup()
    f.founding_changes = False
    f.found_status = 'accepted' if accepted else 'rejected'
    await advance(ctl, rt, f, 1)
    await advance(ctl, rt, f, 2)
    with pytest.raises(MatchAborted, match='capital_unresolved'):
        await advance(ctl, rt, f, 3)
    assert sum(len(r['execution']) for r in reports(records)) == 1
    assert ctl._capital.completion is None
    assert model.posts_sent <= 3


async def test_foreign_city_or_unconsumed_founder_never_completes_automatic_plan():
    ctl, rt, _, f, _ = capital_setup()
    f.founding_changes = False
    await advance(ctl, rt, f, 1)
    await advance(ctl, rt, f, 2)
    async def observe(turn):
        ctl._capital.observe({'get_units': await f.get_units(),
                              'get_cities': await f.get_cities()}, turn)
    f.cities.append({**city('foreign', coord='1,0'), 'owner_id': 1})
    await observe(3)
    assert ctl._capital.completion is None
    f.cities[0]['owner_id'] = 0
    await observe(4)
    assert ctl._capital.completion is None
    f.units = [u for u in f.units if u['unit_id'] != 'settler']
    await observe(5)
    assert ctl._capital.completion is None
    assert ctl._capital.mission['status'] != 'founding_observed'


async def test_failed_turn_closure_does_not_commit_observed_capital():
    ctl, rt, _, f, _ = capital_setup()
    await advance(ctl, rt, f, 1)
    f.closures = [{'status': 'rejected', 'rejection': 'denied'}]
    with pytest.raises(MatchAborted):
        await advance(ctl, rt, f, 2)
    assert f.cities and ctl._capital.completion is None
    assert ctl._growth._completed == 1 and ctl._failed


async def test_cancel_after_found_input_preserves_pending_and_poisoned_controller():
    ctl, rt, _, f, _ = capital_setup()
    await advance(ctl, rt, f, 1)
    original = f.found_city
    async def found(*args, **kwargs):
        result = await original(*args, **kwargs)
        async def cancelled():
            raise asyncio.CancelledError()
        f.get_visible_map = cancelled
        return result
    f.found_city = found
    with pytest.raises(asyncio.CancelledError):
        await advance(ctl, rt, f, 2)
    assert ctl._capital.pending['status'] == 'accepted'
    assert ctl._capital.completion is None and ctl._failed


async def test_no_model_founder_order_uses_guarded_procedural_default_immediately():
    ctl, rt, model, f, records = capital_setup(directive={'version': 1})
    await advance(ctl, rt, f, 1)
    assert f.cities[0]['coord'] == '0,0'
    assert ctl._capital.completion['completed_turn'] == 1
    assert ctl._capital.mission['selection_basis'] == 'controller_default_guarded_in_place'
    assert model.posts_sent == 1
    assert reports(records)[0]['execution'][0]['authority'] == 'controller_default_guarded_in_place'
    assert ctl.directive['tactical_overrides'] == []


async def test_absolute_capital_budget_does_not_reset_with_new_intent():
    ctl, rt, _, f, _ = capital_setup()
    await advance(ctl, rt, f, 1)
    ctl._capital._started_turn = -4
    with pytest.raises(MatchAborted, match='progress_budget_exhausted'):
        await advance(ctl, rt, f, 2)


async def test_growth_disabled_preserves_legacy_no_continuation_behavior():
    ctl, rt, _, f, records = capital_setup(enabled=False)
    for turn in range(1, 4):
        await advance(ctl, rt, f, turn)
    assert ctl._capital is None and not f.cities and not reports(records)


async def test_existing_city_disables_initial_capital_and_does_not_reactivate_after_loss():
    ctl, rt, model, f, records = capital_setup(directive={'version': 1})
    f.cities.append(city())
    await advance(ctl, rt, f, 1)
    assert ctl._capital.disabled
    f.cities.clear()
    model.script = [[use('submit_directive', {
        'tactical_overrides': [{'unit_id': 'settler', 'action': 'move', 'dest': '1,0'}]})]]
    await advance(ctl, rt, f, 2)
    assert ctl._capital.mission is None
    assert not reports(records)[-1]['execution']


async def test_stalled_relocation_requires_founder_repair_then_founds_in_place():
    ctl, rt, model, f, records = capital_setup()
    f.move_changes = False
    await advance(ctl, rt, f, 1)
    model.script = model.script[:1] + [
        [use('submit_directive', {'tactical_overrides': [
            {'unit_id': 'a_guard', 'action': 'hold'}]})],
        [use('submit_directive', {'tactical_overrides': [
            {'unit_id': 'settler', 'action': 'found_city'}]})],
    ]
    await advance(ctl, rt, f, 2)
    assert model.posts_sent == 3
    assert f.calls.count(('move_unit', 'settler', '1,0')) == 1
    assert f.calls.count(('found_city', 'settler')) == 1
    completion = ctl._capital.completion
    assert completion['site'] == '0,0' and completion['observed_city_id'] == 'new_city'
    assert completion['observed_player_id'] == 0 and completion['founder_consumed'] is True
    assert completion['selection_basis'] == 'explicit_model_founding'
    old = ctl._capital.summary()['retired_intents'][0]
    assert old['pending']['status'] == 'accepted' and old['mission']['site'] == '1,0'
    assert reports(records)[-1]['mission']['observed_city_id'] == 'new_city'


async def test_same_failed_edge_rejected_before_dispatch_then_distinct_retarget():
    ctl, rt, model, f, _ = capital_setup()
    f.move_changes = False
    f.tiles['0,1'] = {'terrain': 'GRASSLAND', 'owner_id': -1, 'city_id': ''}
    await advance(ctl, rt, f, 1)
    f.move_changes = True
    model.script = model.script[:1] + [[use('submit_directive', {'tactical_overrides': [
        {'unit_id': 'settler', 'action': 'move', 'dest': dest}]})]
        for dest in ['1,0', '0,1']]
    await advance(ctl, rt, f, 2)
    await advance(ctl, rt, f, 3)
    assert model.posts_sent == 3
    assert f.calls.count(('move_unit', 'settler', '1,0')) == 1
    assert f.calls.count(('move_unit', 'settler', '0,1')) == 1
    assert ctl._capital.completion['site'] == '0,1'
    assert ctl._capital.completion['selection_basis'] == \
        'explicit_model_relocation_then_guarded_founding'


async def test_explicit_first_turn_founding_uses_same_causal_completion_fields():
    ctl, rt, model, f, records = capital_setup(directive={'tactical_overrides': [
        {'unit_id': 'settler', 'action': 'found_city'}]})
    await advance(ctl, rt, f, 1)
    assert model.posts_sent == 1 and f.calls.count(('found_city', 'settler')) == 1
    assert ctl._capital.completion['observed_city_id'] == 'new_city'
    assert ctl._capital.completion['selection_basis'] == 'explicit_model_founding'
    assert reports(records)[0]['execution'] == []  # explicit input belongs to scouting audit
    assert reports(records)[0]['mission']['founder_consumed'] is True
    closed = next(r for r in records if r['audit'] == 'strategy_turn_closed')
    assert closed['initial_capital_completion'] == ctl._capital.completion


@pytest.mark.parametrize('guard', ['damaged', 'unknown_health', 'foreign_contact'])
async def test_default_never_injects_override_to_bypass_guard_and_stops(guard):
    ctl, rt, _, f, records = capital_setup(directive={'version': 1})
    actor = next(u for u in f.units if u['unit_id'] == 'settler')
    if guard == 'damaged':
        actor['hp'] = 40
    elif guard == 'unknown_health':
        actor.update(hp=None, max_hp=None, health_valid=False)
    else:
        f.units.append(unit('foreign', owner=1, coord='0,1', is_barbarian=False))
    await advance(ctl, rt, f, 1)
    await advance(ctl, rt, f, 2)
    with pytest.raises(MatchAborted, match='guard_hold_budget_exhausted'):
        await advance(ctl, rt, f, 3)
    assert not f.cities and not any(r['execution'] for r in reports(records))
    assert ctl.directive['tactical_overrides'] == []


async def test_total_relocation_cap_survives_explicit_revisions():
    ctl, rt, _, f, _ = capital_setup()
    await advance(ctl, rt, f, 1)
    ctl.directive['tactical_overrides'] = [{'unit_id': 'settler', 'action': 'move', 'dest': '2,0'}]
    await advance(ctl, rt, f, 2)
    ctl.directive['tactical_overrides'] = [{'unit_id': 'settler', 'action': 'move', 'dest': '3,0'}]
    with pytest.raises(MatchAborted, match='capital_relocation_unavailable'):
        await advance(ctl, rt, f, 3)
    assert ('move_unit', 'settler', '3,0') not in f.calls
    assert ctl._capital.summary()['relocations_used'] == 2


async def test_actor_loss_on_post_scout_refresh_stops_without_founding_input():
    ctl, rt, model, f, _ = capital_setup(directive={'version': 1})
    original = f.get_units
    reads = 0
    async def units():
        nonlocal reads
        reads += 1
        result = await original()
        return [u for u in result if u['unit_id'] != 'settler'] if reads >= 2 else result
    f.get_units = units
    with pytest.raises(MatchAborted, match='capital_unresolved'):
        await advance(ctl, rt, f, 1)
    assert model.posts_sent == 1 and not f.cities
    assert not [c for c in f.calls if isinstance(c, tuple) and c[0] == 'found_city']
