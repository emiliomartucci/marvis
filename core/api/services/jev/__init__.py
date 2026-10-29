# v1.0.0 - 2026-09-21 - Plan 2026-09-21 U1: Jev decision provider package.
"""Jev (TypeSafe System One) provider package — the ``ScoringProvider`` port.

Import surface for callers:

- :data:`ScoringProvider` / :class:`~core.api.services.jev.types.ScoringQuestion`
  and friends from :mod:`.types` — always present, provider-neutral;
- :func:`load_provider` — returns the concrete adapter, or ``None`` on the
  OSS mirror where the provider carve-out (``client.py``) is removed by
  ``core/scripts/mirror/mirror_filter.sh`` (same pattern as
  ``embedding_backends/remote_backend.py``).

Callers must treat ``None`` and every unavailable outcome as fail-closed and
keep their deterministic behaviour (plan R17).
"""
from __future__ import annotations

import asyncio
import os
from typing import Any
from weakref import WeakKeyDictionary

from core.api.services.jev.flags import (
    FlagDecision,
    FlagResolver,
    get_flag_resolver,
    reset_flag_resolver,
)
from core.api.services.jev.ledger import OUTCOMES, iter_events, record_call
from core.api.services.jev.types import (
    ScoringAnswer,
    ScoringBatchResult,
    ScoringProvider,
    ScoringQuestion,
)

try:  # provider carve-out: absent on the OSS mirror
    from core.api.services.jev.client import (
        JevScoringProvider,
        choice,
        noul,
        score_question,
    )
except ImportError:  # pragma: no cover - mirror build only
    JevScoringProvider = None  # type: ignore[assignment]
    choice = None  # type: ignore[assignment]
    noul = None  # type: ignore[assignment]
    score_question = None  # type: ignore[assignment]

try:  # local adapter is absent when the scoring package is carved out
    from core.api.services.jev.laya_client import LayaScoringProvider
except ImportError:  # pragma: no cover - mirror build only
    LayaScoringProvider = None  # type: ignore[assignment]


_providers: WeakKeyDictionary = WeakKeyDictionary()


def load_provider(**kwargs: Any) -> "ScoringProvider | None":
    """Reuse the loop's adapter; explicit overrides create caller-owned instances.

    HTTP pools and asyncio guards must never cross event loops. Application
    entrypoints close their shared instance before shutting down the loop.
    """
    provider_name = os.environ.get("MARVIS_SCORING_PROVIDER", "jev").strip().lower()
    provider_class = {"jev": JevScoringProvider, "laya": LayaScoringProvider}.get(provider_name)
    if provider_class is None:
        return None
    explicit = bool(kwargs)
    if provider_name == "laya":
        from core.api.config import settings

        # The local adapter shares the public scoring policy knobs. A customer
        # limit must apply to the ordinary cached load_provider() path too.
        defaults = {
            "timeout_seconds": settings.typesafe_timeout_seconds,
            "daily_call_ceiling": settings.typesafe_daily_call_ceiling,
            "max_concurrent": min(
                settings.typesafe_max_concurrent_per_tenant,
                settings.typesafe_max_concurrent_global,
            ),
            "breaker_failures": settings.typesafe_breaker_failures,
            "breaker_cooldown_seconds": settings.typesafe_breaker_cooldown_seconds,
            "model": settings.marvis_laya_model,
        }
        kwargs = {**defaults, **kwargs}
    if explicit:
        return provider_class(**kwargs)
    loop = asyncio.get_running_loop()
    provider = _providers.get(loop)
    if provider is None:
        provider = provider_class(**kwargs)
        _providers[loop] = provider
    return provider


async def close_provider() -> None:
    """Close only the calling loop's shared provider (also safe in the mirror)."""
    provider = _providers.pop(asyncio.get_running_loop(), None)
    if provider is not None:
        await provider.aclose()


def check_provider_configuration() -> None:
    provider = load_provider()
    if provider is not None:
        provider.check_configuration(os.environ.get("TENANT_ID", "tenant").strip() or "tenant")


def provider_health() -> dict[str, Any]:
    provider = load_provider()
    return provider.health_snapshot() if provider is not None else {"status": "unavailable"}


__all__ = [
    "OUTCOMES",
    "FlagDecision",
    "FlagResolver",
    "JevScoringProvider",
    "LayaScoringProvider",
    "ScoringAnswer",
    "ScoringBatchResult",
    "ScoringProvider",
    "ScoringQuestion",
    "choice",
    "check_provider_configuration",
    "close_provider",
    "get_flag_resolver",
    "iter_events",
    "load_provider",
    "noul",
    "provider_health",
    "record_call",
    "reset_flag_resolver",
    "score_question",
]
