-- v1.0.0 - 2026-04-22 - Feed-style columns for project_status_updates (PR #9 single-pager v2)
-- Additive migration: adds kind/content_md/ref_id so the existing structured
-- payload (status/what_done/blockers/next_steps) keeps working while the new
-- /projects/detail single-pager v2 can render a chronological feed of mixed
-- manual + auto-derived entries.

ALTER TABLE project_status_updates ADD COLUMN kind TEXT
    CHECK (kind IS NULL OR kind IN ('manual', 'auto_handoff', 'auto_commit', 'ai_summary'));
ALTER TABLE project_status_updates ADD COLUMN content_md TEXT;
ALTER TABLE project_status_updates ADD COLUMN ref_id TEXT;
ALTER TABLE project_status_updates ADD COLUMN author_display TEXT;

-- Backfill kind='manual' for pre-existing structured rows so the feed query
-- can UNION them with fresh auto_* rows without NULLs confusing downstream.
UPDATE project_status_updates SET kind = 'manual' WHERE kind IS NULL;

CREATE INDEX IF NOT EXISTS idx_status_updates_project_kind_ts
    ON project_status_updates(project, kind, created_at DESC);

INSERT OR IGNORE INTO schema_versions (version) VALUES (86);
