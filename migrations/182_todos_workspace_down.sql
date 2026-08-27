-- Migration 182 rollback. The global source/source_ref key cannot represent
-- two workspace-owned rows with the same source identity, so the temporary
-- unique index is built before replacing the live table and fails closed when
-- such rows exist. No tenant row is deleted or merged.

PRAGMA foreign_keys=OFF;
BEGIN IMMEDIATE;

DROP TRIGGER IF EXISTS todos_workspace_required_insert;
DROP TRIGGER IF EXISTS todos_workspace_required_update;
DROP TRIGGER IF EXISTS todos_workspace_immutable;

CREATE TABLE todos_v182_down (
    id             TEXT PRIMARY KEY,
    type           TEXT NOT NULL CHECK (type IN (
                     'promemoria','azione','idea','decidi','approva','rivedi'
                   )),
    family         TEXT NOT NULL CHECK (family IN ('captured','system')),
    status         TEXT NOT NULL DEFAULT 'aperto',
    text           TEXT NOT NULL,
    payload        TEXT,
    fu             TEXT NOT NULL,
    project        TEXT,
    source         TEXT NOT NULL DEFAULT 'user',
    source_ref     TEXT,
    doer           TEXT CHECK (doer IN ('human','agent','hybrid')),
    linked_task_id TEXT,
    created_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL,
    resolved_at    TEXT
);

INSERT INTO todos_v182_down (
    id,type,family,status,text,payload,fu,project,source,source_ref,doer,
    linked_task_id,created_at,updated_at,resolved_at
)
SELECT
    id,type,family,status,text,payload,fu,project,source,source_ref,doer,
    linked_task_id,created_at,updated_at,resolved_at
FROM todos;

CREATE UNIQUE INDEX idx_todos_dedup_v182_down
ON todos_v182_down(source, source_ref)
WHERE source_ref IS NOT NULL;

DROP TABLE todos;
ALTER TABLE todos_v182_down RENAME TO todos;

DROP INDEX idx_todos_dedup_v182_down;
CREATE INDEX idx_todos_open ON todos(status, fu);
CREATE INDEX idx_todos_project ON todos(project);
CREATE UNIQUE INDEX idx_todos_dedup
ON todos(source, source_ref) WHERE source_ref IS NOT NULL;

DELETE FROM schema_versions WHERE version = 182;

COMMIT;
PRAGMA foreign_keys=ON;
