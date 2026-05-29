"""Claude JSONL parser: happy path, synthetic-skip, [1m] normalize, duration."""
from __future__ import annotations

import json

import pytest

from core.api.services import claude_metrics
from core.api.services.claude_metrics import (
    ClaudeMetricsProvider,
    find_conversation_by_id,
    parse_conversation,
)
from core.api.services.model_registry import normalize_model_id


def _jsonl_line(obj: dict) -> str:
    return json.dumps(obj) + "\n"


def _write_jsonl(path, messages: list[dict]) -> None:
    with open(path, "w") as f:
        for m in messages:
            f.write(_jsonl_line(m))


def _assistant(
    *,
    model: str = "claude-opus-4-7",
    inp: int = 10,
    out: int = 20,
    cr: int = 0,
    cw: int = 0,
    ts: str = "2026-04-22T10:00:00Z",
) -> dict:
    return {
        "type": "assistant",
        "timestamp": ts,
        "message": {
            "model": model,
            "usage": {
                "input_tokens": inp,
                "output_tokens": out,
                "cache_read_input_tokens": cr,
                "cache_creation_input_tokens": cw,
            },
        },
    }


def test_parse_conversation_happy_path(tmp_path):
    path = tmp_path / "11111111-2222-3333-4444-555555555555.jsonl"
    _write_jsonl(
        path,
        [
            _assistant(
                inp=100, out=50, cr=500, cw=1000, ts="2026-04-22T10:00:00Z"
            ),
            _assistant(
                inp=200, out=80, cr=2000, cw=0, ts="2026-04-22T10:05:00Z"
            ),
        ],
    )
    m = parse_conversation(path)
    assert m is not None
    assert m.conversation_id == "11111111-2222-3333-4444-555555555555"
    assert m.model == "claude-opus-4-7"
    assert m.input_tokens == 300
    assert m.output_tokens == 130
    assert m.cache_read_tokens == 2500
    assert m.cache_write_tokens == 1000
    assert m.message_count == 2
    # Last assistant context_tokens = inp+cr+cw = 200+2000+0 = 2200
    # ctx_window(opus) = 1M → pct = 0.22
    assert m.context_pct == pytest.approx(0.2, abs=0.05)
    # Cost Opus 4.7: in=$5, out=$25, cr=$0.5, cw=$6.25 per 1M
    # (300*5 + 130*25 + 2500*0.5 + 1000*6.25) / 1e6
    # = (1500 + 3250 + 1250 + 6250) / 1e6 = 12250/1e6 = 0.01225
    # round(0.01225, 4) in Python = 0.0123 (banker's rounding), tolerance loose
    assert m.cost_usd == pytest.approx(0.0123, abs=1e-4)
    assert m.duration_minutes == pytest.approx(5.0, abs=0.05)
    assert m.first_timestamp == "2026-04-22T10:00:00Z"
    assert m.last_timestamp == "2026-04-22T10:05:00Z"


def test_synthetic_messages_do_not_set_model(tmp_path):
    """Claude writes <synthetic> on some internal messages — skip for model attribution."""
    path = tmp_path / "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa.jsonl"
    _write_jsonl(
        path,
        [
            _assistant(model="claude-opus-4-7", inp=100, out=50, cr=0, cw=0),
            _assistant(model="<synthetic>", inp=5, out=5, cr=0, cw=0),
        ],
    )
    m = parse_conversation(path)
    assert m is not None
    # Model should stay at the real model, not be overwritten by <synthetic>
    assert m.model == "claude-opus-4-7"
    # But tokens from synthetic are still summed (current behavior; not in scope
    # to change in PR1 — just verify we don't silently skip everything)
    assert m.message_count == 2


def test_normalize_model_strips_1m_suffix():
    assert normalize_model_id("claude-opus-4-7[1m]") == "claude-opus-4-7"
    assert normalize_model_id("claude-opus-4-7") == "claude-opus-4-7"
    assert normalize_model_id(None) is None


def test_model_suffix_normalized_in_parsed_output(tmp_path):
    """JSONL may contain model with [1m] — parser should strip it."""
    path = tmp_path / "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb.jsonl"
    _write_jsonl(
        path, [_assistant(model="claude-opus-4-7[1m]", inp=100, out=10)]
    )
    m = parse_conversation(path)
    assert m is not None
    assert m.model == "claude-opus-4-7"


def test_last_context_tokens_disjoint_sum(tmp_path):
    """Anthropic docs: inp + cr + cw are DISJOINT — their sum is the total."""
    path = tmp_path / "cccccccc-cccc-cccc-cccc-cccccccccccc.jsonl"
    _write_jsonl(
        path,
        [
            _assistant(
                model="claude-haiku-4-5", inp=50, out=10, cr=1000, cw=500
            )
        ],
    )
    m = parse_conversation(path)
    assert m is not None
    # ctx denominator (haiku 200K): last_ctx_tokens = 50+1000+500 = 1550
    # pct = 1550 / 200_000 * 100 = 0.775 → rounded to 1 decimal = 0.8
    assert m.context_pct == pytest.approx(0.8, abs=0.05)


def test_duration_minutes_from_first_last(tmp_path):
    path = tmp_path / "dddddddd-dddd-dddd-dddd-dddddddddddd.jsonl"
    _write_jsonl(
        path,
        [
            _assistant(ts="2026-04-22T10:00:00Z"),
            _assistant(ts="2026-04-22T10:30:00Z"),
            _assistant(ts="2026-04-22T11:15:00Z"),
        ],
    )
    m = parse_conversation(path)
    assert m is not None
    # 75 min
    assert m.duration_minutes == pytest.approx(75.0, abs=0.1)


def test_empty_jsonl_returns_none(tmp_path):
    path = tmp_path / "eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee.jsonl"
    path.touch()
    assert parse_conversation(path) is None


def test_nonexistent_jsonl_returns_none(tmp_path):
    assert parse_conversation(tmp_path / "missing.jsonl") is None


# ---------------------------------------------------------------------------
# PR2: TTL-split cache, dual ctx, working_seconds_msg
# ---------------------------------------------------------------------------


def _assistant_pr2(
    *,
    model: str = "claude-opus-4-7",
    inp: int = 100,
    out: int = 50,
    cr: int = 0,
    cw5: int | None = None,
    cw1: int | None = None,
    cw_legacy: int | None = None,
    ts: str = "2026-04-22T10:00:00Z",
    synthetic: bool = False,
) -> dict:
    usage: dict = {
        "input_tokens": inp,
        "output_tokens": out,
        "cache_read_input_tokens": cr,
    }
    if cw5 is not None or cw1 is not None:
        usage["cache_creation_input_tokens"] = (cw5 or 0) + (cw1 or 0)
        usage["cache_creation"] = {
            "ephemeral_5m_input_tokens": cw5 or 0,
            "ephemeral_1h_input_tokens": cw1 or 0,
        }
    elif cw_legacy is not None:
        usage["cache_creation_input_tokens"] = cw_legacy
    return {
        "type": "assistant",
        "timestamp": ts,
        "message": {
            "model": "<synthetic>" if synthetic else model,
            "usage": usage,
        },
    }


def _user_msg(ts: str) -> dict:
    return {"type": "user", "timestamp": ts, "message": {"role": "user"}}


def test_ttl_split_populated_from_new_format(tmp_path):
    """When JSONL exposes ephemeral_5m/1h, parser splits cache_write."""
    path = tmp_path / "11111111-1111-1111-1111-111111111111.jsonl"
    _write_jsonl(
        path,
        [_assistant_pr2(cw5=1000, cw1=500)],
    )
    m = parse_conversation(path)
    assert m is not None
    assert m.cache_write_5m_tokens == 1000
    assert m.cache_write_1h_tokens == 500
    assert m.cache_write_tokens == 1500  # back-compat sum


def test_ttl_split_legacy_format_falls_back_to_5m(tmp_path):
    """Legacy JSONL with only cache_creation_input_tokens → all to 5m bucket."""
    path = tmp_path / "22222222-2222-2222-2222-222222222222.jsonl"
    _write_jsonl(
        path,
        [_assistant_pr2(cw_legacy=800)],
    )
    m = parse_conversation(path)
    assert m is not None
    assert m.cache_write_5m_tokens == 800
    assert m.cache_write_1h_tokens == 0


def test_cost_formula_uses_ttl_split_pricing(tmp_path):
    """Cost formula applies cw5 × cache_write_5m + cw1 × cache_write_1h rates."""
    # Opus pricing (kb/claude-pricing-2026-04-22.json):
    # input=5, output=25, cache_read=0.5, cache_write_5m=6.25, cache_write_1h=10.0
    path = tmp_path / "33333333-3333-3333-3333-333333333333.jsonl"
    _write_jsonl(
        path,
        [_assistant_pr2(inp=100, out=50, cr=200, cw5=1000, cw1=500)],
    )
    m = parse_conversation(path)
    assert m is not None
    # (100*5 + 50*25 + 200*0.5 + 1000*6.25 + 500*10) / 1e6
    # = (500 + 1250 + 100 + 6250 + 5000) / 1e6 = 13100 / 1e6 = 0.0131
    assert m.cost_usd == pytest.approx(0.0131, abs=1e-4)
    assert m.cost_conversation_usd == pytest.approx(0.0131, abs=1e-4)


def test_dual_context_pct_real_and_scaled(tmp_path):
    """context_pct_scaled = real * 100/84 (capped 100)."""
    path = tmp_path / "44444444-4444-4444-4444-444444444444.jsonl"
    # Opus ctx=1M, last_tokens = inp+cr+cw5+cw1 = 100+0+1000+500 = 1600 → 0.16%
    _write_jsonl(path, [_assistant_pr2(inp=100, cw5=1000, cw1=500)])
    m = parse_conversation(path)
    assert m is not None
    assert m.context_pct_real == pytest.approx(0.2, abs=0.05)
    # scaled = 0.16 * 100/84 ≈ 0.19
    assert m.context_pct_scaled == pytest.approx(0.2, abs=0.1)
    # Legacy alias mirrors real
    assert m.context_pct == m.context_pct_real


def test_context_pct_scaled_capped_at_100(tmp_path):
    """Even at >84% real, scaled must not exceed 100."""
    path = tmp_path / "55555555-5555-5555-5555-555555555555.jsonl"
    # Force huge token count to exceed 84% of opus 1M
    _write_jsonl(
        path,
        [_assistant_pr2(inp=900_000, cw5=0, cw1=0)],
    )
    m = parse_conversation(path)
    assert m is not None
    assert m.context_pct_scaled is not None
    assert m.context_pct_scaled <= 100.0


def test_working_seconds_msg_sums_user_to_assistant_gaps(tmp_path):
    """Each user msg pairs with the next non-synthetic assistant; negatives excluded."""
    path = tmp_path / "66666666-6666-6666-6666-666666666666.jsonl"
    _write_jsonl(
        path,
        [
            _user_msg("2026-04-22T10:00:00Z"),
            _assistant_pr2(ts="2026-04-22T10:00:10Z"),  # +10s
            _user_msg("2026-04-22T10:01:00Z"),
            _assistant_pr2(ts="2026-04-22T10:01:05Z", synthetic=True),  # skip
            _assistant_pr2(ts="2026-04-22T10:01:20Z"),  # +20s (vs user)
        ],
    )
    m = parse_conversation(path)
    assert m is not None
    # 10 + 20 = 30s
    assert m.working_seconds_msg == 30


def test_pricing_version_set(tmp_path):
    path = tmp_path / "77777777-7777-7777-7777-777777777777.jsonl"
    _write_jsonl(path, [_assistant_pr2()])
    m = parse_conversation(path)
    assert m is not None
    assert m.pricing_version == "2026-04-22"


def test_claude_provider_integration(tmp_path, monkeypatch):
    """ClaudeMetricsProvider.parse_session finds the right JSONL."""
    # Simulate the directory layout Claude Code uses
    conv_id = "ffffffff-ffff-ffff-ffff-ffffffffffff"
    fake_cwd = "/tmp/fake-workspace"
    encoded = fake_cwd.replace("/", "-")
    project_dir = tmp_path / "projects" / encoded
    project_dir.mkdir(parents=True)
    jsonl_path = project_dir / f"{conv_id}.jsonl"
    _write_jsonl(jsonl_path, [_assistant(inp=10, out=5)])

    monkeypatch.setattr(claude_metrics, "CLAUDE_PROJECTS_DIR", tmp_path / "projects")

    mp = ClaudeMetricsProvider()
    m = mp.parse_session(conv_id, fake_cwd)
    assert m is not None
    assert m.conversation_id == conv_id
    assert m.input_tokens == 10
