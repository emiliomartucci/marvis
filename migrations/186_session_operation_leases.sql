-- Migration 186: serialize destructive session lifecycle operations.
--
-- A complete/hibernate/restart/resume/delete sequence performs slow tmux I/O
-- outside SQLite.  The persisted generation below prevents another process
-- from starting a replacement operation for the same tenant/name while that
-- external effect is still in flight.  Expiry is crash recovery, not normal
-- release: successful callers clear the operation with the exact generation.

BEGIN IMMEDIATE;

CREATE TABLE session_operation_leases (
    workspace_id TEXT NOT NULL,
    session_name TEXT NOT NULL,
    session_uuid TEXT NOT NULL,
    generation INTEGER NOT NULL DEFAULT 0 CHECK (generation >= 0),
    operation TEXT CHECK (
        operation IS NULL OR operation IN (
            'complete', 'delete', 'hibernate', 'resume', 'restart'
        )
    ),
    lease_expires_at TEXT,
    updated_at TEXT NOT NULL DEFAULT (
        strftime('%Y-%m-%dT%H:%M:%fZ','now')
    ),
    PRIMARY KEY (workspace_id, session_name),
    CHECK (
        (operation IS NULL AND lease_expires_at IS NULL)
        OR (operation IS NOT NULL AND lease_expires_at IS NOT NULL)
    )
);

CREATE INDEX idx_session_operation_leases_active
    ON session_operation_leases(workspace_id, lease_expires_at)
    WHERE operation IS NOT NULL;

INSERT OR IGNORE INTO schema_versions(version) VALUES (186);

COMMIT;
