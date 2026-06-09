"""SessionMetricsService: circuit breaker + dispatch + memoization."""
from __future__ import annotations

import asyncio
import time

import pytest

from core.api.services import metrics_providers as mp_module
from core.api.services.claude_metrics import SessionMetrics
from core.api.services.session_metrics_service import (
    SessionMetricsService,
    parse_conversation_cost_memo,
)


def _fake_metrics(conv_id: str = "abc", cost: float = 1.23) -> SessionMetrics:
    return SessionMetrics(
        conversation_id=conv_id,
        model="claude-opus-4-7",
        context_pct=5.0,
        cost_usd=cost,
        message_count=1,
    )


@pytest.mark.asyncio
async def test_refresh_dispatches_to_claude_provider(monkeypatch):
    """Happy path: Claude row routes to ClaudeMetricsProvider via to_thread."""
    captured = {}

    class FakeClaude:
        name = "claude"

        def parse_session(self, conv_id, cwd=None):
            captured["conv_id"] = conv_id
            captured["cwd"] = cwd
            return _fake_metrics(conv_id)

        def get_last_context_pct(self, conv_id, cwd=None):
            return None

    monkeypatch.setitem(mp_module.METRICS_PROVIDERS, "claude", FakeClaude())
    svc = SessionMetricsService()
    row = {
        "name": "sess1",
        "provider": "claude",
        "conversation_id": "11111111-2222-3333-4444-555555555555",
    }
    result = await svc.refresh(row)
    assert result is not None
    assert result.conversation_id == "11111111-2222-3333-4444-555555555555"
    assert captured["conv_id"] == "11111111-2222-3333-4444-555555555555"


@pytest.mark.asyncio
async def test_refresh_dispatches_to_opencode_provider(monkeypatch):
    captured = {}

    class FakeOpenCode:
        name = "opencode"

        def parse_session(self, conv_id, cwd=None):
            captured["conv_id"] = conv_id
            return _fake_metrics(conv_id, cost=0.1)

        def get_last_context_pct(self, conv_id, cwd=None):
            return None

    monkeypatch.setitem(mp_module.METRICS_PROVIDERS, "opencode", FakeOpenCode())
    svc = SessionMetricsService()
    row = {
        "name": "sess2",
        "provider": "opencode",
        "conversation_id": "ses_abc123",
    }
    result = await svc.refresh(row)
    assert result is not None
    assert captured["conv_id"] == "ses_abc123"


@pytest.mark.asyncio
async def test_invalid_claude_conv_id_returns_none(monkeypatch):
    """Non-UUID conv_id for Claude skips dispatch."""

    class ShouldNotRun:
        name = "claude"

        def parse_session(self, *a, **kw):
            raise AssertionError("should not have been called")

        def get_last_context_pct(self, *a, **kw):
            raise AssertionError("should not have been called")

    monkeypatch.setitem(mp_module.METRICS_PROVIDERS, "claude", ShouldNotRun())
    svc = SessionMetricsService()
    result = await svc.refresh(
        {"name": "sess", "provider": "claude", "conversation_id": "not-a-uuid"}
    )
    assert result is None


@pytest.mark.asyncio
async def test_invalid_opencode_conv_id_returns_none(monkeypatch):
    class ShouldNotRun:
        name = "opencode"

        def parse_session(self, *a, **kw):
            raise AssertionError

        def get_last_context_pct(self, *a, **kw):
            raise AssertionError

    monkeypatch.setitem(mp_module.METRICS_PROVIDERS, "opencode", ShouldNotRun())
    svc = SessionMetricsService()
    result = await svc.refresh(
        {"name": "sess", "provider": "opencode", "conversation_id": "wrong_fmt"}
    )
    assert result is None


@pytest.mark.asyncio
async def test_missing_conversation_id_returns_none():
    svc = SessionMetricsService()
    result = await svc.refresh(
        {"name": "sess", "provider": "claude", "conversation_id": None}
    )
    assert result is None


@pytest.mark.asyncio
async def test_unknown_provider_returns_none():
    svc = SessionMetricsService()
    result = await svc.refresh(
        {
            "name": "sess",
            "provider": "gpt",
            "conversation_id": "whatever",
        }
    )
    assert result is None


@pytest.mark.asyncio
async def test_circuit_breaker_opens_after_three_failures(monkeypatch):
    call_count = {"n": 0}

    class FailingProvider:
        name = "claude"

        def parse_session(self, conv_id, cwd=None):
            call_count["n"] += 1
            raise RuntimeError("boom")

        def get_last_context_pct(self, conv_id, cwd=None):
            return None

    monkeypatch.setitem(mp_module.METRICS_PROVIDERS, "claude", FailingProvider())
    svc = SessionMetricsService()
    row = {
        "name": "flaky",
        "provider": "claude",
        "conversation_id": "11111111-2222-3333-4444-555555555555",
    }

    for i in range(3):
        with pytest.raises(RuntimeError):
            await svc.refresh(row)

    # Breaker now open — next call short-circuits, does NOT invoke provider.
    calls_before = call_count["n"]
    result = await svc.refresh(row)
    assert result is None
    assert call_count["n"] == calls_before  # provider not called


@pytest.mark.asyncio
async def test_breaker_resets_on_success(monkeypatch):
    flaky = {"fail": True}

    class Provider:
        name = "claude"

        def parse_session(self, conv_id, cwd=None):
            if flaky["fail"]:
                raise RuntimeError("x")
            return _fake_metrics(conv_id)

        def get_last_context_pct(self, conv_id, cwd=None):
            return None

    monkeypatch.setitem(mp_module.METRICS_PROVIDERS, "claude", Provider())
    svc = SessionMetricsService()
    row = {
        "name": "sess",
        "provider": "claude",
        "conversation_id": "11111111-2222-3333-4444-555555555555",
    }

    # Fail twice (not yet at threshold)
    for _ in range(2):
        with pytest.raises(RuntimeError):
            await svc.refresh(row)

    # Now succeed — counter resets
    flaky["fail"] = False
    m = await svc.refresh(row)
    assert m is not None

    # Failing once more shouldn't immediately open the breaker
    flaky["fail"] = True
    with pytest.raises(RuntimeError):
        await svc.refresh(row)
    # Counter is at 1, breaker NOT open
    assert svc._skip_until.get("sess", 0) <= time.time()


def test_parse_conversation_cost_memo_returns_none_for_unknown_provider():
    """Unknown provider short-circuits without raising."""
    parse_conversation_cost_memo.cache_clear()
    result = parse_conversation_cost_memo("id", 1.0, 100, "unknown")
    assert result is None


def test_parse_conversation_cost_memo_caches_by_key(monkeypatch):
    """Calling twice with same key only hits provider once."""
    parse_conversation_cost_memo.cache_clear()
    calls = {"n": 0}

    class Provider:
        name = "claude"

        def parse_session(self, conv_id, cwd=None):
            calls["n"] += 1
            return _fake_metrics(conv_id, cost=9.99)

        def get_last_context_pct(self, conv_id, cwd=None):
            return None

    monkeypatch.setitem(mp_module.METRICS_PROVIDERS, "claude", Provider())

    a = parse_conversation_cost_memo("c1", 100.0, 1024, "claude")
    b = parse_conversation_cost_memo("c1", 100.0, 1024, "claude")
    assert a == 9.99
    assert b == 9.99
    assert calls["n"] == 1  # cached on 2nd call


def test_parse_conversation_cost_memo_invalidates_on_mtime_change(monkeypatch):
    parse_conversation_cost_memo.cache_clear()
    calls = {"n": 0}
    costs = [1.0, 2.0]

    class Provider:
        name = "claude"

        def parse_session(self, conv_id, cwd=None):
            calls["n"] += 1
            return _fake_metrics(conv_id, cost=costs[calls["n"] - 1])

        def get_last_context_pct(self, conv_id, cwd=None):
            return None

    monkeypatch.setitem(mp_module.METRICS_PROVIDERS, "claude", Provider())

    a = parse_conversation_cost_memo("c2", 100.0, 1024, "claude")
    b = parse_conversation_cost_memo("c2", 200.0, 1024, "claude")  # mtime changed
    assert a == 1.0
    assert b == 2.0
    assert calls["n"] == 2
