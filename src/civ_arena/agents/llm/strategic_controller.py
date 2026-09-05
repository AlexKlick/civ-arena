"""Opt-in, fresh-match strategic decisions above bounded deterministic execution.

This version deliberately refuses resume: coordinator checkpoints do not yet
persist this controller's directive and trigger history. Audit records describe
reproduction inputs but are not a checkpoint restore implementation.
"""
from __future__ import annotations

import copy
import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from civ_arena.agents.llm.client import ModelUnavailable
from civ_arena.agents.llm.context_curator import ContextCurator
from civ_arena.agents.llm.tool_schemas import TOOL_SCHEMAS
from civ_arena.agents.scouting import run_scouting
from civ_arena.agents.strategy_directive import DIRECTIVE_SCHEMA, validate_directive
from civ_arena.arena.referee import MatchAborted

SYSTEM = """You are the strategic commander of one Civilization seat. Submit exactly
one submit_directive tool call containing one JSON directive. Do not request basic
state, narrate analysis, or call game tools. Current projected state is supplied.
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
Only observed IDs and coordinates may be used. Visible contacts are not proof of
war. Probabilities in the controller graph are seeded action-selection weights,
not calibrated success predictions. No prose or hidden reasoning is required."""


def _encode(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)


@dataclass
class StrategicController:
    match_id: str
    cadence: int = 5
    audit: Callable[[dict], None] | None = None
    opening_units_frozen: bool = False
    directive: dict | None = field(default=None, init=False)
    _last_turn: int = field(default=0, init=False)
    _last_decision: int = field(default=0, init=False)
    _previous: dict | None = field(default=None, init=False)
    _failed: bool = field(default=False, init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.match_id, str) or not self.match_id:
            raise ValueError('strategic controller requires match_id')
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
        try:
            curator = ContextCurator(facade, runtime.profile.player_id,
                                     runtime.llm.max_result_chars)
            await curator.refresh()
            frozen_ids = ({u['unit_id'] for u in curator.own('get_units')}
                          if self.opening_units_frozen else set())
            facts = self._facts(curator)
            reasons = self._reasons(facts, turn, tactical_requested)
            if reasons:
                directive = await self._decide(runtime, curator, reasons)
                self.directive = copy.deepcopy(directive)
                self._last_decision = turn
            else:
                directive = copy.deepcopy(self.directive)
            self._emit(runtime, 'strategy_execution', source='model' if reasons else 'autopilot',
                       reasons=reasons, last_decision_turn=self._last_decision,
                       seed=runtime.profile.seed, directive=directive,
                       persistence='fresh_only_v1', cadence=self.cadence,
                       opening_frozen_unit_ids=sorted(frozen_ids))

            async def refresh() -> dict:
                await curator.refresh()
                return curator.state

            graph = await run_scouting(
                curator.state, directive=directive, player_id=runtime.profile.player_id,
                match_id=self.match_id, agent_id=runtime.profile.agent_id, turn=turn,
                execute=curator.execute, refresh=refresh, seed=runtime.profile.seed,
                frozen_unit_ids=frozen_ids)
            self._emit(runtime, 'strategy_graph', source='autopilot', graph=graph,
                       probability_meaning='seeded action selection; not calibrated success')
            await self._economy(runtime, curator, directive)
            # Overrides are ephemeral even if an action was rejected. Never replay
            # a stale attack or coordinate automatically on the following turn.
            self.directive['tactical_overrides'] = []
            await runtime._close_turn(facade)
            self._previous = self._facts(curator)
            self._last_turn = turn
            self._emit(runtime, 'strategy_turn_closed', source='controller')
            self._failed = False
        except ModelUnavailable as exc:
            self._emit(runtime, 'strategy_failed', reason='model_unavailable')
            raise MatchAborted(f'strategic model unavailable: {exc}') from exc
        except Exception as exc:
            self._emit(runtime, 'strategy_failed', reason=type(exc).__name__)
            raise

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

    async def _decide(self, runtime: Any, curator: ContextCurator,
                      reasons: list[str]) -> dict:
        initial_posts = getattr(runtime.client, 'posts_sent', 0)
        metadata = {'turn': runtime._turn, 'player_id': runtime.profile.player_id,
                    'reasons': reasons, 'previous_directive': self.directive,
                    'movement_authority': {
                        'opening_units_frozen': self.opening_units_frozen,
                        'untouched_owned_unit_ids': sorted(
                            u['unit_id'] for u in curator.own('get_units'))
                            if self.opening_units_frozen else [],
                        'natural_allowance': 'unobserved' if self.opening_units_frozen
                            else 'use_projected_movement',
                        'legality': 'engine_checked_not_proven_by_this_metadata'}}
        schema = {'name': 'submit_directive',
                  'description': 'Submit one strategy; tactical overrides expire this turn.',
                  'input_schema': DIRECTIVE_SCHEMA}
        # One normal attempt plus at most one format repair, both charged to the
        # existing turn/request caps. Client transport retries still count every POST.
        limit = min(2, runtime.llm.max_tool_rounds)
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
            curator.budget = runtime.llm.max_result_chars - len(encoded_metadata) - 1
            if curator.budget < 1:
                raise MatchAborted('strategic metadata exceeds context budget')
            context = encoded_metadata + '\n' + curator.render()
            self._emit(runtime, 'strategy_request', attempt=attempt,
                       named_tool='submit_directive', user_context=context,
                       context_sha256=hashlib.sha256(context.encode('utf-8')).hexdigest(),
                       context_chars=len(context))
            reply = await runtime.client.create(
                system=SYSTEM, messages=[{'role': 'user', 'content': context}], tools=[schema],
                tool_choice={'type': 'tool', 'name': 'submit_directive'})
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
                           - initial_posts)
                return directive
            if not repair_available:
                raise MatchAborted(f'strategic directive rejected: {category}/{reason}; '
                                   'no format repair budget remains')
            metadata['format_repair'] = {
                'attempt': 2, 'previous_category': category, 'previous_reason': reason,
                'instruction': 'Return exactly one complete submit_directive tool call '
                               'matching its schema and the currently owned IDs. '
                               'No game action has been executed. '
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
        for city in cities:
            if city.get('production_queue'):
                continue
            cid = city['city_id']
            item = pick(curator.production.get(cid, []), 'item_id',
                        directive['production_preferences'])
            if item:
                args = {'city_id': cid, 'item_id': item}
                result = await curator.execute('set_city_production', args)
                self._emit(runtime, 'strategy_economy', source='autopilot',
                           tool='set_city_production', args=args, result=result)
                await curator.refresh()
