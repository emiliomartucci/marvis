-- Migration 153 — rebuild-safe manual project relations.
--
-- ADDITIVE, costo costante: 1 CREATE TABLE + 2 indici. Nessuna riga esistente
-- toccata. VERIFY prod schema_versions max prima del merge: prod = 150; 151 e'
-- riservata al worker parallelo, 152 e' project_gui_metadata → questo file e'
-- 153 (il runner applica solo version > MAX).
--
-- `graph_edges.source='manual'` esiste, ma i full rebuild ricostruiscono
-- graph_edges. Le relazioni create a mano dalla GUI devono sopravvivere:
-- persistono qui con provenance esplicita e vengono fuse nei read path.

BEGIN IMMEDIATE;

CREATE TABLE IF NOT EXISTS manual_project_edges (
    workspace_id TEXT NOT NULL DEFAULT 'ws_default',
    src_slug TEXT NOT NULL,
    dst_slug TEXT NOT NULL,
    kind TEXT NOT NULL CHECK(kind IN ('related', 'depends_on')),
    provenance TEXT NOT NULL DEFAULT 'manual' CHECK(provenance = 'manual'),
    created_by TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    CHECK(src_slug <> dst_slug),
    PRIMARY KEY (workspace_id, src_slug, dst_slug, kind)
);

CREATE INDEX IF NOT EXISTS idx_manual_project_edges_src
    ON manual_project_edges(workspace_id, src_slug);

CREATE INDEX IF NOT EXISTS idx_manual_project_edges_dst
    ON manual_project_edges(workspace_id, dst_slug);

INSERT OR IGNORE INTO schema_versions(version) VALUES (153);

COMMIT;
