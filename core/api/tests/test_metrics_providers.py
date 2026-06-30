"""Protocol compliance + registry dispatch for MetricsProvider."""
from __future__ import annotations

import pytest

from core.api.services import claude_metrics
from core.api.services.claude_metrics import ClaudeMetricsProvider
from core.api.services.codex_metrics import CodexMetricsProvider
from core.api.services.metrics_providers import (
    METRICS_PROVIDERS,
    MetricsProvider,
    get_metrics_provider,
)
from core.api.services.opencode_metrics import OpenCodeMetricsProvider


def _satisfies_protocol(obj) -> bool:
    """Duck-typed Protocol check (we deliberately don't use @runtime_checkable)."""
    return (
        hasattr(obj, "name")
        and isinstance(obj.name, str)
        and callable(getattr(obj, "parse_session", None))
        and callable(getattr(obj, "get_last_context_pct", None))
    )


def test_claude_provider_has_name():
    assert ClaudeMetricsProvider.name == "claude"


def test_opencode_provider_has_name():
    assert OpenCodeMetricsProvider.name == "opencode"


def test_codex_provider_has_name():
    assert CodexMetricsProvider.name == "codex"


def test_claude_provider_satisfies_protocol():
    assert _satisfies_protocol(ClaudeMetricsProvider())


def test_opencode_provider_satisfies_protocol():
    assert _satisfies_protocol(OpenCodeMetricsProvider())


def test_codex_provider_satisfies_protocol():
    assert _satisfies_protocol(CodexMetricsProvider())


def test_protocol_is_slim_two_methods():
    """Protocol should declare exactly parse_session + get_last_context_pct + name attr."""
    members = set(dir(MetricsProvider))
    assert "parse_session" in members
    assert "get_last_context_pct" in members
    # `name` is an annotated class var — check via __annotations__
    annotations = getattr(MetricsProvider, "__annotations__", {})
    assert "name" in annotations
    # PEP 563 stringified via `from __future__ import annotations`
    assert annotations["name"] in (str, "str")


def test_registry_has_claude_and_opencode():
    assert "claude" in METRICS_PROVIDERS
    assert "opencode" in METRICS_PROVIDERS
    assert "codex" in METRICS_PROVIDERS
    assert METRICS_PROVIDERS["claude"].name == "claude"
    assert METRICS_PROVIDERS["opencode"].name == "opencode"
    assert METRICS_PROVIDERS["codex"].name == "codex"


def test_get_metrics_provider_claude():
    mp = get_metrics_provider("claude")
    assert mp is not None
    assert mp.name == "claude"
    assert isinstance(mp, ClaudeMetricsProvider)


def test_get_metrics_provider_opencode():
    mp = get_metrics_provider("opencode")
    assert mp is not None
    assert mp.name == "opencode"
    assert isinstance(mp, OpenCodeMetricsProvider)


def test_get_metrics_provider_codex():
    mp = get_metrics_provider("codex")
    assert mp is not None
    assert mp.name == "codex"
    assert isinstance(mp, CodexMetricsProvider)


def test_get_metrics_provider_unknown_returns_none():
    assert get_metrics_provider("gpt") is None
    assert get_metrics_provider("") is None  # empty string is explicit unknown
    assert get_metrics_provider("gemini") is None


def test_get_metrics_provider_none_defaults_to_claude():
    """Legacy rows with NULL provider column should dispatch to Claude."""
    mp = get_metrics_provider(None)
    assert mp is not None
    assert mp.name == "claude"


def test_claude_provider_parse_session_delegates(tmp_path, monkeypatch):
    """ClaudeMetricsProvider.parse_session wraps find_conversation_by_id."""
    called = {}

    def fake_find(conv_id, cwd):
        called["conv_id"] = conv_id
        called["cwd"] = cwd
        return None

    monkeypatch.setattr(claude_metrics, "find_conversation_by_id", fake_find)
    mp = ClaudeMetricsProvider()
    result = mp.parse_session("abc-123", "/tmp/foo")
    assert result is None
    assert called == {"conv_id": "abc-123", "cwd": "/tmp/foo"}


def test_claude_provider_uses_default_cwd_when_none(monkeypatch):
    """Passing cwd=None should use DEFAULT_CWD, not pass None through."""
    captured = {}

    def fake_find(conv_id, cwd):
        captured["cwd"] = cwd
        return None

    monkeypatch.setattr(claude_metrics, "find_conversation_by_id", fake_find)
    mp = ClaudeMetricsProvider()
    mp.parse_session("abc-123", None)
    assert captured["cwd"] == claude_metrics.DEFAULT_CWD
