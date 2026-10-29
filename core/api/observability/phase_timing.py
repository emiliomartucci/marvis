"""Per-call phase timings attached to the ``marvis.tool_call`` log line.

The tool-profiles middleware opens one accumulator per tool call; code on the
hot path wraps its expensive steps in ``phase("name")`` (or ``aphase`` for
awaited work) and the durations, in ms, land in the call's log line as
``phases``. The accumulator is a dict held in a ContextVar, so steps in
``asyncio`` tasks and ``asyncio.to_thread`` workers spawned by the call add to
the same dict. Outside a tool call every helper is a no-op.

Concurrent steps each record their own wall time, so phases may sum to more
than the call's ``ms``.
"""
from __future__ import annotations

import contextvars
import time
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager

_PHASES: contextvars.ContextVar[dict[str, float] | None] = contextvars.ContextVar(
    "mcp_phase_timings", default=None
)


def begin() -> contextvars.Token:
    return _PHASES.set({})


def end(token: contextvars.Token | None) -> dict[str, float]:
    phases = _PHASES.get() or {}
    if token is not None:
        _PHASES.reset(token)
    return dict(phases)


def record(name: str, started: float) -> None:
    """Add the time since ``started`` (a ``time.perf_counter()`` value)."""
    phases = _PHASES.get()
    if phases is not None:
        phases[name] = phases.get(name, 0.0) + (time.perf_counter() - started) * 1000.0


@contextmanager
def phase(name: str) -> Iterator[None]:
    started = time.perf_counter()
    try:
        yield
    finally:
        record(name, started)


@asynccontextmanager
async def aphase(name: str) -> AsyncIterator[None]:
    started = time.perf_counter()
    try:
        yield
    finally:
        record(name, started)
