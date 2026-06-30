# v1.0.0 - 2026-04-22 - MetricsProvider Protocol + registry (PR1)
"""Provider-agnostic metrics abstraction.

Claude Code (JSONL) and OpenCode (SQLite) both expose per-conversation
metrics (cost, context %, tokens, model). This module defines the slim
2-method Protocol they both implement, plus a string-keyed registry so the
maintenance loop and routers dispatch uniformly by `provider` column.

Keep this intentionally tiny:
  - NO @runtime_checkable — measurable overhead in 3.12+ and duck-typing
    via attribute access is enough for our needs.
  - Static metadata (context_window, pricing) lives in model_registry, not
    on the Protocol — a model isn't tied to one provider.
"""
from __future__ import annotations

from typing import Protocol

from core.api.services.claude_metrics import ClaudeMetricsProvider, SessionMetrics
from core.api.services.codex_metrics import CodexMetricsProvider
from core.api.services.opencode_metrics import OpenCodeMetricsProvider


class MetricsProvider(Protocol):
    """Slim 2-method contract implemented by Claude/OpenCode/... providers."""

    name: str

    def parse_session(
        self,
        session_id: str,
        cwd: str | None = None,
    ) -> SessionMetrics | None:
        """Return aggregate metrics for a conversation, or None if unavailable.

        `None` means expected-but-missing (file gone, DB locked, no assistant
        messages yet). Raise only on contract violation.
        """
        ...

    def get_last_context_pct(
        self,
        session_id: str,
        cwd: str | None = None,
    ) -> float | None:
        """Fast last-message context % (0-100), or None if unavailable."""
        ...


METRICS_PROVIDERS: dict[str, MetricsProvider] = {
    "claude": ClaudeMetricsProvider(),
    "codex": CodexMetricsProvider(),
    "opencode": OpenCodeMetricsProvider(),
}


def get_metrics_provider(name: str | None) -> MetricsProvider | None:
    """Return the provider for a name, or None if unknown.

    `None` input falls back to Claude (default provider for legacy rows that
    predate the `provider` column being populated).
    """
    if name is None:
        return METRICS_PROVIDERS.get("claude")
    return METRICS_PROVIDERS.get(name)
