-- Migration 175: workspace-scoped, tamper-evident audit chain foundations.
--
-- audit_log's five nullable columns are added by the guarded Python migration
-- hook in core/api/db.py. SQLite does not portably support ADD COLUMN IF NOT
-- EXISTS, while fresh installs and interrupted upgrades must both be retryable.
-- This SQL therefore owns only objects that are safe to create idempotently.
-- The hook also installs the partial unique index and validation triggers once
-- the columns exist. Existing migration-145 update/delete guards are preserved.
--
-- Enforcement starts OFF. U4a introduces the chain primitives only; a later
-- caller migration may activate the chainless-insert guard after every required
-- writer participates in a caller-owned transaction.

BEGIN IMMEDIATE;

CREATE TABLE IF NOT EXISTS audit_chain_heads (
    workspace_id    TEXT PRIMARY KEY CHECK (length(trim(workspace_id)) > 0),
    last_sequence   INTEGER NOT NULL CHECK (last_sequence >= 0),
    last_entry_hash TEXT NOT NULL CHECK (
        length(last_entry_hash) = 64
        AND last_entry_hash = lower(last_entry_hash)
        AND last_entry_hash NOT GLOB '*[^0-9a-f]*'
    ),
    hash_version    INTEGER NOT NULL DEFAULT 1 CHECK (hash_version = 1),
    updated_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_chain_state (
    id                  INTEGER PRIMARY KEY CHECK (id = 1),
    enforcement_enabled INTEGER NOT NULL DEFAULT 0
        CHECK (enforcement_enabled IN (0, 1)),
    activated_at        TEXT,
    legacy_root_hash    TEXT CHECK (
        legacy_root_hash IS NULL
        OR (
            length(legacy_root_hash) = 64
            AND legacy_root_hash = lower(legacy_root_hash)
            AND legacy_root_hash NOT GLOB '*[^0-9a-f]*'
        )
    ),
    CHECK (
        (enforcement_enabled = 0 AND activated_at IS NULL)
        OR (enforcement_enabled = 1 AND activated_at IS NOT NULL)
    )
);

INSERT OR IGNORE INTO audit_chain_state
    (id, enforcement_enabled, activated_at, legacy_root_hash)
VALUES (1, 0, NULL, NULL);

INSERT OR IGNORE INTO schema_versions (version) VALUES (175);

COMMIT;
