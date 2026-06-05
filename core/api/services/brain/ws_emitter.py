# Brain v1 — WebSocket emitter (sub-05 S5, §4.16).
# Server-side broadcast of `marvisx:brain_cycle_changed` payloads. Each
# subscriber gets a per-connection visibility filter applied at emit time —
# never bypass it (sub-05 §9 cross-cutting invariant 2).
from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class Subscriber:
    """One active /ws/brain connection."""

    ws: Any  # fastapi.WebSocket — not typed to keep this module dep-light
    user_id: str
    system_role: str
    workspace_id: str
    visible_projects: set[str] = field(default_factory=set)
    is_unrestricted: bool = False
    queue: asyncio.Queue[dict[str, Any]] = field(default_factory=lambda: asyncio.Queue(maxsize=64))


class BrainWebSocketHub:
    """Per-process registry of active /ws/brain subscribers.

    The hub is intentionally simple: O(N) fan-out on emit (N expected to be
    in the low dozens — Console operators + one or two agents). No replay
    buffer in v1; clients refetch on reconnect.
    """

    def __init__(self) -> None:
        # Dataclass-based Subscriber is unhashable by design (it carries mutable
        # state). Storing references in a list keeps identity semantics
        # without requiring __hash__.
        self._subscribers: list[Subscriber] = []
        self._lock = asyncio.Lock()

    async def register(self, sub: Subscriber) -> None:
        async with self._lock:
            if sub not in self._subscribers:
                self._subscribers.append(sub)

    async def unregister(self, sub: Subscriber) -> None:
        async with self._lock:
            try:
                self._subscribers.remove(sub)
            except ValueError:
                pass

    def subscriber_count(self) -> int:
        return len(self._subscribers)

    def _is_visible_to(
        self, involved_projects: Iterable[str] | None, sub: Subscriber
    ) -> bool:
        if sub.is_unrestricted:
            return True
        if not involved_projects:
            return True  # cycle-level events have no scope filter
        return set(involved_projects).issubset(sub.visible_projects)

    async def broadcast(
        self,
        *,
        payload: dict[str, Any],
        involved_projects: Iterable[str] | None = None,
    ) -> int:
        """Fan-out a payload. Returns the number of subscribers notified.

        Visibility filter: subscribers see the event iff they have access to
        ALL `involved_projects`. Unrestricted (admin/super_admin) bypass the
        filter (see sub-05 §5 role matrix).
        """
        async with self._lock:
            targets = list(self._subscribers)

        notified = 0
        for sub in targets:
            if not self._is_visible_to(involved_projects, sub):
                continue
            try:
                sub.queue.put_nowait(payload)
                notified += 1
            except asyncio.QueueFull:
                logger.warning(
                    "ws_emitter: dropping payload for slow subscriber user=%s",
                    sub.user_id,
                )
        return notified

    async def emit_cycle_changed(
        self,
        *,
        cycle_key: str,
        run_id: str | None,
        status: str,
        phase: str,
        deltas: dict[str, int] | None = None,
        involved_projects: Iterable[str] | None = None,
    ) -> int:
        payload = {
            "type": "marvisx:brain_cycle_changed",
            "cycle_key": cycle_key,
            "run_id": run_id,
            "status": status,
            "phase": phase,
            "deltas": deltas or {
                "events": 0,
                "drift": 0,
                "memory_ops": 0,
                "findings": 0,
            },
        }
        return await self.broadcast(
            payload=payload, involved_projects=involved_projects
        )


_HUB = BrainWebSocketHub()


def get_hub() -> BrainWebSocketHub:
    return _HUB


def _serialize(payload: dict[str, Any]) -> str:
    return json.dumps(payload, separators=(",", ":"))


async def emit_phase_complete(
    *,
    cycle_key: str,
    run_id: str | None,
    status: str,
    phase: str,
    deltas: dict[str, int] | None = None,
    involved_projects: Iterable[str] | None = None,
) -> int:
    """Convenience entry point used by jobs.py + recompute.

    Never raises — emitter failures must not abort the cycle.
    """
    try:
        return await _HUB.emit_cycle_changed(
            cycle_key=cycle_key,
            run_id=run_id,
            status=status,
            phase=phase,
            deltas=deltas,
            involved_projects=involved_projects,
        )
    except Exception:
        logger.exception("ws_emitter: emit_phase_complete failed")
        return 0


__all__ = [
    "BrainWebSocketHub",
    "Subscriber",
    "_serialize",
    "emit_phase_complete",
    "get_hub",
]
