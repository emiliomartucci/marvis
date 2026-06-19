-- Migration 152 — project GUI color metadata.
--
-- ADDITIVE, costo costante: 1 CREATE TABLE + 1 indice. Nessuna riga esistente
-- toccata. VERIFY prod schema_versions max prima del merge: prod = 150; 151 e'
-- riservata al worker parallelo → questo file e' 152 (il runner applica solo
-- version > MAX).
--
-- I progetti MarvisX sono filesystem-backed (`/data/projects/{slug}/project.yaml`),
-- non esiste una tabella `projects` affidabile da alterare. Il colore GUI vive
-- quindi in una tabella overlay piccola keyed by workspace+project_slug.
-- `color IS NULL` = palette di default lato client.

BEGIN IMMEDIATE;

CREATE TABLE IF NOT EXISTS project_gui_metadata (
    workspace_id TEXT NOT NULL DEFAULT 'ws_default',
    project_slug TEXT NOT NULL,
    color TEXT CHECK(
        color IS NULL
        OR color GLOB '#[0-9A-Fa-f][0-9A-Fa-f][0-9A-Fa-f][0-9A-Fa-f][0-9A-Fa-f][0-9A-Fa-f]'
    ),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    PRIMARY KEY (workspace_id, project_slug)
);

CREATE INDEX IF NOT EXISTS idx_project_gui_metadata_project
    ON project_gui_metadata(project_slug);

INSERT OR IGNORE INTO schema_versions(version) VALUES (152);

COMMIT;
