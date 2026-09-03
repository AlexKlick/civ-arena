"""Read-only parent inspection for non-scored V2 child episodes."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from civ_arena.v2.contracts import (
    ArtifactRefV2,
    ComputeConfigV2,
    EpisodeConfigV2,
    EpisodeReceiptV2,
    EventTypeV2,
    PolicyDescriptorV2,
    PolicySpendV2,
    PolicyStateV2,
    TurnProposalV2,
    TurnReceiptV2,
    TurnTerminationV2,
)
from civ_arena.v2.ledger import (
    LedgerIntegrityError,
    ObjectStoreV2,
    load_events_v2,
    verify_ledger_v2,
)
from civ_arena.v2.schemas import ContractError


@dataclass(frozen=True)
class ResumePlanV2:
    parent_episode_dir: Path
    parent_episode_id: str
    parent_terminal_event_hash: str
    checkpoint_sequence: int
    checkpoint_event_hash: str
    completed_phases: int
    policy_states: tuple[tuple[int, tuple[PolicyStateV2, ...]], ...]
    spend_attempts: tuple[tuple[int, int], ...]
    episode_config: EpisodeConfigV2

    def states_for(self, player_id: int) -> tuple[PolicyStateV2, ...]:
        return dict(self.policy_states).get(player_id, ())

    def spends_for(self, player_id: int) -> int:
        return dict(self.spend_attempts).get(player_id, 0)


def resume_checkpoint_sequence_v2(parent_episode_dir: Path | str) -> int:
    """Return the policy-state event closing the last completed parent phase."""

    root = Path(parent_episode_dir)
    events = load_events_v2(root / "events.jsonl")
    if not events or events[-1].event_type is not EventTypeV2.EPISODE_TERMINATED:
        raise LedgerIntegrityError("resume parent is not terminally receipted")
    completed = [
        (event.sequence_number, event.payload_value)
        for event in events[1:-1]
        if event.event_type is EventTypeV2.TURN_COMPLETED
        and isinstance(event.payload_value, TurnReceiptV2)
        and event.payload_value.termination is TurnTerminationV2.COMPLETED
    ]
    if not completed:
        return 0
    receipt_sequence, receipt = completed[-1]
    assert isinstance(receipt, TurnReceiptV2)
    store = ObjectStoreV2(root)
    for event in events[receipt_sequence + 1 : -1]:
        if event.event_type is not EventTypeV2.POLICY_STATE_RECORDED:
            continue
        state = _artifact_model(store, event.payload_value, PolicyStateV2)
        assert isinstance(state, PolicyStateV2)
        if state.player_id == receipt.player_id and state.proposal_id == receipt.proposal_id:
            return event.sequence_number
    raise ContractError("resume parent predates V2 policy-state custody at its checkpoint")


def _artifact_model(
    store: ObjectStoreV2,
    value: object,
    model: (
        type[EpisodeConfigV2]
        | type[PolicyStateV2]
        | type[PolicySpendV2]
        | type[TurnProposalV2]
    ),
) -> EpisodeConfigV2 | PolicyStateV2 | PolicySpendV2 | TurnProposalV2:
    if not isinstance(value, ArtifactRefV2):
        raise LedgerIntegrityError("V2 custody event does not reference an artifact")
    if value.schema_ref != model.SCHEMA_REF:
        raise LedgerIntegrityError("V2 custody artifact declares the wrong schema")
    return model.from_doc(store.read_doc(value))


def inspect_resume_parent_v2(
    parent_episode_dir: Path | str,
    *,
    environment_id: str,
    policies_by_player: dict[int, PolicyDescriptorV2],
    seed: int | None,
    compute: ComputeConfigV2,
    episode_config: EpisodeConfigV2,
) -> ResumePlanV2:
    """Validate a terminal parent and select its last complete phase boundary.

    The original episode is never opened for writing. A failed parent may have
    later partial-turn evidence; the child deterministically rolls gameplay and
    policy memory back to the final completed phase while retaining every
    provider POST attempt for cumulative budget accounting.
    """

    root = Path(parent_episode_dir)
    verification = verify_ledger_v2(
        root,
        expected_environment_id=environment_id,
        require_terminal=True,
    )
    events = load_events_v2(root / "events.jsonl")
    terminal = events[-1].payload_value
    if not isinstance(terminal, EpisodeReceiptV2):
        raise LedgerIntegrityError("resume parent has no terminal episode receipt")
    if terminal.scored:
        raise ContractError("scored V2 episodes cannot resume")
    if terminal.seed != seed:
        raise ContractError("resume parent seed does not match the configured child")
    if terminal.compute != compute:
        raise ContractError("resume parent compute contract does not match the child")
    configured = tuple(
        sorted(policies_by_player.values(), key=lambda item: item.descriptor_id)
    )
    if terminal.policies != configured:
        raise ContractError("resume parent policy identities do not match the child")

    store = ObjectStoreV2(root)
    config_events = [
        event
        for event in events[1:-1]
        if event.event_type is EventTypeV2.EPISODE_CONFIG_RECORDED
    ]
    if len(config_events) != 1 or config_events[0].sequence_number != 1:
        raise LedgerIntegrityError(
            "resume parent must custody exactly one episode config before turn events"
        )
    parent_config = _artifact_model(
        store,
        config_events[0].payload_value,
        EpisodeConfigV2,
    )
    assert isinstance(parent_config, EpisodeConfigV2)
    if parent_config != episode_config:
        raise ContractError("resume parent episode config does not match the child")

    proposals: dict[str, tuple[str, int]] = {}
    completed: list[tuple[int, TurnReceiptV2]] = []
    state_by_sequence: dict[int, PolicyStateV2] = {}
    spends: dict[int, list[PolicySpendV2]] = {
        player_id: [] for player_id in policies_by_player
    }

    for event in events[1:-1]:
        if event.event_type is EventTypeV2.POLICY_PROPOSAL_RECORDED:
            proposal = _artifact_model(store, event.payload_value, TurnProposalV2)
            assert isinstance(proposal, TurnProposalV2)
            proposals[proposal.proposal_id] = (proposal.policy_id, event.turn_id or 0)
        elif event.event_type is EventTypeV2.POLICY_STATE_RECORDED:
            state = _artifact_model(store, event.payload_value, PolicyStateV2)
            assert isinstance(state, PolicyStateV2)
            descriptor = policies_by_player.get(state.player_id)
            if descriptor is None or descriptor.descriptor_id != state.policy_id:
                raise ContractError("resume policy state has an unknown seat identity")
            if event.turn_id != state.turn:
                raise ContractError("resume policy state turn does not match its event")
            if state.proposal_id is not None and proposals.get(state.proposal_id) != (
                state.policy_id,
                state.turn,
            ):
                raise ContractError("resume policy state is not bound to its proposal")
            state_by_sequence[event.sequence_number] = state
        elif event.event_type is EventTypeV2.POLICY_SPEND_RECORDED:
            spend = _artifact_model(store, event.payload_value, PolicySpendV2)
            assert isinstance(spend, PolicySpendV2)
            descriptor = policies_by_player.get(spend.player_id)
            if descriptor is None or descriptor.descriptor_id != spend.policy_id:
                raise ContractError("resume spend has an unknown seat identity")
            if event.turn_id != spend.turn:
                raise ContractError("resume spend turn does not match its event")
            spends[spend.player_id].append(spend)
        elif event.event_type is EventTypeV2.TURN_COMPLETED:
            receipt = event.payload_value
            if not isinstance(receipt, TurnReceiptV2):
                raise LedgerIntegrityError("TurnCompleted does not contain a receipt")
            if receipt.termination is TurnTerminationV2.COMPLETED:
                completed.append((event.sequence_number, receipt))

    if terminal.turns_completed != len(completed):
        raise LedgerIntegrityError(
            "resume parent phase count does not match completed turn receipts"
        )

    checkpoint_sequence = resume_checkpoint_sequence_v2(root)

    checkpoint_event = events[checkpoint_sequence]
    selected_states: dict[int, list[PolicyStateV2]] = {
        player_id: [] for player_id in policies_by_player
    }
    for sequence, state in sorted(state_by_sequence.items()):
        if sequence <= checkpoint_sequence:
            selected_states[state.player_id].append(state)

    spend_counts: dict[int, int] = {}
    for player_id, rows in spends.items():
        attempts = [item.attempt for item in rows]
        expected = list(range(1, len(rows) + 1))
        if attempts != expected:
            raise ContractError("resume provider-attempt sequence is not contiguous")
        spend_counts[player_id] = len(rows)

    return ResumePlanV2(
        parent_episode_dir=root,
        parent_episode_id=terminal.episode_id,
        parent_terminal_event_hash=verification.terminal_event_hash,
        checkpoint_sequence=checkpoint_sequence,
        checkpoint_event_hash=checkpoint_event.event_hash,
        completed_phases=len(completed),
        policy_states=tuple(
            (player_id, tuple(states))
            for player_id, states in sorted(selected_states.items())
        ),
        spend_attempts=tuple(sorted(spend_counts.items())),
        episode_config=parent_config,
    )
