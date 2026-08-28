from __future__ import annotations

import json
import importlib
import sqlite3
from pathlib import Path

from core.api.services.audit_chain import legacy_root_hash_v1
from core.api.services.workspace_tools import AuditEvent
from core.api.use_cases._context import CallerContext


def _activated_audit_db(path: Path) -> None:
    with sqlite3.connect(path) as conn:
        conn.executescript(
            """
            CREATE TABLE audit_log (
                id TEXT PRIMARY KEY DEFAULT (lower(hex(randomblob(16)))),
                timestamp TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
                action TEXT NOT NULL,
                user TEXT NOT NULL,
                resource_type TEXT NOT NULL,
                resource_id TEXT NOT NULL,
                details_json TEXT,
                workspace_id TEXT,
                workspace_sequence INTEGER,
                previous_hash TEXT,
                entry_hash TEXT,
                hash_version INTEGER
            );
            CREATE UNIQUE INDEX idx_audit_log_workspace_sequence
            ON audit_log(workspace_id, workspace_sequence)
            WHERE workspace_id IS NOT NULL AND workspace_sequence IS NOT NULL;
            CREATE TABLE audit_chain_heads (
                workspace_id TEXT PRIMARY KEY,
                last_sequence INTEGER NOT NULL,
                last_entry_hash TEXT NOT NULL,
                hash_version INTEGER NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE audit_chain_state (
                id INTEGER PRIMARY KEY,
                enforcement_enabled INTEGER NOT NULL,
                activated_at TEXT,
                legacy_root_hash TEXT
            );
            CREATE TRIGGER audit_log_chainless_after_activation
            BEFORE INSERT ON audit_log
            WHEN (SELECT enforcement_enabled FROM audit_chain_state WHERE id = 1) = 1
                 AND NEW.hash_version IS NULL
            BEGIN
                SELECT RAISE(ABORT, 'audit_log chain fields required after activation');
            END;
            """
        )
        conn.execute(
            "INSERT INTO audit_chain_state "
            "(id, enforcement_enabled, activated_at, legacy_root_hash) "
            "VALUES (1, 1, '2026-08-28T00:00:00Z', ?)",
            (legacy_root_hash_v1([]),),
        )


def test_workspace_audit_sink_appends_to_activated_chain(
    tmp_path: Path, monkeypatch
) -> None:
    workspace = importlib.import_module("core.api.mcp.tools.workspace")
    db_path = tmp_path / "audit.db"
    _activated_audit_db(db_path)
    monkeypatch.setenv("PIR_DB_PATH", str(db_path))
    monkeypatch.setattr(
        workspace,
        "current_mcp_context",
        lambda: CallerContext(
            username="local",
            user_id="local",
            system_role="super_admin",
            user_type="human",
            workspace_id="ws_default",
            scopes=("*",),
            is_human_session=True,
        ),
    )

    workspace._workspace_audit_sink(
        AuditEvent(
            action="write_file",
            phase="completion",
            actor_id="local",
            tenant_id="core",
            path="project/context.md",
            outcome="ok",
            metadata={"size_bytes": 12, "content": "must-not-be-audited"},
        )
    )

    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            "SELECT action, workspace_id, workspace_sequence, previous_hash, "
            "entry_hash, hash_version, details_json FROM audit_log"
        ).fetchone()
        head = conn.execute(
            "SELECT last_sequence, last_entry_hash FROM audit_chain_heads "
            "WHERE workspace_id = 'ws_default'"
        ).fetchone()

    assert row is not None
    assert row[:3] == ("workspace.write_file.completion", "ws_default", 1)
    assert all(isinstance(value, str) and len(value) == 64 for value in row[3:5])
    assert row[5] == 1
    assert json.loads(row[6])["metadata"] == {"size_bytes": 12}
    assert head == (1, row[4])
