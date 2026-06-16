"""Codex JSONL metrics provider."""
from __future__ import annotations

import json

import pytest

from core.api.services import codex_metrics
from core.api.services.codex_metrics import (
    CodexMetricsProvider,
    detect_codex_for_process,
    detect_codex_for_session,
    parse_session_file,
)


SESSION_ID = "019df34d-c458-7ab0-8706-4c77077d843b"


def _write_jsonl(path, events: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for event in events:
            f.write(json.dumps(event) + "\n")


def _codex_events(
    *,
    session_id: str = SESSION_ID,
    started_at: str = "2026-05-04T14:04:13.388Z",
) -> list[dict]:
    return [
        {
            "timestamp": "2026-05-04T14:04:13.500Z",
            "type": "session_meta",
            "payload": {
                "id": session_id,
                "timestamp": started_at,
                "cwd": "/var/marvisx/workspace",
                "model_provider": "openai",
            },
        },
        {
            "timestamp": "2026-05-04T14:04:14.000Z",
            "type": "turn_context",
            "payload": {"model": "gpt-5.5"},
        },
        {
            "timestamp": "2026-05-04T14:04:15.000Z",
            "type": "event_msg",
            "payload": {"type": "user_message", "message": "do work"},
        },
        {
            "timestamp": "2026-05-04T14:04:16.000Z",
            "type": "event_msg",
            "payload": {"type": "agent_message", "message": "working"},
        },
        {
            "timestamp": "2026-05-04T14:04:17.000Z",
            "type": "event_msg",
            "payload": {
                "type": "token_count",
                "info": None,
            },
        },
        {
            "timestamp": "2026-05-04T14:05:13.000Z",
            "type": "event_msg",
            "payload": {
                "type": "token_count",
                "info": {
                    "model_context_window": 258400,
                    "last_token_usage": {
                        "input_tokens": 100000,
                        "cached_input_tokens": 90000,
                        "output_tokens": 1000,
                        "reasoning_output_tokens": 500,
                        "total_tokens": 101000,
                    },
                    "total_token_usage": {
                        "input_tokens": 150000,
                        "cached_input_tokens": 120000,
                        "output_tokens": 3000,
                        "reasoning_output_tokens": 1200,
                        "total_tokens": 153000,
                    },
                },
            },
        },
    ]


def test_parse_session_file_reads_tokens_and_context(tmp_path):
    path = tmp_path / "2026" / "05" / "04" / f"rollout-2026-05-04T14-04-13-{SESSION_ID}.jsonl"
    _write_jsonl(path, _codex_events())

    metrics = parse_session_file(path)

    assert metrics is not None
    assert metrics.conversation_id == SESSION_ID
    assert metrics.model == "gpt-5.5"
    assert metrics.input_tokens == 150000
    assert metrics.cache_read_tokens == 120000
    assert metrics.output_tokens == 3000
    assert metrics.reasoning_tokens == 1200
    assert metrics.message_count == 1
    assert metrics.context_pct_real == pytest.approx(39.1, abs=0.05)
    assert metrics.context_pct == metrics.context_pct_real
    assert metrics.first_timestamp == "2026-05-04T14:04:13.388Z"
    assert metrics.last_timestamp == "2026-05-04T14:05:13.000Z"
    assert metrics.duration_minutes == pytest.approx(1.0, abs=0.05)


def test_provider_parse_session_finds_nested_jsonl(tmp_path, monkeypatch):
    session_dir = tmp_path / "sessions"
    path = session_dir / "2026" / "05" / "04" / f"rollout-2026-05-04T14-04-13-{SESSION_ID}.jsonl"
    _write_jsonl(path, _codex_events())
    monkeypatch.setattr(codex_metrics, "CODEX_SESSIONS_DIR", session_dir)

    metrics = CodexMetricsProvider().parse_session(SESSION_ID)

    assert metrics is not None
    assert metrics.conversation_id == SESSION_ID


def test_detect_codex_for_session_matches_start_window(tmp_path, monkeypatch):
    session_dir = tmp_path / "sessions"
    wanted = session_dir / "2026" / "05" / "04" / f"rollout-2026-05-04T14-04-13-{SESSION_ID}.jsonl"
    late_id = "019df355-0000-7000-8000-000000000000"
    late = session_dir / "2026" / "05" / "04" / f"rollout-2026-05-04T14-09-13-{late_id}.jsonl"
    _write_jsonl(wanted, _codex_events())
    _write_jsonl(
        late,
        _codex_events(
            session_id=late_id,
            started_at="2026-05-04T14:09:13.000Z",
        ),
    )
    monkeypatch.setattr(codex_metrics, "CODEX_SESSIONS_DIR", session_dir)

    # Epoch for 2026-05-04T14:04:10Z. The wanted JSONL starts 3s later,
    # while the later unrelated session is outside the bounded match window.
    assert detect_codex_for_session(1777903450.0) == SESSION_ID


def test_detect_codex_for_session_respects_already_linked(tmp_path, monkeypatch):
    session_dir = tmp_path / "sessions"
    path = session_dir / "2026" / "05" / "04" / f"rollout-2026-05-04T14-04-13-{SESSION_ID}.jsonl"
    _write_jsonl(path, _codex_events())
    monkeypatch.setattr(codex_metrics, "CODEX_SESSIONS_DIR", session_dir)

    assert detect_codex_for_session(1777903450.0, already_linked=[SESSION_ID]) is None


def test_detect_codex_for_process_reads_open_jsonl_fd(tmp_path, monkeypatch):
    session_dir = tmp_path / "sessions"
    path = (
        session_dir
        / "2026"
        / "04"
        / "28"
        / f"rollout-2026-04-28T15-48-42-{SESSION_ID}.jsonl"
    )
    _write_jsonl(
        path,
        _codex_events(
            started_at="2026-04-28T15:48:42.000Z",
        ),
    )

    proc_root = tmp_path / "proc"
    root_task = proc_root / "100" / "task" / "100"
    child_task = proc_root / "101" / "task" / "101"
    child_fd = proc_root / "101" / "fd"
    root_task.mkdir(parents=True)
    child_task.mkdir(parents=True)
    child_fd.mkdir(parents=True)
    (root_task / "children").write_text("101\n", encoding="utf-8")
    (child_task / "children").write_text("", encoding="utf-8")
    (child_fd / "50").symlink_to(path)

    monkeypatch.setattr(codex_metrics, "CODEX_SESSIONS_DIR", session_dir)
    monkeypatch.setattr(codex_metrics, "_PROC_ROOT", proc_root)

    # A resumed Codex session can be days older than the tmux pane, so the
    # timestamp detector misses it while the process-fd detector still links it.
    assert detect_codex_for_session(1777903450.0) is None
    assert detect_codex_for_process(100) == SESSION_ID
    assert detect_codex_for_process(100, already_linked=[SESSION_ID]) is None
