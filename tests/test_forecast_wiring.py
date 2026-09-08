"""S3 wiring: knob-gated advisory + audit ledger; executor unchanged when on."""
import copy
from unittest.mock import AsyncMock

import pytest

from civ_arena.agents.llm.runtime import LLMAgentRuntime
from civ_arena.agents.llm.strategic_controller import StrategicController
from civ_arena.agents.runtime import AgentProfile, build_runtime
from civ_arena.config import ConfigError, LLMSpec, parse_config
from fakes import FakeModel, use

CATALOG = (('MONUMENT', 'building', 25, 3), ('WARRIOR', 'unit', 30, 2))


class Facade:
    """Engine-like catalog: turns COUNT DOWN from the turn production was set
    (GetTurnsLeft semantics). `stall` pins MONUMENT's estimate so the implied
    completion date slips — the rate-assumption invalidation case."""

    def __init__(self):
        self.units = [{'unit_id': 'u0:1', 'owner_id': 0, 'type': 'SCOUT',
                       'coord': '0,0', 'movement': 2, 'hp': 100,
                       'max_hp': 100, 'health_valid': True}]
        self.cities = [{'city_id': 'c0:1', 'owner': 0, 'coord': '0,0',
                        'production_queue': [], 'population': 3}]
        self.you = {'researching': 'MINING', 'gold': 0}
        self.calls = []
        self.turn = 1
        self.production_set_turn = 1
        self.stall = False

    async def get_visible_map(self):
        self.calls.append('map')
        return {'tiles': {'0,0': {'terrain': 'PLAINS'}, '1,0': {'terrain': 'PLAINS'}}}

    async def get_units(self):
        self.calls.append('units')
        return copy.deepcopy(self.units)

    async def get_cities(self):
        return copy.deepcopy(self.cities)

    async def get_overview(self):
        return {'you': copy.deepcopy(self.you), 'public': {}}

    async def get_available_research(self):
        return [{'tech_id': 'POTTERY'}]

    async def get_available_production(self, city_id):
        city = next((c for c in self.cities if c['city_id'] == city_id),
                    {'owner': None})
        if city.get('owner') != 0:
            # native accessor shape: unowned or unknown city ids error out
            # (lua: "production observation city unavailable")
            return {'status': 'rejected',
                    'rejection': 'production observation city unavailable'}
        elapsed = self.turn - self.production_set_turn
        return [{'item_id': item, 'kind': kind, 'cost': cost,
                 'turns': base if self.stall and item == 'MONUMENT'
                 else base - elapsed}
                for item, kind, cost, base in CATALOG]

    async def set_research(self, tech_id):
        self.calls.append(('set_research', tech_id))
        self.you['researching'] = tech_id
        return {'status': 'accepted'}

    async def set_city_production(self, city_id, item_id):
        self.calls.append(('set_city_production', city_id, item_id))
        city = next(c for c in self.cities if c['city_id'] == city_id)
        city['production_queue'] = item_id
        self.production_set_turn = self.turn
        return {'status': 'accepted'}

    async def fortify(self, unit_id, **kwargs):
        self.calls.append(('fortify', unit_id))
        return {'status': 'accepted'}

    async def move_unit(self, unit_id, dest, **kwargs):
        self.calls.append(('move_unit', unit_id, dest))
        return {'status': 'accepted'}

    async def end_turn(self):
        self.calls.append('end_turn')
        return {'status': 'accepted'}


@pytest.fixture
def forecast_setup(monkeypatch):
    scout = AsyncMock(return_value={'nodes': [], 'seed': 'fixture'})
    monkeypatch.setattr('civ_arena.agents.llm.strategic_controller.run_scouting', scout)
    model = FakeModel([[use('submit_directive', {'version': 1})]])
    llm = LLMSpec('http://unused', 'UNUSED', 'fake')
    runtime = LLMAgentRuntime.build(AgentProfile('a', 0, 'llm', 4, llm=llm), client=model)
    records = []
    controller = StrategicController('fresh-match', cadence=5, audit=records.append,
                                     economic_forecast=True)
    return controller, runtime, model, Facade(), records


@pytest.fixture
def baseline_setup(monkeypatch):
    scout = AsyncMock(return_value={'nodes': [], 'seed': 'fixture'})
    monkeypatch.setattr('civ_arena.agents.llm.strategic_controller.run_scouting', scout)
    model = FakeModel([[use('submit_directive', {'version': 1})]])
    llm = LLMSpec('http://unused', 'UNUSED', 'fake')
    runtime = LLMAgentRuntime.build(AgentProfile('a', 0, 'llm', 4, llm=llm), client=model)
    records = []
    controller = StrategicController('fresh-match', cadence=5, audit=records.append)
    return controller, runtime, model, Facade(), records


async def advance(controller, runtime, facade, turn, **kwargs):
    facade.turn = turn
    runtime.begin_turn(turn)
    await controller.take_turn(runtime, facade, **kwargs)


def of_kind(records, kind):
    return [record for record in records if record['audit'] == kind]


async def test_forecast_audit_is_pre_action_and_advisory_reaches_the_model(forecast_setup):
    controller, runtime, model, facade, records = forecast_setup
    await advance(controller, runtime, facade, 1)
    forecasts = of_kind(records, 'strategy_forecast')
    assert len(forecasts) == 1
    assert forecasts[0]['forecast']['issued_turn'] == 1
    assert forecasts[0]['forecast']['observed']['engine_turns_estimate'] == 3
    assert forecasts[0]['forecast']['observed']['catalog_cost'] == 25
    executed = [index for index, record in enumerate(records)
                if record['audit'] == 'strategy_economy'
                and record.get('tool') == 'set_city_production']
    assert executed and records.index(forecasts[0]) < executed[0]
    assert ('set_city_production', 'c0:1', 'MONUMENT') in facade.calls
    context = model.requests[0]['messages'][0]['content']
    assert '"economic_forecast"' in context
    assert '"advisory_only_executor_unchanged"' in context
    assert '"city_plans"' in context


async def test_completion_outcome_recorded_without_touching_the_forecast(forecast_setup):
    controller, runtime, model, facade, records = forecast_setup
    await advance(controller, runtime, facade, 1)
    issued = of_kind(records, 'strategy_forecast')[0]['forecast']
    for turn in (2, 3):
        await advance(controller, runtime, facade, turn)
    assert of_kind(records, 'strategy_forecast_outcome') == []
    facade.cities[0]['production_queue'] = []
    await advance(controller, runtime, facade, 4)
    outcomes = of_kind(records, 'strategy_forecast_outcome')
    completed = [row for row in outcomes if row['event'] == 'completed']
    assert completed and completed[0]['verdict'] == 'in_estimated_window'
    assert completed[0]['estimated_completion_turn'] == 4
    assert of_kind(records, 'strategy_forecast')[0]['forecast'] == issued


async def test_threat_censors_before_next_production_and_survives_completion(
        forecast_setup):
    controller, runtime, model, facade, records = forecast_setup
    await advance(controller, runtime, facade, 1)
    facade.units.append({'unit_id': 'u9:1', 'owner_id': 1, 'type': 'WARRIOR',
                         'coord': '2,0', 'is_barbarian': True})
    await advance(controller, runtime, facade, 2)
    censor = [row for row in of_kind(records, 'strategy_forecast_outcome')
              if row['event'] == 'threat_censored']
    assert censor and censor[0]['turn'] == 2
    assert facade.calls.count(('set_city_production', 'c0:1', 'MONUMENT')) == 1
    facade.cities[0]['production_queue'] = []
    await advance(controller, runtime, facade, 3)
    completed = [row for row in of_kind(records, 'strategy_forecast_outcome')
                 if row['event'] == 'completed']
    assert completed and completed[0]['censored'] == 'threat_interrupt'
    assert completed[0]['censored_turn'] == 2


async def test_current_turn_resolution_reaches_that_turns_decision(forecast_setup):
    """R1 regression: the model deciding on turn N sees turn-N outcomes."""
    controller, runtime, model, facade, records = forecast_setup
    await advance(controller, runtime, facade, 1)
    for turn in (2, 3):
        await advance(controller, runtime, facade, turn)
    facade.cities[0]['production_queue'] = []
    await advance(controller, runtime, facade, 4, tactical_requested=True)
    completed = [row for row in of_kind(records, 'strategy_forecast_outcome')
                 if row['event'] == 'completed']
    assert completed and completed[0]['observed_turn'] == 4
    assert completed[0]['estimated_completion_turn'] == 4
    # The decision request for turn 4 already carries the resolved outcome.
    context = model.requests[-1]['messages'][0]['content']
    assert '"completed"' in context
    assert '"recent_outcomes"' in context


async def test_knob_off_default_is_inert(baseline_setup):
    controller, runtime, model, facade, records = baseline_setup
    for turn in range(1, 5):
        await advance(controller, runtime, facade, turn)
    assert not [record for record in records
                if record['audit'].startswith('strategy_forecast')]
    assert facade.calls.count(('set_city_production', 'c0:1', 'MONUMENT')) == 1
    assert all('"economic_forecast"' not in request['messages'][0]['content']
               for request in model.requests)


async def test_knob_on_leaves_executor_calls_identical(forecast_setup, baseline_setup):
    on_controller, on_runtime, _, on_facade, _ = forecast_setup
    off_controller, off_runtime, _, off_facade, _ = baseline_setup
    for turn in range(1, 4):
        await advance(on_controller, on_runtime, on_facade, turn)
        await advance(off_controller, off_runtime, off_facade, turn)
    assert on_facade.calls == off_facade.calls


async def test_rate_slippage_censors_through_controller(forecast_setup):
    """R2 regression: healthy countdown never censors; a slipping estimate does."""
    controller, runtime, model, facade, records = forecast_setup
    await advance(controller, runtime, facade, 1)  # MONUMENT, completion turn 4
    await advance(controller, runtime, facade, 2)  # countdown 2 -> implied 4: clean
    assert not [row for row in of_kind(records, 'strategy_forecast_outcome')
                if row['event'] == 'rate_estimate_changed']
    facade.stall = True
    await advance(controller, runtime, facade, 3)  # estimate pinned at 3 -> implied 6
    slipped = [row for row in of_kind(records, 'strategy_forecast_outcome')
               if row['event'] == 'rate_estimate_changed']
    assert slipped and slipped[0]['turn'] == 3
    assert slipped[0]['recorded_completion_turn'] == 4
    assert slipped[0]['implied_completion_turn'] == 6


async def test_lost_pending_city_resolves_through_the_monitor(forecast_setup):
    """R3 regression: the catalog pre-read must skip a captured city — the
    native accessor rejects unowned ids — so the loss resolves through
    observe()'s invalidated_city_unobserved path instead of aborting."""
    controller, runtime, model, facade, records = forecast_setup
    facade.cities.append({'city_id': 'c1:2', 'owner': 0, 'coord': '1,0',
                          'production_queue': [], 'population': 2})
    await advance(controller, runtime, facade, 1)  # forecasts in both cities
    assert 'c1:2' in [row['city_id'] for row in of_kind(records, 'strategy_forecast')]
    facade.cities[1]['owner'] = 1  # captured with its build still pending
    await advance(controller, runtime, facade, 2)  # must not raise MatchAborted
    lost = [row for row in of_kind(records, 'strategy_forecast_outcome')
            if row['event'] == 'invalidated_city_unobserved']
    assert lost and lost[0]['city_id'] == 'c1:2'


async def test_comparison_defense_totals_come_from_the_policy_result(forecast_setup):
    """R3 regression: the comparison's defense numbers are the policy result's
    own totals, transmitted verbatim — never recomputed over candidates."""
    controller, runtime, model, facade, records = forecast_setup
    facade.units.append({'unit_id': 'u9:1', 'owner_id': 1, 'type': 'WARRIOR',
                         'coord': '2,0', 'is_barbarian': True})
    await advance(controller, runtime, facade, 1)
    forecast = of_kind(records, 'strategy_forecast')[0]
    economy = next(row for row in records
                   if row['audit'] == 'strategy_economy'
                   and row.get('tool') == 'set_city_production')
    contingency = forecast['comparison']['threat_contingency']
    assert contingency['defenders_owned_queued_reserved'] == \
        economy['production_policy']['defenders_owned_queued_reserved']
    assert contingency['defense_goal'] == \
        economy['production_policy']['defense_goal']
    assert contingency['shortfall_basis'] == 'policy_result'


def _config_doc(*, knob, strategic=True, adaptive=True):
    llm = {'base_url': 'http://unused', 'api_key_env': 'K', 'model_id': 'm'}
    if knob is not None:
        llm['economic_forecast'] = knob
    if adaptive:
        llm['adaptive_context'] = {'provider_context_tokens': 100000}
    agent = {'agent_id': 'a', 'player_id': 0, 'policy': 'llm', 'llm': llm}
    if strategic:
        agent['decision_mode'] = 'strategic_autopilot'
    return {'match': {'match_id': 'm1', 'seed': 1}, 'agents': [agent]}


def test_config_gate_for_the_forecast_knob():
    parsed = parse_config(_config_doc(knob=True))
    assert parsed.agents[0].llm.economic_forecast is True
    assert parse_config(_config_doc(knob=False)).agents[0].llm.economic_forecast is False
    with pytest.raises(ConfigError, match='economic_forecast requires strategic_autopilot'):
        parse_config(_config_doc(knob=True, strategic=False))
    with pytest.raises(ConfigError, match='economic_forecast requires boolean'):
        parse_config(_config_doc(knob=True, adaptive=False))
    with pytest.raises(ConfigError, match='economic_forecast requires boolean'):
        parse_config(_config_doc(knob='yes'))
    assert LLMSpec('http://unused', 'K', 'm').economic_forecast is False


def test_build_runtime_gate_for_the_forecast_knob():
    adaptive = LLMSpec('http://unused', 'K', 'm',
                       adaptive_context=parse_config(_config_doc(knob=True))
                       .agents[0].llm.adaptive_context,
                       economic_forecast=True)
    plain = LLMSpec('http://unused', 'K', 'm', economic_forecast=True)
    with pytest.raises(ValueError, match='economic_forecast requires strategic'):
        build_runtime(AgentProfile('a', 0, 'llm', 4, llm=adaptive, decision_mode='legacy'))
    with pytest.raises(ValueError, match='economic_forecast requires strategic'):
        build_runtime(AgentProfile('a', 0, 'llm', 4, llm=plain,
                                   decision_mode='strategic_autopilot'),
                      match_id='m')
