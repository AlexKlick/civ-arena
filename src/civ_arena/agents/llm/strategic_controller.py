"""Opt-in, fresh-match strategic decisions above bounded deterministic execution.

This version deliberately refuses resume: coordinator checkpoints do not yet
persist this controller's directive and trigger history. Audit records describe
reproduction inputs but are not a checkpoint restore implementation.
"""
from __future__ import annotations

import asyncio
import copy
import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from civ_arena.agents.growth_policy import GrowthPolicy
from civ_arena.agents.initial_capital import InitialCapitalPlan
from civ_arena.agents.llm.client import ModelUnavailable
from civ_arena.agents.llm.context_curator import ContextCurator
from civ_arena.agents.llm.decision_packet import decision_packet, decision_snapshot
from civ_arena.agents.llm.request_budget import (
    GenerationAdmission,
    TokenCount,
    encoded,
    input_payload,
    measurements,
    payload_hash,
    task_for,
)
from civ_arena.agents.llm.tool_schemas import TOOL_SCHEMAS
from civ_arena.agents.production_policy import choose_production
from civ_arena.agents.recovery import RecoveryPolicy, RecoveryTracker
from civ_arena.agents.scouting import ScoutingFeedback, run_scouting
from civ_arena.agents.settlement_executor import SettlementExecutor
from civ_arena.agents.strategy_directive import DIRECTIVE_SCHEMA, validate_directive
from civ_arena.arena.referee import MatchAborted

SYSTEM = """You are the strategic commander of one Civilization seat. Submit exactly
one submit_directive tool call containing one JSON directive. Do not request basic
state, narrate analysis, or call game tools. Current projected state is supplied.
The decision_packet lists outstanding choices and changes since the prior strategy
request, including quiet turns. option_sources distinguishes observed empty lists
from catalogs not requested because a choice is already active. Do not infer that
those unqueried catalogs are empty or that a disappeared contact was destroyed.
Read movement_authority in the supplied metadata. When opening_units_frozen is
true, the live mod temporarily zeroed the listed untouched owned units' movement.
The controller may attempt one initial owned-unit action under the unit's natural
allowance; its amount is unobserved and engine legality still applies. Opening
zero alone does not rule out founding a city with an owned settler. This is not
extra movement and does not authorize refilling an already attempted unit. When
opening_units_frozen is false, ordinary zero remains observed spent movement;
never assume a positive allowance or infer that a requested action is legal.
The controller executes routine scouting, research preferences and production
preferences without further model requests. Tactical overrides last this turn
only; use them for specific battles or founding a city with an observed owned
settler. Persisted preferences govern later quiet turns until the next decision.
Production preferences are recurring rankings for empty queues, not a one-time
build order. Owned units plus all owned cities' queued units count toward desired
inventory. Default targets are two SCOUTs empire-wide, one BUILDER per owned city,
and one of each other unit type. Optional unit_targets overrides use exact available
unit item IDs and integer counts 0 through 32 (zero disables new production).
Available buildings provide fallback when a preferred unit has reached its target.
Use SUMERIAN_WAR_CART only if that exact item is offered; WAR_CART is not an alias.
Confirmed nearby is_barbarian=true contacts can prioritize available defenders
until the empire has two defenders per city; explicit unit_targets still apply.
scouting.unit_types assigns persistent roles; include SCOUT to send built scouts
exploring. Explicit omission holds those scouts and suppresses new scout production;
tactical orders still apply.
The recovery overlay pauses normal missions below the configured observed HP ratio
until the resume threshold is observed. Unknown health cannot prove recovery.
Explicit tactical orders can interrupt recovery for one turn (for example retreat
from ongoing damage), without cancelling recovery or replacing the unit's mission.
Recovery holds are ordinary standing orders; a submitted order is not proof of healing.
Review recovery_under_damage or recovery_no_observed_gain using observed terrain and
contacts. No observed enemy does not prove safety, and unknown health is not full HP.
If production_targets_satisfied requests a late economic adjustment, scouting has
already executed. Adjust available production targets or preferences; tactical
overrides from that late request will be discarded.
Only observed IDs and coordinates may be used. Visible contacts are not proof of
war. Probabilities in the controller graph are seeded action-selection weights,
not calibrated success predictions. No prose or hidden reasoning is required."""


GROWTH_SYSTEM = """
Growth autopilot is enabled. Its aggregate military cap and observed capability
policy govern production; exact unit_targets remain upper bounds, not commands to
exceed the aggregate cap. Two scouts remains the default scout cap. Nearby guards,
one spare escort and a persistent settler mission are reserved from routine scouting.
A confirmed local barbarian triggers defensive priority; nonbarbarian contacts have
unknown war/intent and constrain settlement routes without implying hostility.
Three clear observed own turns are the configurable heuristic before returning to
growth. Missing information never proves safety, optimal sites or victory odds.
The growth metadata names why expansion is waiting, including no healthy spare
escort or no observed route. The controller attempts at most one automatic mission
action per turn, preserving explicit tactical commands and health holds. A pending
unconfirmed action will not automatically repeat. You may issue explicit tactical
orders for recovery/repositioning; they do not replace the persistent mission/site.
Capabilities are retained exact native observations, not current availability.
Before the first owned city, found immediately with an explicit SETTLER tactical
order or move that settler one known adjacent tile to select a fixed capital site.
That first relocation selects a persistent initial-capital intent: after observed
arrival, the controller will attempt founding there on a later quiet turn, subject
to current ownership, health, terrain, city spacing and nearby contact guards.
It never relocates that site automatically. No production catalog or spare escort
is needed for this initial prerequisite. Other tactical orders still last one turn.
Rejected or unconfirmed founding is not retried automatically. If no site was
selected, one additional choice review is available; no optimal site is invented.
An unsent expansion intent may select another currently observed feasible site,
at most three times; committed travel intents and attempted missions preserve their sites.
One preparatory founder may train below the city cap in growth mode with a verified
healthy city guard and nearby spare escort, even while surveying the destination.
Owned, queued and accepted reserved founders count toward this single reserve.
Training does not authorize travel through unknown ownership or unsafe founding.
Accepted founder training stays committed until confirmed settlement closure. If
its queue vanishes without an observed owned founder, one review reports the missing
outcome; no automatic replacement trains. An observed founder is not proof of origin.
Preparatory training retires any infeasible unsent site; surveying must establish
a currently feasible site before a new travel mission can start.
Before founder commitment, a spare escort may survey rather than wait indefinitely.
Production options currently enumerate only units/buildings. Unlisted districts
and city projects have unknown native availability; do not infer none exist.
"""


def _encode(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)


@dataclass
class StrategicController:
    match_id: str
    cadence: int = 5
    audit: Callable[[dict], None] | None = None
    opening_units_frozen: bool = False
    growth_autopilot: bool = False
    _growth: GrowthPolicy | None = field(default=None, init=False)
    _settlement: SettlementExecutor | None = field(default=None, init=False)
    _capital: InitialCapitalPlan | None = field(default=None, init=False)
    recovery_policy: RecoveryPolicy = field(default_factory=RecoveryPolicy)
    _recovery: RecoveryTracker = field(init=False)
    _recovery_proposal: dict | None = field(default=None, init=False)
    directive: dict | None = field(default=None, init=False)
    _last_turn: int = field(default=0, init=False)
    _last_decision: int = field(default=0, init=False)
    _previous: dict | None = field(default=None, init=False)
    _decision_observation: dict | None = field(default=None, init=False)
    _failed: bool = field(default=False, init=False)
    _turn_strategy_requests: int = field(default=0, init=False)
    _scouting_feedback: ScoutingFeedback = field(default_factory=ScoutingFeedback, init=False)

    def __post_init__(self) -> None:
        self._recovery = RecoveryTracker(self.recovery_policy)
        if not isinstance(self.match_id, str) or not self.match_id:
            raise ValueError('strategic controller requires match_id')
        if type(self.growth_autopilot) is not bool:
            raise ValueError('growth_autopilot must be an explicit boolean')
        if type(self.opening_units_frozen) is not bool:
            raise ValueError('opening_units_frozen must be an explicit boolean')
        if type(self.cadence) is not int or not 1 <= self.cadence <= 60:
            raise ValueError('strategic cadence must be between 1 and 60')

    def _emit(self, runtime: Any, kind: str, **payload: Any) -> None:
        if self.audit is not None:
            self.audit({'audit': kind, 'controller_version': 1,
                        'match_id': self.match_id, 'agent_id': runtime.profile.agent_id,
                        'player_id': runtime.profile.player_id, 'turn': runtime._turn,
                        **copy.deepcopy(payload)})

    @staticmethod
    def _facts(curator: ContextCurator) -> dict:
        units = curator.own('get_units')
        return {
            'units': sorted(u['unit_id'] for u in units),
            'cities': sorted(c['city_id'] for c in curator.own('get_cities')),
            'contacts': sorted(u['unit_id'] for u in curator.state['get_units']
                               if u not in units),
            'researching': curator.state['get_overview'].get('you', {}).get('researching'),
            'hp': {u['unit_id']: u['hp'] for u in units if type(u.get('hp')) in (int, float)},
            'settlers': sorted(u['unit_id'] for u in units if u.get('type') == 'SETTLER'),
        }

    def _reasons(self, facts: dict, turn: int, tactical_requested: bool) -> list[str]:
        if self.directive is None:
            return ['initial_strategy']
        reasons = []
        if turn - self._last_decision >= self.cadence:
            reasons.append('strategy_cadence')
        previous = self._previous or {}
        if facts['cities'] != previous.get('cities'):
            reasons.append('owned_city_roster_changed')
        if set(facts['contacts']) - set(previous.get('contacts', [])):
            reasons.append('new_visible_contact')
        if set(facts['settlers']) - set(previous.get('settlers', [])):
            reasons.append('new_owned_settler')
        if previous.get('researching') and previous['researching'] != facts['researching']:
            reasons.append('research_changed' if facts['researching']
                           else 'research_choice_required')
        if set(previous.get('units', [])) - set(facts['units']):
            reasons.append('owned_unit_lost')
        if any(hp < previous.get('hp', {}).get(uid, hp) for uid, hp in facts['hp'].items()):
            reasons.append('owned_unit_damaged')
        if tactical_requested:
            reasons.append('tactical_control_requested')
        return reasons

    async def take_turn(self, runtime: Any, facade: Any, *,
                        tactical_requested: bool = False) -> None:
        """Keep one instance per runtime; close only through its existing closure gate."""
        turn = runtime._turn
        if self._failed:
            raise MatchAborted('strategic controller previously failed; restart is unsupported')
        if type(turn) is not int or turn != self._last_turn + 1:
            raise MatchAborted('strategic controller v1 is fresh-only; '
                               'resume/skipped turns unsupported')
        if type(tactical_requested) is not bool:
            raise ValueError('tactical_requested must be boolean')
        # Poison until the entire turn, including closure and audit, succeeds.
        self._failed = True
        self._turn_strategy_requests = 0
        try:
            curator = ContextCurator(facade, runtime.profile.player_id,
                                     runtime.llm.max_result_chars)
            await curator.refresh(include_options=False)
            frozen_ids = ({u['unit_id'] for u in curator.own('get_units')}
                          if self.opening_units_frozen else set())
            feedback = self._scouting_feedback.begin_turn(
                curator.state, player_id=runtime.profile.player_id, turn=turn,
                completed_turn=self._last_turn)
            recovery = self._recovery.begin_turn(
                curator.state['get_units'], player_id=runtime.profile.player_id,
                turn=turn, completed_turn=self._last_turn)
            self._recovery_proposal = recovery
            if self.growth_autopilot:
                await curator.refresh()
                if self._growth is None:
                    self._growth = GrowthPolicy(runtime.profile.player_id, mission_execution=True)
                    self._settlement = SettlementExecutor(runtime.profile.player_id)
                if self._growth.player_id != runtime.profile.player_id:
                    raise MatchAborted('growth controller player identity changed')
                if self._capital is None:
                    self._capital = InitialCapitalPlan(runtime.profile.player_id)
                self._growth.begin_turn(curator.state, turn=turn, catalogs=curator.production)
                self._settlement.observe(self._growth, curator.state, turn)
                self._capital.observe(curator.state, turn)
            facts = self._facts(curator)
            reasons = self._reasons(facts, turn, tactical_requested)
            reasons.extend(recovery['review_reasons'])
            if self._settlement is not None:
                reasons.extend(self._settlement.review_reasons(turn, self._growth.mission))
                reasons.extend(self._growth.training_review_reasons(turn))
            if self._capital is not None:
                reasons.extend(self._capital.review_reasons(turn))
            if reasons:
                await curator.refresh()
                directive = await self._decide(runtime, curator, reasons)
                self.directive = copy.deepcopy(directive)
                self._last_decision = turn
            else:
                directive = copy.deepcopy(self.directive)
            self._emit(runtime, 'strategy_execution', source='model' if reasons else 'autopilot',
                       reasons=reasons, last_decision_turn=self._last_decision,
                       seed=runtime.profile.seed, directive=directive,
                       persistence='fresh_only_v1', cadence=self.cadence,
                       opening_frozen_unit_ids=sorted(frozen_ids), recovery=recovery)

            async def refresh() -> dict:
                # Scouting consumes map/entities only; refresh economy once afterwards.
                await curator.refresh(include_options=False, include_overview=False)
                return curator.state

            reserved = self._growth.reserved_roles(curator.state) if self._growth else {}
            if self._capital is not None:
                self._capital.prepare(curator.state, directive, turn)
                reserved.update(self._capital.reserved_roles(curator.state))
            graph = await run_scouting(
                curator.state, directive=directive, player_id=runtime.profile.player_id,
                match_id=self.match_id, agent_id=runtime.profile.agent_id, turn=turn,
                execute=curator.execute, refresh=refresh, seed=runtime.profile.seed,
                frozen_unit_ids=frozen_ids, nonprogress=feedback["suppressed"],
                recovery=recovery['units'], recovery_policy=self.recovery_policy,
                reserved_roles=reserved)
            # A post-action refresh can first reveal unknown/damaged health in
            # another unit. Commit that newly held state only after turn closure.
            for uid, row in graph.get('recovery_overlay', {}).items():
                recovery['units'].setdefault(uid, copy.deepcopy(row))
            graph["nonprogress_feedback"] = feedback
            graph["recovery"] = recovery
            # Retain explicit role exclusions, but make otherwise idle scouts
            # visible in the graph details consumed by the existing dashboard.
            excluded = ({u['unit_id'] for u in curator.own('get_units')
                         if u.get('type') == 'SCOUT'}
                        if 'SCOUT' not in directive['scouting']['unit_types'] else set())
            excluded -= {order['unit_id'] for order in directive['tactical_overrides']}
            graph['unassigned_scout_ids'] = sorted(excluded)
            for decision in [*graph.get('decisions', []),
                             *(row.get('decision', {}) for row in graph.get('execution', []))]:
                if decision.get('unit_id') in excluded:
                    decision['assignment_warning'] = 'SCOUT omitted from scouting.unit_types'
            self._emit(runtime, 'strategy_graph', source='autopilot', graph=graph,
                       probability_meaning='seeded action selection; not calibrated success')
            if self._growth is not None:
                attempted = {row['unit_id'] for row in graph.get('execution', [])}
                tactical_ids = {row['unit_id'] for row in directive['tactical_overrides']}
                capital_report = await self._capital.run(
                    curator.state, turn=turn, match_id=self.match_id, graph=graph,
                    execute=curator.execute, refresh=refresh, recovery=recovery['units'],
                    tactical_ids=tactical_ids, untouched_frozen_ids=frozen_ids - attempted)
                self._emit(runtime, 'strategy_initial_capital', **capital_report)
                if capital_report['execution']:
                    mission_report = {'source': 'automatic_settlement_mission', 'execution': [],
                                      'outcome': 'initial_capital_used_mission_action',
                                      'mission': self._growth.mission}
                else:
                    mission_report = await self._settlement.run(
                        self._growth, curator.state, turn=turn, match_id=self.match_id,
                        execute=curator.execute, refresh=refresh, reserved_roles=reserved,
                        recovery=recovery['units'], tactical_ids=tactical_ids,
                        attempted_ids=attempted, untouched_frozen_ids=frozen_ids - attempted)
                self._emit(runtime, 'strategy_settlement', **mission_report)
                self._emit(runtime, 'strategy_growth', source='autopilot',
                           assessment=self._growth.assessment, reserved_roles=reserved)
            await self._economy(runtime, curator, directive)
            # Overrides are ephemeral even if an action was rejected. Never replay
            # a stale attack or coordinate automatically on the following turn.
            self.directive['tactical_overrides'] = []
            await runtime._close_turn(facade)
            self._previous = self._facts(curator)
            self._last_turn = turn
            self._recovery.commit(recovery)
            if self._growth is not None:
                self._growth.complete_turn(turn)
                self._capital.complete_turn(turn)
            pending = self._scouting_feedback.remember_completed(graph, turn=turn)
            self._emit(runtime, 'strategy_turn_closed', source='controller',
                       scouting_pending_confirmation=pending)
            self._failed = False
        except ModelUnavailable as exc:
            self._emit(runtime, 'strategy_failed', reason='model_unavailable')
            raise MatchAborted(f'strategic model unavailable: {exc}') from exc
        except Exception as exc:
            self._emit(runtime, 'strategy_failed', reason=type(exc).__name__)
            raise

    def _recovery_context(self) -> dict | None:
        if self._recovery_proposal is None:
            return None
        proposal = self._recovery_proposal
        columns = ['unit_id', 'state', 'hp', 'max_hp', 'no_gain_turns']
        return {'policy': proposal['policy'], 'observed_turn': proposal['turn'],
                'columns': columns, 'units': [[row[key] for key in columns]
                    for row in proposal['units'].values()],
                'meaning': 'Pauses routine mission; explicit tactical action interrupts one turn. '
                    'Null health is unavailable. A standing order does not prove healing.'}

    @staticmethod
    def _response_shape(reply: Any) -> tuple[dict, list[dict]]:
        """Only bounded categorical metadata; never copy model prose, thinking, or args."""
        known_tools = {tool['name'] for tool in TOOL_SCHEMAS} | {'submit_directive'}
        known_blocks = {'text', 'thinking', 'redacted_thinking', 'tool_use'}
        known_stops = {'end_turn', 'max_tokens', 'tool_use', 'stop_sequence',
                       'pause_turn', 'refusal'}
        blocks = reply.content if isinstance(reply.content, list) else []
        counts: dict[str, int] = {}
        uses = []
        text_chars = 0
        for block in blocks:
            kind = block.get('type') if isinstance(block, dict) else None
            kind = kind if isinstance(kind, str) and kind in known_blocks else 'other'
            counts[kind] = counts.get(kind, 0) + 1
            if kind == 'tool_use':
                uses.append(block)
            elif kind == 'text' and isinstance(block.get('text'), str):
                text_chars += len(block['text'])
        names = [use.get('name') if isinstance(use.get('name'), str)
                 and use['name'] in known_tools else 'other' for use in uses[:16]]
        stop = reply.stop_reason
        return {'stop_reason': stop if isinstance(stop, str) and stop in known_stops else 'other',
                'content_is_list': isinstance(reply.content, list), 'block_count': len(blocks),
                'block_types': counts, 'tool_count': len(uses), 'tool_names': names,
                'tool_names_omitted': max(0, len(uses) - len(names)),
                'text_blocks': counts.get('text', 0), 'text_chars': text_chars}, uses

    async def _provider_call(self, runtime: Any, request_kind: str,
                             action: Any, kwargs: dict) -> Any:
        client = runtime.client
        if not hasattr(client, 'on_request_post'):
            return await action(**kwargs)
        previous = client.on_request_post

        def posted(kind: str) -> None:
            self._emit(runtime, 'strategy_provider_post', request_kind=kind,
                       expected_request_kind=request_kind,
                       posts_sent=client.posts_sent,
                       provider_posts_by_kind=copy.deepcopy(client.posts_by_kind))
            if previous is not None:
                previous(kind)

        client.on_request_post = posted
        try:
            return await action(**kwargs)
        finally:
            client.on_request_post = previous

    async def _count_request(self, runtime: Any, kwargs: dict, *, task: str
                             ) -> GenerationAdmission:
        policy = runtime.llm.adaptive_context
        client_spec = getattr(runtime.client, 'spec', None)
        if client_spec is not None and (client_spec.model_id != runtime.llm.model_id
                                        or client_spec.max_tokens != runtime.llm.max_tokens):
            raise ModelUnavailable(
                'adaptive context unavailable: generation configuration mismatch')
        body = input_payload(runtime.llm.model_id, **kwargs)
        report = measurements(body, runtime.llm.max_tokens)
        report.update(task=task, provider_context_tokens=policy.provider_context_tokens,
                      request_kind='count_tokens')
        self._emit(runtime, 'strategy_token_count_request', **report)
        counter = getattr(runtime.client, 'count_tokens', None)
        if not callable(counter):
            self._emit(runtime, 'strategy_token_count_result', outcome='unavailable',
                       reason='counter_not_supported', **report)
            raise ModelUnavailable('adaptive context unavailable: token counter not supported')
        if not callable(getattr(runtime.client, 'create_admitted', None)):
            raise ModelUnavailable('adaptive context unavailable: bound generation not supported')
        before = getattr(runtime.client, 'posts_sent', 0)
        try:
            async with asyncio.timeout(runtime.llm.request_timeout_s):
                receipt = await self._provider_call(
                    runtime, 'count_tokens', counter, copy.deepcopy(kwargs))
        except (TimeoutError, ModelUnavailable) as exc:
            self._emit(runtime, 'strategy_token_count_result', outcome='unavailable',
                       reason=type(exc).__name__,
                       posts_attempted=getattr(runtime.client, 'posts_sent', before) - before,
                       **report)
            raise ModelUnavailable('adaptive context unavailable: token counter failed') from exc
        valid = (isinstance(receipt, TokenCount) and type(receipt.input_tokens) is int
                 and receipt.input_tokens > 0 and receipt.model == body['model']
                 and receipt.input_payload_sha256 == report['input_payload_sha256']
                 and receipt.source in ('provider_count_tokens', 'injected_token_counter')
                 and payload_hash(input_payload(body['model'], **kwargs))
                 == report['input_payload_sha256'])
        if not valid:
            self._emit(runtime, 'strategy_token_count_result', outcome='invalid_receipt', **report)
            raise ModelUnavailable('adaptive context unavailable: token count identity mismatch')
        total = receipt.input_tokens + report['output_reserve_tokens']
        admitted = total <= report['provider_context_tokens']
        self._emit(runtime, 'strategy_token_count_result',
                   outcome='admitted' if admitted else 'provider_window_exceeded',
                   input_tokens=receipt.input_tokens, total_reserved_tokens=total,
                   count_source=receipt.source,
                   provider_exact=receipt.source == 'provider_count_tokens',
                   posts_attempted=getattr(runtime.client, 'posts_sent', before) - before, **report)
        if not admitted:
            raise ModelUnavailable(
                'provider-counted input plus output reserve exceeds declared window')
        admission = GenerationAdmission(
            encoded({**body, 'max_tokens': report['output_reserve_tokens']}), receipt,
            report['provider_context_tokens'])
        admission.body()
        return admission

    async def _decide(self, runtime: Any, curator: ContextCurator,
                      reasons: list[str], *, opening_actions_pending: bool = True) -> dict:
        adaptive = runtime.llm.adaptive_context
        task = task_for(reasons, opening_actions_pending)
        opening_authority = self.opening_units_frozen and opening_actions_pending
        initial_posts = getattr(runtime.client, 'posts_sent', 0)
        observation = decision_snapshot(curator, runtime._turn)
        metadata = {'turn': runtime._turn, 'player_id': runtime.profile.player_id,
                    'reasons': reasons, 'previous_directive': self.directive,
                    'decision_packet': decision_packet(observation, self._decision_observation),
                    'recovery': self._recovery_context(),
                    'movement_authority': {
                        'opening_units_frozen': opening_authority,
                        'untouched_owned_unit_ids': sorted(
                            u['unit_id'] for u in curator.own('get_units'))
                            if opening_authority else [],
                        'natural_allowance': 'unobserved' if opening_authority
                            else 'use_projected_movement',
                        'legality': 'engine_checked_not_proven_by_this_metadata'}}
        if self._growth is not None:
            metadata['growth'] = self._growth.summary()
            metadata['growth']['execution'] = self._settlement.summary()
            metadata['growth']['initial_capital'] = self._capital.summary()
        schema = {'name': 'submit_directive',
                  'description': 'Submit one strategy; tactical overrides expire this turn.',
                  'input_schema': copy.deepcopy(DIRECTIVE_SCHEMA) if adaptive else DIRECTIVE_SCHEMA}
        if adaptive:
            metadata['briefing_policy'] = {'task': task, 'soft_target_chars':
                getattr(adaptive, task + '_target_chars'),
                'tactical_overrides_allowed': task != 'economy',
                'hard_limit': 'complete provider-counted input plus reserved output window'}
            if task == 'economy':
                schema['input_schema']['properties']['tactical_overrides']['maxItems'] = 0
        # One normal attempt plus at most one format repair, both charged to the
        # existing turn/request caps. Client transport retries still count every POST.
        limit = min(2, runtime.llm.max_tool_rounds - self._turn_strategy_requests)
        if limit < 1:
            raise MatchAborted('strategic model request budget exhausted')
        usage_in = usage_out = 0
        for attempt in range(1, limit + 1):
            posts = getattr(runtime.client, 'posts_sent', 0)
            if posts >= runtime.llm.max_requests_per_match:
                raise MatchAborted('strategic model request budget exhausted before format attempt')
            encoded_metadata = _encode(metadata)
            # Re-budget the complete fresh request INCLUDING repair metadata.
            # Invalid assistant content is discarded, never echoed or interpreted.
            if adaptive:
                target = getattr(adaptive, task + '_target_chars')
                context = encoded_metadata + '\n' + curator.render(adaptive_task=task,
                    target_chars=None if target is None else max(0, target-len(encoded_metadata)-1))
                self._emit(runtime, 'strategy_briefing', **curator.last_render_audit,
                           full_context_chars=len(context), configured_soft_target_chars=target)
            else:
                curator.budget = runtime.llm.max_result_chars - len(encoded_metadata) - 1
                if curator.budget < 1:
                    raise MatchAborted('strategic metadata exceeds context budget')
                context = encoded_metadata + '\n' + curator.render()
            kwargs = {'system': SYSTEM + (GROWTH_SYSTEM if self.growth_autopilot else ''),
                      'messages': [{'role': 'user', 'content': context}],
                      'tools': [schema],
                      'tool_choice': {'type': 'tool', 'name': 'submit_directive'}}
            if adaptive:
                admission = await self._count_request(runtime, kwargs, task=task)
                if getattr(runtime.client, 'posts_sent', 0) >= runtime.llm.max_requests_per_match:
                    raise ModelUnavailable('request budget exhausted after token count')
            posts = getattr(runtime.client, 'posts_sent', 0)
            self._emit(runtime, 'strategy_request', attempt=attempt,
                       request_kind='generation', named_tool='submit_directive',
                       user_context=context,
                       context_sha256=hashlib.sha256(context.encode('utf-8')).hexdigest(),
                       context_chars=len(context),
                       **({'admitted_request_payload_sha256': payload_hash(admission.body()),
                           'admitted_input_payload_sha256':
                               admission.receipt.input_payload_sha256}
                          if adaptive else {}))
            self._turn_strategy_requests += 1
            reply = (await self._provider_call(
                runtime, 'generation', runtime.client.create_admitted, {'admission': admission})
                     if adaptive else await runtime.client.create(**kwargs))
            runtime._report_usage(reply)
            usage_in += reply.input_tokens
            usage_out += reply.output_tokens
            shape, uses = self._response_shape(reply)
            category, reason = 'invalid_shape', 'tool_count'
            directive = None
            if reply.stop_reason == 'max_tokens':
                reason = 'truncated'
            elif len(uses) == 1:
                if uses[0].get('name') != 'submit_directive':
                    reason = 'wrong_tool'
                else:
                    category, reason = 'invalid_args', 'schema_or_ownership'
                    value = uses[0].get('input')
                    try:
                        if len(_encode(value)) > runtime.llm.max_result_chars:
                            reason = 'arguments_oversized'
                        else:
                            directive = validate_directive(
                                value, player_id=runtime.profile.player_id,
                                owned_unit_ids={u['unit_id'] for u in curator.own('get_units')})
                            if adaptive and task == 'economy' and directive['tactical_overrides']:
                                directive = None
                                reason = 'tactical_authority_disabled'
                    except (ValueError, TypeError, OverflowError, RecursionError):
                        # Validation exceptions may contain model values. Use only
                        # fixed categories in durable diagnostics and repair prompts.
                        reason = 'schema_or_ownership'
                    if directive is not None:
                        category, reason = 'valid', 'complete_directive'
            repair_available = (directive is None and attempt < limit
                                and getattr(runtime.client, 'posts_sent', 0)
                                < runtime.llm.max_requests_per_match)
            self._emit(runtime, 'strategy_response_shape', attempt=attempt, category=category,
                       reason=reason, shape=shape, input_tokens=reply.input_tokens,
                       output_tokens=reply.output_tokens, context_chars=len(context),
                       posts_attempted=getattr(runtime.client, 'posts_sent', posts) - posts,
                       repair_available=repair_available)
            if directive is not None:
                self._emit(runtime, 'strategy_decision', source='model', reasons=reasons,
                           directive=directive, model=reply.model,
                           input_tokens=usage_in, output_tokens=usage_out,
                           context_chars=len(context), format_attempts=attempt,
                           posts_attempted=getattr(runtime.client, 'posts_sent', initial_posts)
                           - initial_posts,
                           provider_posts_by_kind=copy.deepcopy(
                               getattr(runtime.client, 'posts_by_kind', {})))
                self._decision_observation = observation
                return directive
            if not repair_available:
                raise MatchAborted(f'strategic directive rejected: {category}/{reason}; '
                                   'no format repair budget remains')
            metadata['format_repair'] = {
                'attempt': 2, 'previous_category': category, 'previous_reason': reason,
                'instruction': 'Return exactly one complete submit_directive tool call '
                               'matching its schema and the currently owned IDs. '
                               'No action from this rejected response has been executed. '
                               'This is the final format attempt.'}
        raise MatchAborted('strategic directive format attempts exhausted')

    async def _economy(self, runtime: Any, curator: ContextCurator, directive: dict) -> None:
        """One attempt per empty queue; preferences never replace an active build."""
        def pick(options: list, key: str, preferences: list) -> str | None:
            available = sorted({row[key] for row in options
                                if isinstance(row, dict) and isinstance(row.get(key), str)})
            return next((item for item in preferences if item in available),
                        available[0] if available else None)

        await curator.refresh()
        if not curator.state['get_overview'].get('you', {}).get('researching'):
            tech = pick(curator.state.get('get_available_research', []), 'tech_id',
                        directive['research_preferences'])
            if tech:
                result = await curator.execute('set_research', {'tech_id': tech})
                self._emit(runtime, 'strategy_economy', source='autopilot', tool='set_research',
                           args={'tech_id': tech}, result=result)
                await curator.refresh()
        cities = curator.own('get_cities')
        if len(cities) > 32:
            raise MatchAborted('strategic economy exceeds 32-city action bound')
        reservations: dict[str, str] = {}
        refreshed_strategy = False
        for initial in sorted(cities, key=lambda city: city['city_id']):
            cid = initial['city_id']
            # An earlier action may have changed a later city. Consult its current
            # observation and all current empire queues before every choice.
            city = next((city for city in curator.own('get_cities')
                         if city['city_id'] == cid), None)
            if city is None or city.get('production_queue'):
                continue
            if self._growth is not None:
                self._growth.refresh(curator.state, catalogs=curator.production)
            policy = choose_production(curator.state, player_id=runtime.profile.player_id,
                                       city_id=cid, options=curator.production.get(cid, []),
                                       directive=directive, reservations=reservations,
                                       growth_policy=self._growth)
            item = policy['item_id']
            if item is None and policy['candidates'] and not refreshed_strategy:
                self._emit(runtime, 'strategy_economy', source='autopilot',
                           tool='set_city_production', production_policy=policy,
                           outcome='requesting_one_strategy_refresh')
                # Current owned roster and all observed queues are already fresh.
                # This uses the existing request/format budgets, with no extra
                # discovery loop or change to context serialization.
                directive = await self._decide(runtime, curator, ['production_targets_satisfied'],
                                               opening_actions_pending=False)
                discarded = sorted(order['unit_id'] for order in directive['tactical_overrides'])
                directive['tactical_overrides'] = []
                self.directive = copy.deepcopy(directive)
                self._last_decision = runtime._turn
                refreshed_strategy = True
                self._emit(runtime, 'strategy_execution', source='model',
                           reasons=['production_targets_satisfied'], phase='economy',
                           directive=directive, discarded_late_tactical_override_ids=discarded,
                           last_decision_turn=self._last_decision)
                if self._growth is not None:
                    self._growth.refresh(curator.state, catalogs=curator.production)
                policy = choose_production(curator.state, player_id=runtime.profile.player_id,
                                           city_id=cid, options=curator.production.get(cid, []),
                                           directive=directive, reservations=reservations,
                                           growth_policy=self._growth)
                item = policy['item_id']
            if item is None:
                self._emit(runtime, 'strategy_economy', source='autopilot',
                           tool='set_city_production', production_policy=policy,
                           outcome='no_eligible_production')
                if self._growth is not None:
                    raise MatchAborted(f'no eligible supported production for {cid}; '
                                       'district/project choices remain unrepresented')
                raise MatchAborted(f'no eligible production within unit targets for {cid}')
            args = {'city_id': cid, 'item_id': item}
            result = await curator.execute('set_city_production', args)
            self._emit(runtime, 'strategy_economy', source='autopilot',
                       tool='set_city_production', args=args, result=result,
                       production_policy=policy)
            if result.get('status') not in ('accepted', 'rejected'):
                raise MatchAborted('production action returned no canonical status')
            if result['status'] == 'accepted':
                reservations[cid] = item
                if self._growth is not None:
                    founder_reserved = self._growth.reserve_founder_production(
                        curator.state, cid, item,
                        preparatory=policy['growth']['preparatory_founder_reserve']['selected'])
                    if founder_reserved:
                        self._emit(runtime, 'strategy_growth_preparation', source='autopilot',
                                   city_id=cid, item_id=item, mission=self._growth.mission,
                                   preparation=self._growth.summary()['preparatory_training'],
                                   authority='accepted_production_not_travel_or_founding')
            await curator.refresh()
