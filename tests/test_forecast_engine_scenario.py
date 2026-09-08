"""Independent-engine scenario: monitor bookkeeping against the real sim engine.

The sim catalog publishes cost but no turns estimate, so the honest forecast on
this adapter is completion-unsupported — the scenario pins that honesty and
validates the observation/resolution machinery end to end: the monitor's
completion classification must agree with the engine's actual unit spawn, and
a swapped queue head must invalidate the option.
"""
from civ_arena.agents.build_option import DevelopmentOptionMonitor
from civ_arena.agents.economic_forecast import build_forecast
from civ_arena.game.sim.rules import available_production
from civ_arena.game.sim.simulator import SimulatorAdapter
from civ_arena.game.sim.state import UNIT_TYPES
from conftest import cmd, own_units


def cities_now(state, player_id):
    # Production rows in the projected runtime carry "q,r" coords; the raw sim
    # state keeps integer q/r. Synthesize the projected shape for the monitor.
    return [{**city, 'coord': f"{city['q']},{city['r']}"}
            for city in state.cities.values() if city['owner'] == player_id]


async def founded_city(adapter, player_id=0):
    await adapter.begin_phase(player_id, 1)
    state = adapter.state
    settler = own_units(state, player_id, 'SETTLER')[0]
    await adapter.act(cmd('found_city', {'unit_id': settler['unit_id']}, player_id))
    city_id = sorted(city['city_id'] for city in state.cities.values()
                     if city['owner'] == player_id)[0]
    await adapter.act(cmd('set_city_production', {'city_id': city_id,
                                                  'item_id': 'WARRIOR'}, player_id))
    await adapter.end_phase(player_id, 1)
    # The sim advances the round only after the last seat closes the turn.
    await adapter.begin_phase(1, 1)
    await adapter.end_phase(1, 1)
    return city_id


async def drive_round(adapter, monitor, turn):
    """One hotseat round: observe before this turn's effects, then ambient."""
    await adapter.begin_phase(0, turn)
    events = monitor.observe(turn=turn, cities=cities_now(adapter.state, 0), units=[])
    await adapter.end_phase(0, turn)
    await adapter.begin_phase(1, turn)
    await adapter.end_phase(1, turn)
    return events


async def test_monitor_completion_agrees_with_real_engine_production():
    adapter = SimulatorAdapter()
    await adapter.setup({'seed': 5})
    city_id = await founded_city(adapter)
    state = adapter.state
    catalog = available_production(state, 0, city_id)
    warrior = next(row for row in catalog if row['item_id'] == 'WARRIOR')
    assert warrior['cost'] == UNIT_TYPES['WARRIOR']['cost']

    monitor = DevelopmentOptionMonitor(0)
    forecast = build_forecast(turn=2, city_id=city_id, item_id='WARRIOR', row=warrior)
    # The sim adapter publishes no engine turns estimate: unsupported, not guessed.
    assert forecast['observed']['engine_turns_estimate'] is None
    assert 'engine_turns_estimate' in forecast['unsupported']
    monitor.register(forecast)

    resolved = None
    for turn in range(2, 40):
        events = await drive_round(adapter, monitor, turn)
        if events:
            resolved = events[0]
            break
    assert resolved is not None, 'engine never completed the queued WARRIOR'
    assert resolved['event'] == 'completed'
    assert resolved['verdict'] == 'completion_unsupported'
    assert resolved['item_id'] == 'WARRIOR'
    # The engine really produced the unit in that turn's ambient phase.
    assert own_units(adapter.state, 0, 'WARRIOR')


async def test_engine_queue_head_swap_invalidates_the_option():
    adapter = SimulatorAdapter()
    await adapter.setup({'seed': 5})
    city_id = await founded_city(adapter)
    state = adapter.state
    catalog = available_production(state, 0, city_id)
    warrior = next(row for row in catalog if row['item_id'] == 'WARRIOR')
    monitor = DevelopmentOptionMonitor(0)
    monitor.register(build_forecast(turn=2, city_id=city_id, item_id='WARRIOR',
                                    row=warrior))
    await adapter.begin_phase(0, 2)
    # The engine advances to a different head before observed completion.
    state.city(city_id)['production_queue'] = ['SCOUT']
    events = monitor.observe(turn=2, cities=cities_now(state, 0), units=[])
    assert [event['event'] for event in events] == ['invalidated_queue_changed']
    assert events[0]['item_id'] == 'WARRIOR'
    await adapter.end_phase(0, 2)
    await adapter.begin_phase(1, 2)
    await adapter.end_phase(1, 2)
