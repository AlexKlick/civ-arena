"""LLMAgentRuntime: a model-driven agent behind the same take_turn(facade) seam.

Contract notes (the whole lane in one docstring):

- The runtime closes its own phase: a model that will not call end_turn is
  FORCE-closed at the round cap — the match must never stall.
- The model never supplies idempotency keys: identical repeated actions
  inside a turn dedupe automatically by content (the wanted safety property).
- Unknown tool names die at the facade (AttributeError) and malformed
  arguments die at the call — both become error tool_results with FIXED
  telemetry keys ("llm_unknown_tool" / "llm_malformed_args"; never a
  model-supplied dict key) and the loop moves on. No re-prompt repair.
- Per-turn conversation is discarded at end of turn; cross-turn memory is
  the diary plus the rendered strategy view (goals/predictions/beliefs —
  identity-free, injected through the same template-pure header). Assistant
  content is echoed back VERBATIM (thinking blocks included) — proven on
  the wire in docs/llm-lane.md.
- Budget: the runtime checks BEFORE each request and the client counts
  EVERY post (retries included); either side tripping ends the match via
  MatchAborted, so a summary is still written. A ModelUnavailable
  (dead endpoint, auth, malformed shape) is equally match-ending.
- ``rng`` exists because the coordinator's checkpoint path calls
  rng_to_doc on every runtime; the LLM runtime draws none.
- ``begin_turn(turn)`` is called by the coordinator so the turn header is
  correct across resume (a runtime-internal counter would desync).
"""

from __future__ import annotations

import json
import random
from dataclasses import dataclass, field
from typing import Any

from civ_arena.agents.llm.client import (
    ModelClient,
    ModelReply,
    ModelUnavailable,
    tool_uses,
)
from civ_arena.agents.llm.prompts import SYSTEM_PROMPT, turn_header
from civ_arena.agents.llm.tool_schemas import TOOL_SCHEMAS
from civ_arena.agents.runtime import AgentProfile
from civ_arena.arena.referee import MatchAborted
from civ_arena.config import LLMSpec
from civ_arena.strategy.view import render_memory

_SCHEMA_BY_NAME = {s["name"]: s for s in TOOL_SCHEMAS}


@dataclass
class LLMAgentRuntime:
    profile: AgentProfile
    client: ModelClient
    llm: LLMSpec
    telemetry: Any = None
    diary: Any = None
    strategy: Any = None  # StrategyStore; the memory view renders when set
    rng: random.Random = field(default=None)  # type: ignore[assignment]
    _turn: int = field(default=0, init=False)

    def __post_init__(self) -> None:
        if self.rng is None:
            self.rng = random.Random(self.profile.seed)

    @classmethod
    def build(cls, profile: AgentProfile, *, telemetry: Any = None,
              diary: Any = None, strategy: Any = None,
              client: ModelClient | None = None,
              on_post: Any = None) -> LLMAgentRuntime:
        if profile.llm is None:
            raise ValueError(f"agent {profile.agent_id!r}: policy 'llm' "
                             "requires an LLMSpec on the profile")
        if client is None:
            from civ_arena.agents.llm.client import MiniMaxMessagesClient

            client = MiniMaxMessagesClient(profile.llm, on_post=on_post)
        elif on_post is not None and hasattr(client, "on_post") \
                and client.on_post is None:
            client.on_post = on_post
        return cls(profile=profile, client=client, llm=profile.llm,
                   telemetry=telemetry, diary=diary, strategy=strategy)

    # ------------------------------------------------------------- hooks
    def begin_turn(self, turn: int) -> None:
        """Coordinator hook: the authoritative turn number (resume-safe)."""
        self._turn = int(turn)

    def bind_services(self, *, diary: Any = None,
                      strategy: Any = None) -> None:
        """Coordinator hook: receive the arena-owned cross-turn stores
        (injected runtimes get them this way too — construction AND resume).
        An explicit opt-in rather than attribute probing, so a runtime with
        a read-only property never breaks Arena construction."""
        if diary is not None:
            self.diary = diary
        if strategy is not None:
            self.strategy = strategy

    async def aclose(self) -> None:
        await self.client.aclose()

    # -------------------------------------------------------------- turn
    async def take_turn(self, facade: Any) -> None:
        if self._turn < 1:
            raise RuntimeError(
                "begin_turn(turn) must be called before take_turn "
                "(the coordinator does this; direct callers must too)"
            )
        diary_text = (self.diary.get(self.profile.player_id)
                      if self.diary is not None else "")
        memory = (render_memory(self.strategy, self.profile.player_id,
                                self._turn)
                  if self.strategy is not None else "")
        messages: list[dict[str, Any]] = [
            {"role": "user",
             "content": turn_header(self._turn, diary_text, memory)}
        ]
        try:
            for _round in range(self.llm.max_tool_rounds):
                reply = await self._create(messages)
                # verbatim echo: thinking blocks included
                messages.append({"role": "assistant", "content": reply.content})
                self._report_usage(reply)
                uses = tool_uses(reply)
                if not uses:
                    # prose-only reply: per the system prompt that means the
                    # model is done. Force the phase closed — never stall.
                    await self._close_turn(facade)
                    return
                results: list[dict[str, Any]] = []
                for i, use in enumerate(uses):
                    tool_use_id = use.get("id") or \
                        f"toolu_{self._turn}_{_round}_{i}"
                    ok, payload = await self._call_tool(
                        facade, use.get("name"), use.get("input"))
                    results.append({
                        "type": "tool_result",
                        "tool_use_id": tool_use_id,
                        "content": payload,
                        "is_error": not ok,
                    })
                    if ok and use.get("name") == "end_turn":
                        # lease released: sibling calls would be guaranteed
                        # rejections, so skip them entirely
                        return
                messages.append({"role": "user", "content": results})
            # round cap exhausted without end_turn — the runtime closes the
            # phase itself; a model that never finishes cannot stall the match
            await self._close_turn(facade)
        except ModelUnavailable as exc:
            raise MatchAborted(
                f"llm runtime {self.profile.agent_id!r}: {exc}") from exc

    # ------------------------------------------------------------ helpers
    async def _close_turn(self, facade: Any) -> None:
        result = await facade.end_turn()
        if isinstance(result, dict) and result.get("rejection") == "unmoved_units":
            units = result.get("unmoved_units")
            if not isinstance(units, list) or len(units) > 256:
                raise MatchAborted("invalid or oversized completeness repair")
            # One bounded pass, via the same audited facade as model actions.
            for uid in dict.fromkeys(units):
                await facade.fortify(uid)
            result = await facade.end_turn()
        if not isinstance(result, dict) or result.get("status") != "accepted":
            raise MatchAborted("forced end_turn did not release the turn")

    async def _create(self, messages: list[dict[str, Any]]) -> ModelReply:
        posts = getattr(self.client, "posts_sent", 0)
        if posts >= self.llm.max_requests_per_match:
            raise MatchAborted(
                f"llm request budget exhausted ({posts} posts >= "
                f"{self.llm.max_requests_per_match}) for "
                f"{self.profile.agent_id!r}"
            )
        return await self.client.create(system=SYSTEM_PROMPT, messages=messages,
                                        tools=TOOL_SCHEMAS)

    async def _call_tool(self, facade: Any, name: Any, args: Any
                         ) -> tuple[bool, str]:
        if not isinstance(name, str) or name not in _SCHEMA_BY_NAME:
            # manifest lookup FIRST: a facade attribute that is not a tool
            # (e.g. "names") must degrade to an error result, never dispatch
            self._note_error("llm_unknown_tool")
            return False, self._error(f"unknown tool: {name!r}")
        if not isinstance(args, dict):
            self._note_error("llm_malformed_args")
            return False, self._error("tool arguments must be a JSON object")
        schema = _SCHEMA_BY_NAME[name]["input_schema"]
        properties = schema.get("properties", {})
        required = set(schema.get("required", []))
        unknown = set(args) - set(properties)
        if unknown or not required <= set(args):
            self._note_error("llm_malformed_args")
            return False, self._error(
                f"bad arguments: unknown keys {sorted(unknown)}, required "
                f"{sorted(required)}, got {sorted(args)}"
            )
        for key, value in args.items():
            declared = properties[key].get("type")
            if declared == "string" and not isinstance(value, str) \
                    or declared == "integer" and (
                        not isinstance(value, int) or isinstance(value, bool)):
                self._note_error("llm_malformed_args")
                return False, self._error(
                    f"bad arguments: {key} must be {declared}, got "
                    f"{type(value).__name__}"
                )
            max_len = properties[key].get("maxLength")
            if (max_len is not None and isinstance(value, str)
                    and len(value) > max_len):
                # bounded BEFORE any log record: an oversized diary note
                # rejected here never reaches the referee, so no event is
                # emitted and the model-free replay cannot diverge on it
                self._note_error("llm_malformed_args")
                return False, self._error(
                    f"bad arguments: {key} exceeds maxLength {max_len} "
                    f"(got {len(value)} chars)"
                )
        try:
            # kwargs dispatch: no positional reordering can ever reinterpret
            # a malformed argument as a different parameter
            result = await getattr(facade, name)(**args)
        except TypeError as exc:
            self._note_error("llm_malformed_args")
            return False, self._error(f"bad arguments: {exc}")
        ok = not isinstance(result, dict) or result.get("status") != "rejected"
        if name == "end_turn":
            ok = isinstance(result, dict) and result.get("status") == "accepted"
        return ok, self._compact(result)

    def _compact(self, result: Any) -> str:
        payload = json.dumps(result, sort_keys=True, default=str)
        if len(payload) > self.llm.max_result_chars:
            return (payload[: self.llm.max_result_chars]
                    + f"\n...[truncated, full {len(payload)} chars]")
        return payload

    def _error(self, message: str) -> str:
        return json.dumps({"error": message}, sort_keys=True)

    def _note_error(self, key: str) -> None:
        if self.telemetry is not None:
            self.telemetry.note_model_error(self.profile.agent_id, key)

    def _report_usage(self, reply: ModelReply) -> None:
        if self.telemetry is not None:
            self.telemetry.note_model_usage(
                self.profile.agent_id, reply.model,
                reply.input_tokens, reply.output_tokens)
