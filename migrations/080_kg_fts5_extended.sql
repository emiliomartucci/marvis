-- Migration 080 — KG Phase 6.6: FTS5 virtual tables for tasks / inbox_items / learnings
--
-- Problem (post-Phase 6.5 regression): `graph_nodes_fts` (migration 078) only
-- indexes nodes inside `graph_nodes`. That table does NOT mirror the live
-- rows in `tasks`, `inbox_items`, or `learnings`, so a probe like
-- `search(q="iperammortamento")` returns 0 tasks despite the tasks table
-- containing 5 matching rows. The hybrid search therefore falls back to
-- semantic-only for these three domains, and the cosine scores (~0.35-0.38)
-- get dominated by BM25 graph_nodes hits with much higher RRF weight.
--
-- Solution: introduce three FTS5 virtual tables mirroring the pattern of
-- `graph_nodes_fts` (migration 078). Each table has:
--   * unicode61 + remove_diacritics 2 tokenizer (Italian-friendly)
--   * sync triggers INSERT / UPDATE / DELETE
--   * soft-delete semantics for `tasks` (deleted_at IS NULL)
--   * no soft-delete for `inbox_items` and `learnings` (their schemas use
--     status / lifecycle fields but do not hard-remove rows)
--
-- Contract with callers (api/services/kg/hybrid_search.py): MATCH queries
-- return `id` plus `bm25(...)` score. The hybrid fusion joins these three
-- new sources (tasks_fts, inbox_items_fts, learnings_fts) plus the existing
-- graph_nodes_fts and the semantic retriever through weighted RRF.
--
-- Deploy:
--   1. stop pir-api.service (WAL lock released)
--   2. apply migration via api/db.py loader
--   3. the initial INSERT SELECT populates from active rows
--   4. triggers keep each index current thereafter
--
-- v0.0.0 - 2026-04-16 - KG Phase 6.6 (hybrid search extended — task recall fix)

PRAGMA foreign_keys=OFF;
BEGIN IMMEDIATE;

-- ---------------------------------------------------------------------------
-- tasks_fts: indexes title + description + tags (JSON string) for MATCH.
-- project / status stored UNINDEXED for downstream filtering.
-- ---------------------------------------------------------------------------

CREATE VIRTUAL TABLE IF NOT EXISTS tasks_fts USING fts5(
    id UNINDEXED,
    title,
    description,
    tags,
    project UNINDEXED,
    status UNINDEXED,
    tokenize='unicode61 remove_diacritics 2'
);

INSERT INTO tasks_fts(id, title, description, tags, project, status)
SELECT
    id,
    title,
    COALESCE(description, ''),
    COALESCE(tags, ''),
    project,
    status
FROM tasks
WHERE deleted_at IS NULL;

DROP TRIGGER IF EXISTS tasks_fts_insert;
CREATE TRIGGER tasks_fts_insert AFTER INSERT ON tasks
WHEN NEW.deleted_at IS NULL
BEGIN
    INSERT INTO tasks_fts(id, title, description, tags, project, status)
    VALUES (
        NEW.id,
        NEW.title,
        COALESCE(NEW.description, ''),
        COALESCE(NEW.tags, ''),
        NEW.project,
        NEW.status
    );
END;

-- UPDATE trigger: delete the old row unconditionally and re-insert only if
-- the new state is still active (deleted_at IS NULL). This cleanly handles
-- both content edits and soft-delete transitions.
DROP TRIGGER IF EXISTS tasks_fts_update;
CREATE TRIGGER tasks_fts_update AFTER UPDATE ON tasks
BEGIN
    DELETE FROM tasks_fts WHERE id = OLD.id;
    INSERT INTO tasks_fts(id, title, description, tags, project, status)
    SELECT
        NEW.id,
        NEW.title,
        COALESCE(NEW.description, ''),
        COALESCE(NEW.tags, ''),
        NEW.project,
        NEW.status
    WHERE NEW.deleted_at IS NULL;
END;

DROP TRIGGER IF EXISTS tasks_fts_delete;
CREATE TRIGGER tasks_fts_delete AFTER DELETE ON tasks
BEGIN
    DELETE FROM tasks_fts WHERE id = OLD.id;
END;

-- ---------------------------------------------------------------------------
-- inbox_items_fts: indexes title + content + tldr for MATCH.
-- No soft-delete column on inbox_items; we intentionally index all rows so
-- the probe can reach historical items regardless of status lifecycle.
-- ---------------------------------------------------------------------------

CREATE VIRTUAL TABLE IF NOT EXISTS inbox_items_fts USING fts5(
    id UNINDEXED,
    title,
    content,
    tldr,
    project UNINDEXED,
    status UNINDEXED,
    tokenize='unicode61 remove_diacritics 2'
);

INSERT INTO inbox_items_fts(id, title, content, tldr, project, status)
SELECT
    id,
    COALESCE(title, ''),
    COALESCE(content, ''),
    COALESCE(tldr, ''),
    COALESCE(default_program, ''),
    COALESCE(status, '')
FROM inbox_items;

DROP TRIGGER IF EXISTS inbox_items_fts_insert;
CREATE TRIGGER inbox_items_fts_insert AFTER INSERT ON inbox_items
BEGIN
    INSERT INTO inbox_items_fts(id, title, content, tldr, project, status)
    VALUES (
        NEW.id,
        COALESCE(NEW.title, ''),
        COALESCE(NEW.content, ''),
        COALESCE(NEW.tldr, ''),
        COALESCE(NEW.default_program, ''),
        COALESCE(NEW.status, '')
    );
END;

DROP TRIGGER IF EXISTS inbox_items_fts_update;
CREATE TRIGGER inbox_items_fts_update AFTER UPDATE ON inbox_items
BEGIN
    DELETE FROM inbox_items_fts WHERE id = OLD.id;
    INSERT INTO inbox_items_fts(id, title, content, tldr, project, status)
    VALUES (
        NEW.id,
        COALESCE(NEW.title, ''),
        COALESCE(NEW.content, ''),
        COALESCE(NEW.tldr, ''),
        COALESCE(NEW.default_program, ''),
        COALESCE(NEW.status, '')
    );
END;

DROP TRIGGER IF EXISTS inbox_items_fts_delete;
CREATE TRIGGER inbox_items_fts_delete AFTER DELETE ON inbox_items
BEGIN
    DELETE FROM inbox_items_fts WHERE id = OLD.id;
END;

-- ---------------------------------------------------------------------------
-- learnings_fts: indexes title + description + prevention + tags.
-- learnings has no hard-delete; `status` column (added via migration 049) is
-- stored UNINDEXED so the hybrid caller can post-filter active-vs-archived.
-- ---------------------------------------------------------------------------

CREATE VIRTUAL TABLE IF NOT EXISTS learnings_fts USING fts5(
    id UNINDEXED,
    title,
    description,
    prevention,
    tags,
    project UNINDEXED,
    category UNINDEXED,
    severity UNINDEXED,
    tokenize='unicode61 remove_diacritics 2'
);

INSERT INTO learnings_fts(id, title, description, prevention, tags, project, category, severity)
SELECT
    id,
    title,
    description,
    COALESCE(prevention, ''),
    COALESCE(tags, ''),
    COALESCE(project, ''),
    category,
    COALESCE(severity, '')
FROM learnings;

DROP TRIGGER IF EXISTS learnings_fts_insert;
CREATE TRIGGER learnings_fts_insert AFTER INSERT ON learnings
BEGIN
    INSERT INTO learnings_fts(id, title, description, prevention, tags, project, category, severity)
    VALUES (
        NEW.id,
        NEW.title,
        NEW.description,
        COALESCE(NEW.prevention, ''),
        COALESCE(NEW.tags, ''),
        COALESCE(NEW.project, ''),
        NEW.category,
        COALESCE(NEW.severity, '')
    );
END;

DROP TRIGGER IF EXISTS learnings_fts_update;
CREATE TRIGGER learnings_fts_update AFTER UPDATE ON learnings
BEGIN
    DELETE FROM learnings_fts WHERE id = OLD.id;
    INSERT INTO learnings_fts(id, title, description, prevention, tags, project, category, severity)
    VALUES (
        NEW.id,
        NEW.title,
        NEW.description,
        COALESCE(NEW.prevention, ''),
        COALESCE(NEW.tags, ''),
        COALESCE(NEW.project, ''),
        NEW.category,
        COALESCE(NEW.severity, '')
    );
END;

DROP TRIGGER IF EXISTS learnings_fts_delete;
CREATE TRIGGER learnings_fts_delete AFTER DELETE ON learnings
BEGIN
    DELETE FROM learnings_fts WHERE id = OLD.id;
END;

INSERT OR IGNORE INTO schema_versions (version) VALUES (80);
COMMIT;
PRAGMA foreign_keys=ON;
