"""OpenCodeMetricsProvider: parser, security, concurrency."""
from __future__ import annotations

import json
import sqlite3

import pytest

from core.api.services import opencode_metrics
from core.api.services import model_registry
from core.api.services.opencode_metrics import (
    OPENCODE_SESSION_ID_RE,
    OpenCodeMetricsProvider,
)


# Shape mirroring real OpenCode DB (verified via
# `~/.local/share/opencode/opencode.db`).
_SESSION_COLS = (
    "id TEXT PRIMARY KEY, project_id TEXT NOT NULL, parent_id TEXT, "
    "slug TEXT NOT NULL, directory TEXT NOT NULL, title TEXT NOT NULL, "
    "version TEXT NOT NULL, share_url TEXT, summary_additions INTEGER, "
    "summary_deletions INTEGER, summary_files INTEGER, summary_diffs TEXT, "
    "revert TEXT, permission TEXT, time_created INTEGER NOT NULL, "
    "time_updated INTEGER NOT NULL, time_compacting INTEGER, "
    "time_archived INTEGER, workspace_id TEXT"
)
_MESSAGE_COLS = (
    "id TEXT PRIMARY KEY, session_id TEXT NOT NULL, "
    "time_created INTEGER NOT NULL, time_updated INTEGER NOT NULL, "
    "data TEXT NOT NULL"
)


def _enable_test_opencode_pricing(monkeypatch) -> None:
    """Use a tiny public test matrix; OSS never imports the private kb/ tree."""
    monkeypatch.setattr(
        model_registry,
        "_OPENCODE_PRICING_CACHE",
        {
            "version": "2026-04-23",
            "providers": {
                "openai": {
                    "gpt-5.4": {
                        "input": 1.25,
                        "output": 10.0,
                        "cache_read": 0.13,
                        "cache_write_5m": 1.56,
                        "cache_write_1h": 2.5,
                    }
                },
                "anthropic": {
                    "claude-opus-4-7": {
                        "input": 5.0,
                        "output": 25.0,
                        "cache_read": 0.5,
                        "cache_write_5m": 6.25,
                        "cache_write_1h": 10.0,
                    },
                    "claude-sonnet-4-5": {
                        "input": 3.0,
                        "output": 15.0,
                        "cache_read": 0.3,
                        "cache_write_5m": 3.75,
                        "cache_write_1h": 6.0,
                    },
                },
            },
        },
    )


def _seed_db(path) -> None:
    """Create a tiny DB with 1 session + mixed user/assistant messages."""
    conn = sqlite3.connect(path)
    conn.execute(f"CREATE TABLE session ({_SESSION_COLS})")
    conn.execute(f"CREATE TABLE message ({_MESSAGE_COLS})")

    conn.execute(
        "INSERT INTO session "
        "(id, project_id, slug, directory, title, version, time_created, time_updated) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "ses_test123",
            "global",
            "test",
            "/var/marvisx/workspace",
            "Test",
            "1.3.17",
            1_775_100_000_000,
            1_775_100_300_000,
        ),
    )

    # User message 1
    conn.execute(
        "INSERT INTO message (id, session_id, time_created, time_updated, data) VALUES (?, ?, ?, ?, ?)",
        (
            "msg_u1",
            "ses_test123",
            1_775_100_001_000,
            1_775_100_001_000,
            json.dumps({"role": "user"}),
        ),
    )
    # Assistant 1 — normal message, cost>0, finish=stop
    conn.execute(
        "INSERT INTO message (id, session_id, time_created, time_updated, data) VALUES (?, ?, ?, ?, ?)",
        (
            "msg_a1",
            "ses_test123",
            1_775_100_005_000,  # +4s gap from user
            1_775_100_005_000,
            json.dumps(
                {
                    "role": "assistant",
                    "cost": 0.025,
                    "modelID": "claude-sonnet-4-5",
                    "providerID": "anthropic",
                    "finish": "stop",
                    "tokens": {
                        "total": 5000,
                        "input": 100,
                        "output": 200,
                        "reasoning": 0,
                        "cache": {"read": 3700, "write": 1000},
                    },
                    "time": {"created": 1_775_100_001_500, "completed": 1_775_100_005_000},
                }
            ),
        ),
    )
    # User 2
    conn.execute(
        "INSERT INTO message (id, session_id, time_created, time_updated, data) VALUES (?, ?, ?, ?, ?)",
        (
            "msg_u2",
            "ses_test123",
            1_775_100_010_000,
            1_775_100_010_000,
            json.dumps({"role": "user"}),
        ),
    )
    # Assistant 2 — error finish (exclude from working_seconds, but cost=0 so ok)
    conn.execute(
        "INSERT INTO message (id, session_id, time_created, time_updated, data) VALUES (?, ?, ?, ?, ?)",
        (
            "msg_a2",
            "ses_test123",
            1_775_100_012_000,
            1_775_100_012_000,
            json.dumps(
                {
                    "role": "assistant",
                    "cost": 0.0,
                    "modelID": "claude-sonnet-4-5",
                    "providerID": "anthropic",
                    "finish": "error",
                    "tokens": {
                        "total": 500,
                        "input": 50,
                        "output": 0,
                        "reasoning": 0,
                        "cache": {"read": 450, "write": 0},
                    },
                }
            ),
        ),
    )
    # Assistant 3 — final normal message, cost>0
    conn.execute(
        "INSERT INTO message (id, session_id, time_created, time_updated, data) VALUES (?, ?, ?, ?, ?)",
        (
            "msg_a3",
            "ses_test123",
            1_775_100_020_000,  # +10s gap from user 2
            1_775_100_020_000,
            json.dumps(
                {
                    "role": "assistant",
                    "cost": 0.015,
                    "modelID": "claude-sonnet-4-5",
                    "providerID": "anthropic",
                    "finish": "stop",
                    "tokens": {
                        "total": 8000,
                        "input": 150,
                        "output": 350,
                        "reasoning": 0,
                        "cache": {"read": 6500, "write": 1000},
                    },
                }
            ),
        ),
    )
    conn.commit()
    conn.close()


@pytest.fixture
def seeded_db(tmp_path, monkeypatch):
    db_path = tmp_path / "opencode.db"
    _seed_db(db_path)
    monkeypatch.setattr(opencode_metrics, "OPENCODE_DB_PATH", db_path)
    return db_path


def test_parse_session_happy_path(seeded_db):
    mp = OpenCodeMetricsProvider()
    m = mp.parse_session("ses_test123")
    assert m is not None
    assert m.conversation_id == "ses_test123"
    assert m.model == "claude-sonnet-4-5"
    # Cost excludes error message (cost=0 AND finish!=stop). 0.025 + 0.015 = 0.04
    assert m.cost_usd == pytest.approx(0.04)
    # 3 assistant messages total (including error)
    assert m.message_count == 3
    # Aggregates
    assert m.input_tokens == 100 + 50 + 150
    assert m.output_tokens == 200 + 0 + 350
    assert m.cache_read_tokens == 3700 + 450 + 6500
    assert m.cache_write_tokens == 1000 + 0 + 1000
    # Last message context denominator = max(total=8000, sum=150+350+0+6500+1000=8000)
    # Both equal here; ctx% = 8000 / 200_000 * 100 = 4.0
    assert m.context_pct == pytest.approx(4.0, abs=0.1)
    # Duration = (1775100020000 - 1775100005000) / 1000 / 60 = 0.25 min
    # (rounded to 1 decimal, banker's rounding → 0.2)
    assert m.duration_minutes == pytest.approx(0.2, abs=0.05)


def test_session_id_regex_rejects_traversal(tmp_path, monkeypatch):
    """Regex gate blocks path-traversal / injection before any DB work."""
    # Point to a real DB so we know it's the regex (not missing DB) rejecting.
    db_path = tmp_path / "opencode.db"
    _seed_db(db_path)
    monkeypatch.setattr(opencode_metrics, "OPENCODE_DB_PATH", db_path)

    mp = OpenCodeMetricsProvider()
    assert mp.parse_session("../evil") is None
    assert mp.parse_session("ses_test123'; DROP TABLE session;--") is None
    assert mp.parse_session("") is None
    assert mp.parse_session("ses_") is None  # trailing nothing after _


def test_session_id_regex_positive():
    """The pattern we check matches the real OpenCode format."""
    assert OPENCODE_SESSION_ID_RE.match("ses_test123")
    assert OPENCODE_SESSION_ID_RE.match("ses_aAbB1Z9X")
    assert not OPENCODE_SESSION_ID_RE.match("ses_hello-world")  # no hyphens
    assert not OPENCODE_SESSION_ID_RE.match("xxxx_test")


def test_tool_calls_finish_included_in_cost(tmp_path, monkeypatch):
    _enable_test_opencode_pricing(monkeypatch)
    """finish='tool-calls' is legitimate (LLM emitted a tool call — real tokens
    billed). Must be INCLUDED in both real and equivalent cost aggregation.
    Only finish in ('error', None) are genuine failures to skip."""
    db_path = tmp_path / "opencode.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE session (id TEXT PRIMARY KEY);
        CREATE TABLE message (
            id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL,
            time_created INTEGER NOT NULL,
            time_updated INTEGER NOT NULL,
            data TEXT NOT NULL
        );
        """
    )
    conn.execute("INSERT INTO session (id) VALUES ('ses_toolcalls')")
    # User message
    conn.execute(
        "INSERT INTO message VALUES (?, ?, ?, ?, ?)",
        ("u1", "ses_toolcalls", 1_775_200_000_000, 1_775_200_000_000,
         json.dumps({"role": "user"})),
    )
    # Assistant with finish='tool-calls' — must count
    conn.execute(
        "INSERT INTO message VALUES (?, ?, ?, ?, ?)",
        ("a1", "ses_toolcalls", 1_775_200_002_000, 1_775_200_002_000,
         json.dumps({
             "role": "assistant",
             "cost": 0.005,
             "modelID": "claude-opus-4-7",
             "providerID": "anthropic",
             "finish": "tool-calls",
             "tokens": {"total": 1500, "input": 1000, "output": 500,
                        "reasoning": 0, "cache": {"read": 0, "write": 0}},
             "time": {"created": 1_775_200_001_000, "completed": 1_775_200_002_000},
         })),
    )
    # Assistant with finish='error' — must NOT count
    conn.execute(
        "INSERT INTO message VALUES (?, ?, ?, ?, ?)",
        ("a2", "ses_toolcalls", 1_775_200_003_000, 1_775_200_003_000,
         json.dumps({
             "role": "assistant",
             "cost": 0.0,
             "modelID": "claude-opus-4-7",
             "providerID": "anthropic",
             "finish": "error",
             "tokens": {"total": 100, "input": 100, "output": 0,
                        "reasoning": 0, "cache": {"read": 0, "write": 0}},
         })),
    )
    # Assistant with finish='length' — must count
    conn.execute(
        "INSERT INTO message VALUES (?, ?, ?, ?, ?)",
        ("a3", "ses_toolcalls", 1_775_200_004_000, 1_775_200_004_000,
         json.dumps({
             "role": "assistant",
             "cost": 0.010,
             "modelID": "claude-opus-4-7",
             "providerID": "anthropic",
             "finish": "length",
             "tokens": {"total": 2000, "input": 1500, "output": 500,
                        "reasoning": 0, "cache": {"read": 0, "write": 0}},
         })),
    )
    conn.commit()
    conn.close()
    monkeypatch.setattr(opencode_metrics, "OPENCODE_DB_PATH", db_path)

    mp = OpenCodeMetricsProvider()
    m = mp.parse_session("ses_toolcalls")
    assert m is not None
    # Real cost: tool-calls ($0.005) + length ($0.010) = $0.015, error excluded
    assert m.cost_usd == pytest.approx(0.015)
    # Tokens: tool-calls (1000/500) + error (100/0) + length (1500/500) — all accumulated
    # (token totals include error for transparency; only cost sum gates error)
    assert m.input_tokens == 1000 + 100 + 1500
    assert m.output_tokens == 500 + 0 + 500
    # Shadow: Opus 4.7 = $5/M input, $25/M output
    # tool-calls: (1000*5 + 500*25) = 17500 / 1e6 = $0.0175
    # length:    (1500*5 + 500*25) = 20000 / 1e6 = $0.020
    # error excluded
    # total shadow: $0.0375
    assert m.cost_conversation_equivalent_usd == pytest.approx(0.0375, abs=0.001)


def test_missing_db_returns_none(tmp_path, monkeypatch):
    """No DB file → quietly return None (OpenCode not installed)."""
    monkeypatch.setattr(opencode_metrics, "OPENCODE_DB_PATH", tmp_path / "nope.db")
    mp = OpenCodeMetricsProvider()
    assert mp.parse_session("ses_abc123") is None


def test_empty_session_returns_none(tmp_path, monkeypatch):
    """Session with no assistant messages → None."""
    db_path = tmp_path / "opencode.db"
    conn = sqlite3.connect(db_path)
    conn.execute(f"CREATE TABLE session ({_SESSION_COLS})")
    conn.execute(f"CREATE TABLE message ({_MESSAGE_COLS})")
    conn.execute(
        "INSERT INTO session (id, project_id, slug, directory, title, version, time_created, time_updated) "
        "VALUES ('ses_empty', 'global', 'x', '/tmp', 't', '1.0', 1, 1)"
    )
    conn.commit()
    conn.close()
    monkeypatch.setattr(opencode_metrics, "OPENCODE_DB_PATH", db_path)

    mp = OpenCodeMetricsProvider()
    assert mp.parse_session("ses_empty") is None


def test_db_locked_returns_none(seeded_db, monkeypatch):
    """When sqlite3.OperationalError mentions 'locked' → return None, no crash."""
    import sqlite3 as _sqlite3

    original_connect = _sqlite3.connect

    def fake_connect(*args, **kwargs):
        raise _sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(opencode_metrics.sqlite3, "connect", fake_connect)
    mp = OpenCodeMetricsProvider()
    assert mp.parse_session("ses_test123") is None


def test_other_operational_error_raises(seeded_db, monkeypatch):
    """Non-locking SQL errors should propagate — they indicate a real bug."""
    import sqlite3 as _sqlite3

    def fake_connect(*args, **kwargs):
        raise _sqlite3.OperationalError("no such table: message")

    monkeypatch.setattr(opencode_metrics.sqlite3, "connect", fake_connect)
    mp = OpenCodeMetricsProvider()
    with pytest.raises(_sqlite3.OperationalError):
        mp.parse_session("ses_test123")


def test_token_denominator_uses_max(tmp_path, monkeypatch):
    """G1: ctx uses max(tokens.total, sum of parts) — covers models where
    total excludes reasoning.
    """
    db_path = tmp_path / "opencode.db"
    conn = sqlite3.connect(db_path)
    conn.execute(f"CREATE TABLE session ({_SESSION_COLS})")
    conn.execute(f"CREATE TABLE message ({_MESSAGE_COLS})")
    conn.execute(
        "INSERT INTO session (id, project_id, slug, directory, title, version, time_created, time_updated) "
        "VALUES ('ses_g1', 'global', 'x', '/tmp', 't', '1.0', 1, 1)"
    )
    # total=1000 but parts sum to 5000 (reasoning excluded from total by provider)
    conn.execute(
        "INSERT INTO message (id, session_id, time_created, time_updated, data) VALUES (?, ?, ?, ?, ?)",
        (
            "m1",
            "ses_g1",
            1000,
            1000,
            json.dumps(
                {
                    "role": "assistant",
                    "cost": 0.01,
                    "modelID": "claude-haiku-4-5",
                    "finish": "stop",
                    "tokens": {
                        "total": 1000,
                        "input": 500,
                        "output": 500,
                        "reasoning": 3000,  # excluded from total by provider
                        "cache": {"read": 500, "write": 500},
                    },
                }
            ),
        ),
    )
    conn.commit()
    conn.close()
    monkeypatch.setattr(opencode_metrics, "OPENCODE_DB_PATH", db_path)

    mp = OpenCodeMetricsProvider()
    m = mp.parse_session("ses_g1")
    assert m is not None
    # ctx_tokens = max(1000, 500+500+3000+500+500) = 5000
    # ctx_window for haiku = 200_000 → pct = 2.5
    assert m.context_pct == pytest.approx(2.5, abs=0.1)


def test_unknown_model_uses_fallback_context_window(tmp_path, monkeypatch):
    db_path = tmp_path / "opencode.db"
    conn = sqlite3.connect(db_path)
    conn.execute(f"CREATE TABLE session ({_SESSION_COLS})")
    conn.execute(f"CREATE TABLE message ({_MESSAGE_COLS})")
    conn.execute(
        "INSERT INTO session (id, project_id, slug, directory, title, version, time_created, time_updated) "
        "VALUES ('ses_unk', 'global', 'x', '/tmp', 't', '1.0', 1, 1)"
    )
    conn.execute(
        "INSERT INTO message (id, session_id, time_created, time_updated, data) VALUES (?, ?, ?, ?, ?)",
        (
            "m1",
            "ses_unk",
            1000,
            1000,
            json.dumps(
                {
                    "role": "assistant",
                    "cost": 0.01,
                    "modelID": "some-unknown-model",
                    "finish": "stop",
                    "tokens": {
                        "total": 20000,
                        "input": 10000,
                        "output": 5000,
                        "reasoning": 0,
                        "cache": {"read": 5000, "write": 0},
                    },
                }
            ),
        ),
    )
    conn.commit()
    conn.close()
    monkeypatch.setattr(opencode_metrics, "OPENCODE_DB_PATH", db_path)

    mp = OpenCodeMetricsProvider()
    m = mp.parse_session("ses_unk")
    assert m is not None
    # Unknown model → 200K fallback; ctx = 20000 / 200000 = 10.0
    assert m.context_pct == pytest.approx(10.0, abs=0.1)


def test_get_last_context_pct_wraps_parse_session(seeded_db):
    mp = OpenCodeMetricsProvider()
    pct = mp.get_last_context_pct("ses_test123")
    assert pct is not None
    assert pct == pytest.approx(4.0, abs=0.1)


def test_get_last_context_pct_invalid_id_returns_none(seeded_db):
    mp = OpenCodeMetricsProvider()
    assert mp.get_last_context_pct("../evil") is None


# ---------------------------------------------------------------------------
# PR2: new fields coverage (reasoning_tokens, dual ctx, working_seconds_msg)
# ---------------------------------------------------------------------------


def test_reasoning_tokens_populated(tmp_path, monkeypatch):
    """tokens.reasoning flows into SessionMetrics.reasoning_tokens."""
    db_path = tmp_path / "opencode.db"
    conn = sqlite3.connect(db_path)
    conn.execute(f"CREATE TABLE session ({_SESSION_COLS})")
    conn.execute(f"CREATE TABLE message ({_MESSAGE_COLS})")
    conn.execute(
        "INSERT INTO session (id, project_id, slug, directory, title, version, time_created, time_updated) "
        "VALUES ('ses_reason', 'g', 'x', '/tmp', 't', '1.0', 1, 1)"
    )
    conn.execute(
        "INSERT INTO message (id, session_id, time_created, time_updated, data) VALUES (?, ?, ?, ?, ?)",
        (
            "m1",
            "ses_reason",
            1000,
            1000,
            json.dumps(
                {
                    "role": "assistant",
                    "cost": 0.01,
                    "modelID": "claude-sonnet-4-5",
                    "finish": "stop",
                    "tokens": {
                        "total": 1000,
                        "input": 300,
                        "output": 200,
                        "reasoning": 500,
                        "cache": {"read": 0, "write": 0},
                    },
                }
            ),
        ),
    )
    conn.commit()
    conn.close()
    monkeypatch.setattr(opencode_metrics, "OPENCODE_DB_PATH", db_path)

    mp = OpenCodeMetricsProvider()
    m = mp.parse_session("ses_reason")
    assert m is not None
    assert m.reasoning_tokens == 500


def test_context_pct_real_set_scaled_none(seeded_db):
    """OpenCode exposes context_pct_real, but scaled is always None
    (84% fudge is Claude-specific)."""
    mp = OpenCodeMetricsProvider()
    m = mp.parse_session("ses_test123")
    assert m is not None
    assert m.context_pct_real == pytest.approx(4.0, abs=0.1)
    assert m.context_pct_scaled is None


def test_cost_conversation_and_session_equal_for_single_chain(seeded_db):
    """For PR2, OpenCode sessions are single-conv → cost_session == cost_conversation."""
    mp = OpenCodeMetricsProvider()
    m = mp.parse_session("ses_test123")
    assert m is not None
    assert m.cost_conversation_usd == pytest.approx(0.04, abs=0.001)
    assert m.cost_session_usd == pytest.approx(0.04, abs=0.001)
    assert m.cost_session_incomplete is False


def test_cache_write_mapped_to_1h_bucket(seeded_db):
    """OpenCode lacks TTL split → all cache_write maps to cache_write_1h_tokens."""
    mp = OpenCodeMetricsProvider()
    m = mp.parse_session("ses_test123")
    assert m is not None
    # From fixture: total cache_write = 1000 + 0 + 1000 = 2000
    assert m.cache_write_5m_tokens == 0
    assert m.cache_write_1h_tokens == 2000
    # Legacy back-compat alias preserved
    assert m.cache_write_tokens == 2000


def test_working_seconds_msg_populated(seeded_db):
    """Sums (assistant.time_created - user.time_created) for non-error pairs.

    Implementation detail: each user consumes the *next* assistant slot even
    when that assistant is excluded (error/None finish). user2 pairs with
    the error assistant (skipped), and asst3 is left unpaired. So the total
    comes from user1→asst1 only = 4s.
    """
    mp = OpenCodeMetricsProvider()
    m = mp.parse_session("ses_test123")
    assert m is not None
    assert m.working_seconds_msg == 4


def test_pricing_version_set(seeded_db):
    mp = OpenCodeMetricsProvider()
    m = mp.parse_session("ses_test123")
    assert m is not None
    assert m.pricing_version == "2026-04-22"


# --------------------------------------------------------------------------
# PR4 shadow cost (cost_equivalent_usd)
# --------------------------------------------------------------------------


def test_cost_equivalent_populated_when_provider_model_known(
    seeded_db, monkeypatch
):
    """Anthropic/claude-sonnet-4-5 is in kb/opencode-pricing → equivalent > 0.

    Using the real kb/opencode-pricing-2026-04-23.json pricing:
      sonnet-4-5: input=$3.00/M, output=$15.00/M, cache_read=$0.30/M, cache_write_1h=$6.00/M
    seed has:
      msg_a1: input=100, output=200, cache.read=3700, cache.write=1000  (cost>0, stop)
      msg_a2: skipped (cost=0, finish=error)
      msg_a3: input=150, output=350, cache.read=6500, cache.write=1000  (cost>0, stop)
    equivalent = ((100+150)*3 + (200+350)*15 + (3700+6500)*0.30 + (1000+1000)*6) / 1M
             = (750 + 8250 + 3060 + 12000) / 1_000_000
             = 24060 / 1_000_000
             = 0.02406
    """
    _enable_test_opencode_pricing(monkeypatch)
    mp = OpenCodeMetricsProvider()
    m = mp.parse_session("ses_test123")
    assert m is not None
    assert m.cost_conversation_equivalent_usd is not None
    assert m.cost_conversation_equivalent_usd == pytest.approx(0.02406, abs=1e-5)
    # Version tag populated — matches kb/opencode-pricing-2026-04-23.json
    assert m.cost_equivalent_pricing_version == "2026-04-23"


def test_cost_equivalent_none_when_providerID_missing(tmp_path, monkeypatch):
    """OpenCode rows without `providerID` → skip (fallback_strategy=skip)."""
    import json

    db_path = tmp_path / "opencode.db"
    conn = sqlite3.connect(db_path)
    conn.execute(f"CREATE TABLE session ({_SESSION_COLS})")
    conn.execute(f"CREATE TABLE message ({_MESSAGE_COLS})")
    conn.execute(
        "INSERT INTO session (id, project_id, slug, directory, title, version, time_created, time_updated) "
        "VALUES ('ses_noprov', 'global', 'x', '/tmp', 't', '1.0', 1, 1)"
    )
    conn.execute(
        "INSERT INTO message (id, session_id, time_created, time_updated, data) VALUES (?, ?, ?, ?, ?)",
        (
            "m1",
            "ses_noprov",
            1000,
            1000,
            json.dumps(
                {
                    "role": "assistant",
                    "cost": 0.0,
                    "modelID": "claude-sonnet-4-5",
                    # NOTE: no providerID field (legacy row)
                    "finish": "stop",
                    "tokens": {
                        "total": 1000,
                        "input": 500,
                        "output": 500,
                        "reasoning": 0,
                        "cache": {"read": 0, "write": 0},
                    },
                }
            ),
        ),
    )
    conn.commit()
    conn.close()
    monkeypatch.setattr(opencode_metrics, "OPENCODE_DB_PATH", db_path)

    mp = OpenCodeMetricsProvider()
    m = mp.parse_session("ses_noprov")
    assert m is not None
    assert m.cost_conversation_equivalent_usd is None
    assert m.cost_equivalent_pricing_version is None


def test_cost_equivalent_none_when_model_unknown(tmp_path, monkeypatch):
    """Provider/model pair missing in kb/opencode-pricing → skip."""
    import json

    db_path = tmp_path / "opencode.db"
    conn = sqlite3.connect(db_path)
    conn.execute(f"CREATE TABLE session ({_SESSION_COLS})")
    conn.execute(f"CREATE TABLE message ({_MESSAGE_COLS})")
    conn.execute(
        "INSERT INTO session (id, project_id, slug, directory, title, version, time_created, time_updated) "
        "VALUES ('ses_unk', 'global', 'x', '/tmp', 't', '1.0', 1, 1)"
    )
    conn.execute(
        "INSERT INTO message (id, session_id, time_created, time_updated, data) VALUES (?, ?, ?, ?, ?)",
        (
            "m1",
            "ses_unk",
            1000,
            1000,
            json.dumps(
                {
                    "role": "assistant",
                    "cost": 0.0,
                    "providerID": "mystery-cloud",
                    "modelID": "mystery-model-xl",
                    "finish": "stop",
                    "tokens": {
                        "total": 1000,
                        "input": 500,
                        "output": 500,
                        "reasoning": 0,
                        "cache": {"read": 0, "write": 0},
                    },
                }
            ),
        ),
    )
    conn.commit()
    conn.close()
    monkeypatch.setattr(opencode_metrics, "OPENCODE_DB_PATH", db_path)

    mp = OpenCodeMetricsProvider()
    m = mp.parse_session("ses_unk")
    assert m is not None
    # Unknown pair → fallback strategy=skip → None (NOT zero, NOT guessed)
    assert m.cost_conversation_equivalent_usd is None
    assert m.cost_equivalent_pricing_version is None


def test_cost_equivalent_shadow_for_oauth_session(tmp_path, monkeypatch):
    _enable_test_opencode_pricing(monkeypatch)
    """OAuth session: cost=0 but token volume → equivalent > 0 (shadow value)."""
    import json

    db_path = tmp_path / "opencode.db"
    conn = sqlite3.connect(db_path)
    conn.execute(f"CREATE TABLE session ({_SESSION_COLS})")
    conn.execute(f"CREATE TABLE message ({_MESSAGE_COLS})")
    conn.execute(
        "INSERT INTO session (id, project_id, slug, directory, title, version, time_created, time_updated) "
        "VALUES ('ses_oauth1', 'global', 'x', '/tmp', 't', '1.0', 1, 1)"
    )
    # OpenAI gpt-5.4 OAuth: real cost always 0 but token volume real.
    # Pricing: input=$1.25/M, output=$10.00/M, cache_read=$0.13/M, cache_write_1h=$2.50/M
    # Using 1M input + 100k output + 500k cache_read + 10k cache_write
    #   equivalent = (1_000_000*1.25 + 100_000*10 + 500_000*0.13 + 10_000*2.50) / 1M
    #              = (1_250_000 + 1_000_000 + 65_000 + 25_000) / 1_000_000
    #              = 2_340_000 / 1_000_000
    #              = 2.34
    conn.execute(
        "INSERT INTO message (id, session_id, time_created, time_updated, data) VALUES (?, ?, ?, ?, ?)",
        (
            "m1",
            "ses_oauth1",
            1000,
            1000,
            json.dumps(
                {
                    "role": "assistant",
                    "cost": 0.0,  # OAuth — real cost always zero
                    "providerID": "openai",
                    "modelID": "gpt-5.4",
                    "finish": "stop",
                    "tokens": {
                        "total": 1_610_000,
                        "input": 1_000_000,
                        "output": 100_000,
                        "reasoning": 0,
                        "cache": {"read": 500_000, "write": 10_000},
                    },
                }
            ),
        ),
    )
    conn.commit()
    conn.close()
    monkeypatch.setattr(opencode_metrics, "OPENCODE_DB_PATH", db_path)

    mp = OpenCodeMetricsProvider()
    m = mp.parse_session("ses_oauth1")
    assert m is not None
    assert m.cost_conversation_usd == 0.0  # OAuth → real is zero
    assert m.cost_conversation_equivalent_usd == pytest.approx(2.34, abs=0.01)
    assert m.cost_equivalent_pricing_version == "2026-04-23"
