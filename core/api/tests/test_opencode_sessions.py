from __future__ import annotations

import sqlite3
from pathlib import Path

from core.api.services import opencode_sessions


def _make_session_db(path: Path) -> sqlite3.Connection:
    """Create the OpenCode ``session`` table schema and return an open conn."""
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE session ("
        "id TEXT PRIMARY KEY, "
        "project_id TEXT NOT NULL, "
        "parent_id TEXT, "
        "slug TEXT NOT NULL, "
        "directory TEXT NOT NULL, "
        "title TEXT NOT NULL, "
        "version TEXT NOT NULL, "
        "share_url TEXT, "
        "summary_additions INTEGER, "
        "summary_deletions INTEGER, "
        "summary_files INTEGER, "
        "summary_diffs TEXT, "
        "revert TEXT, "
        "permission TEXT, "
        "time_created INTEGER NOT NULL, "
        "time_updated INTEGER NOT NULL, "
        "time_compacting INTEGER, "
        "time_archived INTEGER, "
        "workspace_id TEXT)"
    )
    return conn


def _insert_sessions(conn: sqlite3.Connection, rows: list[tuple]) -> None:
    normalized_rows = [
        (*row[:4], opencode_sessions._normalize_directory(str(row[4])), *row[5:])
        for row in rows
    ]
    conn.executemany(
        "INSERT INTO session (id, project_id, parent_id, slug, directory, title, version, permission, time_created, time_updated) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        normalized_rows,
    )
    conn.commit()


def test_find_session_id_for_created_at_picks_closest_match(tmp_path, monkeypatch):
    db_path = tmp_path / "opencode.db"
    conn = _make_session_db(db_path)
    _insert_sessions(
        conn,
        [
            (
                "ses_old",
                "global",
                None,
                "old",
                "/var/marvisx/workspace",
                "Old",
                "1.3.17",
                None,
                1_775_632_000_000,
                1_775_632_100_000,
            ),
            (
                "ses_target",
                "global",
                None,
                "target",
                "/var/marvisx/workspace",
                "Target",
                "1.3.17",
                None,
                1_775_632_364_000,
                1_775_632_400_000,
            ),
        ],
    )
    conn.close()

    monkeypatch.setattr(opencode_sessions, "OPENCODE_DB_PATH", db_path)

    session_id = opencode_sessions.find_session_id_for_created_at(
        "/var/marvisx/workspace",
        "2026-04-08T07:12:44+00:00",
    )

    assert session_id == "ses_target"


def test_detect_picks_most_recent_by_time_updated(tmp_path, monkeypatch):
    """3 sessions same directory, different time_updated → returns most recent."""
    db_path = tmp_path / "opencode.db"
    conn = _make_session_db(db_path)
    _insert_sessions(
        conn,
        [
            (
                "ses_oldest",
                "global",
                None,
                "oldest",
                "/var/marvisx/workspace",
                "Oldest",
                "1.3.17",
                None,
                1_775_632_000_000,
                1_775_632_050_000,
            ),
            (
                "ses_middle",
                "global",
                None,
                "middle",
                "/var/marvisx/workspace",
                "Middle",
                "1.3.17",
                None,
                1_775_632_100_000,
                1_775_632_200_000,
            ),
            (
                "ses_newest",
                "global",
                None,
                "newest",
                "/var/marvisx/workspace",
                "Newest",
                "1.3.17",
                None,
                1_775_632_150_000,
                1_775_632_500_000,
            ),
        ],
    )
    conn.close()

    monkeypatch.setattr(opencode_sessions, "OPENCODE_DB_PATH", db_path)

    session_id = opencode_sessions.detect_opencode_for_session(
        "/var/marvisx/workspace"
    )

    assert session_id == "ses_newest"


def test_detect_filters_by_time_updated_not_created(tmp_path, monkeypatch):
    """Filter on time_updated (last activity), NOT time_created. This keeps
    `opencode --continue <ses_id>` sessions discoverable: their time_created
    predates the pane but time_updated is fresh. Sessions whose last activity
    is older than pane_start are correctly rejected."""
    db_path = tmp_path / "opencode.db"
    conn = _make_session_db(db_path)
    _insert_sessions(
        conn,
        [
            # Resumed session: created days before the pane, but updated
            # during pane lifetime → MUST be discoverable.
            (
                "ses_resumed",
                "global",
                None,
                "resumed",
                "/var/marvisx/workspace",
                "Resumed",
                "1.3.17",
                None,
                1_775_600_000_000,  # time_created: way before pane
                1_775_700_000_000,  # time_updated: after pane_start
            ),
            # Stale session: both created and last-updated before the pane
            # → correctly excluded.
            (
                "ses_stale",
                "global",
                None,
                "stale",
                "/var/marvisx/workspace",
                "Stale",
                "1.3.17",
                None,
                1_775_500_000_000,
                1_775_510_000_000,  # last activity BEFORE pane_start
            ),
        ],
    )
    conn.close()

    monkeypatch.setattr(opencode_sessions, "OPENCODE_DB_PATH", db_path)

    # Pane started at 1_775_699_000_000: after ses_stale's last activity
    # (1_775_510_000_000) but before ses_resumed's (1_775_700_000_000).
    pane_start_ms = 1_775_699_000_000
    session_id = opencode_sessions.detect_opencode_for_session(
        "/var/marvisx/workspace",
        pane_start_ms=pane_start_ms,
    )

    assert session_id == "ses_resumed"


def test_detect_rejects_stale_session(tmp_path, monkeypatch):
    """Only a stale session in the cwd → None (pane_start is after all activity)."""
    db_path = tmp_path / "opencode.db"
    conn = _make_session_db(db_path)
    _insert_sessions(
        conn,
        [
            (
                "ses_stale",
                "global",
                None,
                "stale",
                "/var/marvisx/workspace",
                "Stale",
                "1.3.17",
                None,
                1_775_500_000_000,
                1_775_510_000_000,  # updated before pane
            ),
        ],
    )
    conn.close()

    monkeypatch.setattr(opencode_sessions, "OPENCODE_DB_PATH", db_path)

    pane_start_ms = 1_775_699_000_000
    session_id = opencode_sessions.detect_opencode_for_session(
        "/var/marvisx/workspace",
        pane_start_ms=pane_start_ms,
    )

    assert session_id is None


def test_detect_respects_exclude_ids(tmp_path, monkeypatch):
    """2 sessions, exclude the newer → returns the older one."""
    db_path = tmp_path / "opencode.db"
    conn = _make_session_db(db_path)
    _insert_sessions(
        conn,
        [
            (
                "ses_older",
                "global",
                None,
                "older",
                "/var/marvisx/workspace",
                "Older",
                "1.3.17",
                None,
                1_775_632_000_000,
                1_775_632_100_000,
            ),
            (
                "ses_newer",
                "global",
                None,
                "newer",
                "/var/marvisx/workspace",
                "Newer",
                "1.3.17",
                None,
                1_775_632_200_000,
                1_775_632_300_000,
            ),
        ],
    )
    conn.close()

    monkeypatch.setattr(opencode_sessions, "OPENCODE_DB_PATH", db_path)

    session_id = opencode_sessions.detect_opencode_for_session(
        "/var/marvisx/workspace",
        exclude_ids=["ses_newer"],
    )

    assert session_id == "ses_older"


def test_detect_no_match_returns_none(tmp_path, monkeypatch):
    """Different directory → None."""
    db_path = tmp_path / "opencode.db"
    conn = _make_session_db(db_path)
    _insert_sessions(
        conn,
        [
            (
                "ses_other",
                "global",
                None,
                "other",
                "/var/marvisx/other",
                "Other",
                "1.3.17",
                None,
                1_775_632_000_000,
                1_775_632_100_000,
            ),
        ],
    )
    conn.close()

    monkeypatch.setattr(opencode_sessions, "OPENCODE_DB_PATH", db_path)

    session_id = opencode_sessions.detect_opencode_for_session(
        "/var/marvisx/workspace"
    )

    assert session_id is None


def test_detect_missing_db_returns_none(tmp_path, monkeypatch):
    """DB file absent → None without raising."""
    missing = tmp_path / "does-not-exist.db"
    monkeypatch.setattr(opencode_sessions, "OPENCODE_DB_PATH", missing)

    session_id = opencode_sessions.detect_opencode_for_session(
        "/var/marvisx/workspace"
    )

    assert session_id is None
