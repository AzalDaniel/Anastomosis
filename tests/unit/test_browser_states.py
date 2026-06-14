"""Upload state-machine tests: the legal-transition graph is exactly the
contract, and every illegal move is a loud, named failure."""

from __future__ import annotations

import pytest

from anastomosis.deliver.browser.errors import IllegalTransitionError
from anastomosis.deliver.browser.states import (
    CRASH_RECOVERY,
    LEGAL_TRANSITIONS,
    TERMINAL_STATES,
    UploadState,
    validate_transition,
)

# Every (current, new) pair the graph declares legal — the full matrix.
_LEGAL_PAIRS = [(current, new) for current, allowed in LEGAL_TRANSITIONS.items() for new in allowed]

# A representative illegal set: every terminal state as a source (terminals
# own no transitions), plus a few skipped-step and backward moves.
_ILLEGAL_PAIRS = [
    *((term, UploadState.PENDING) for term in TERMINAL_STATES),
    (UploadState.PENDING, UploadState.UPLOADING),  # skips resolve + pre-verify
    (UploadState.PENDING, UploadState.COMPLETED),  # skips everything
    (UploadState.COMPLETED, UploadState.PENDING),  # out of a terminal state
    (UploadState.RESOLVING_PATIENT, UploadState.COMPLETED),  # skips upload
    (UploadState.UPLOADING, UploadState.PENDING),  # backward, not via recovery
    (UploadState.VERIFYING_POST, UploadState.UPLOADING),  # backward
]


def test_every_state_is_a_transition_key() -> None:
    assert set(LEGAL_TRANSITIONS) == set(UploadState)


def test_terminal_states_have_empty_transition_sets() -> None:
    for state in UploadState:
        is_terminal = state in TERMINAL_STATES
        has_no_exits = LEGAL_TRANSITIONS[state] == frozenset()
        assert is_terminal == has_no_exits, state


def test_fifteen_states_seven_non_terminal_eight_terminal() -> None:
    assert len(UploadState) == 15
    assert len(TERMINAL_STATES) == 8
    assert len(set(UploadState) - TERMINAL_STATES) == 7


def test_state_values_are_lowercase_snake_and_unique() -> None:
    values = [s.value for s in UploadState]
    assert len(set(values)) == len(values)
    for value in values:
        assert value == value.lower()
        assert " " not in value and "-" not in value


@pytest.mark.parametrize(("current", "new"), _LEGAL_PAIRS)
def test_legal_transitions_accepted(current: UploadState, new: UploadState) -> None:
    validate_transition(current, new)  # must not raise


@pytest.mark.parametrize(("current", "new"), _ILLEGAL_PAIRS)
def test_illegal_transitions_raise_naming_both_states(
    current: UploadState, new: UploadState
) -> None:
    with pytest.raises(IllegalTransitionError) as excinfo:
        validate_transition(current, new)
    message = str(excinfo.value)
    assert current.name in message
    assert new.name in message


def test_illegal_set_covers_every_terminal_source() -> None:
    sources = {current for current, _ in _ILLEGAL_PAIRS}
    assert TERMINAL_STATES <= sources
    assert len(_ILLEGAL_PAIRS) >= 10


def test_crash_recovery_targets_are_safe() -> None:
    # No work is in flight from a recovered state, and the two states that may
    # have left bytes at the destination land on UPLOAD_INTERRUPTED (the
    # duplicate-scan re-entry), never PENDING.
    assert CRASH_RECOVERY[UploadState.RESOLVING_PATIENT] is UploadState.PENDING
    assert CRASH_RECOVERY[UploadState.VERIFYING_PRE] is UploadState.PENDING
    assert CRASH_RECOVERY[UploadState.UPLOADING] is UploadState.UPLOAD_INTERRUPTED
    assert CRASH_RECOVERY[UploadState.VERIFYING_POST] is UploadState.UPLOAD_INTERRUPTED
    # Recovery only ever applies to non-terminal sources.
    assert set(CRASH_RECOVERY).isdisjoint(TERMINAL_STATES)


def test_upload_interrupted_re_enters_through_resolution() -> None:
    # The resume invariant: an interrupted upload must pass back through
    # RESOLVING_PATIENT (where the duplicate scan runs) before any re-send.
    assert LEGAL_TRANSITIONS[UploadState.UPLOAD_INTERRUPTED] == frozenset(
        {UploadState.RESOLVING_PATIENT}
    )
