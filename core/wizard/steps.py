"""Step ordering + transition functions.

Each function mutates and returns the same WizardState instance so callers
can chain or treat it as immutable depending on need.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Final

from .state import StepId, WizardState

STEP_ORDER: Final[list[StepId]] = [
    StepId.welcome,
    StepId.storage,
    StepId.llm_provider,
    StepId.first_project,
    StepId.recap,
]

SKIPPABLE: Final[frozenset[StepId]] = frozenset(
    {StepId.storage, StepId.llm_provider, StepId.first_project}
)


def next_step(state: WizardState) -> StepId | None:
    idx = STEP_ORDER.index(state.current_step)
    if idx + 1 >= len(STEP_ORDER):
        return None
    return STEP_ORDER[idx + 1]


def previous_step(state: WizardState) -> StepId | None:
    idx = STEP_ORDER.index(state.current_step)
    if idx == 0:
        return None
    return STEP_ORDER[idx - 1]


def advance(state: WizardState) -> WizardState:
    state.mark_completed(state.current_step)
    nxt = next_step(state)
    if nxt is not None:
        state.current_step = nxt
    return state


def go_back(state: WizardState) -> WizardState:
    prv = previous_step(state)
    if prv is not None:
        state.current_step = prv
    return state


def skip_current(state: WizardState) -> WizardState:
    if state.current_step not in SKIPPABLE:
        raise ValueError(f"Step {state.current_step.value!r} cannot be skipped")
    state.mark_skipped(state.current_step)
    nxt = next_step(state)
    if nxt is not None:
        state.current_step = nxt
    return state


def finalize(state: WizardState) -> WizardState:
    """Mark the current step (recap) as completed and stamp completed_at."""
    state.mark_completed(state.current_step)
    state.completed_at = datetime.now(timezone.utc)
    return state
