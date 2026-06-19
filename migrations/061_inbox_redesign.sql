-- v1.0.0 - 2026-04-11 - inbox sources + newsletter editions + app_settings + source_scores.reads
-- PR A: foundation for inbox + newsletter redesign (schema only, no LLM, no CRUD).
--
-- Tables created (idempotent):
--   * inbox_sources           - catalog of ingest sources with health tracking
--   * newsletter_editions     - archive of sent newsletter editions (GDPR-safe: no PII)
--   * app_settings            - runtime kill switches (no restart needed)
--
-- Schema mutations:
--   * source_scores.reads     - counter column for 'read' signal in scoring
--
-- ALTER TABLE ADD COLUMN is safe here because the migration runner in api/db.py
-- checks schema_versions first and only applies each migration file once.
-- The INSERT OR IGNORE INTO schema_versions at the end guards re-execution
-- even if executescript is re-invoked by an unusual flow.

CREATE TABLE IF NOT EXISTS inbox_sources (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  source_key TEXT NOT NULL,
  feed_url TEXT,
  source_type TEXT NOT NULL DEFAULT 'rss',
  active INTEGER NOT NULL DEFAULT 1,
  last_fetch_at TEXT,
  last_fetch_error TEXT,
  workspace_id TEXT NOT NULL DEFAULT 'ws_default',
  created_at TEXT DEFAULT (datetime('now','utc')),
  updated_at TEXT DEFAULT (datetime('now','utc'))
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_inbox_sources_key
  ON inbox_sources(workspace_id, source_key);

CREATE TABLE IF NOT EXISTS newsletter_editions (
  id TEXT PRIMARY KEY,
  edition_number INTEGER NOT NULL,
  subject TEXT NOT NULL,
  html_content TEXT NOT NULL,
  item_ids_json TEXT NOT NULL,
  recipient_count INTEGER NOT NULL DEFAULT 0,
  recipient_hashes_json TEXT,
  sent_by TEXT NOT NULL,
  sent_at TEXT NOT NULL,
  workspace_id TEXT NOT NULL DEFAULT 'ws_default'
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_newsletter_editions_number
  ON newsletter_editions(workspace_id, edition_number);

CREATE INDEX IF NOT EXISTS idx_newsletter_editions_sent_at
  ON newsletter_editions(workspace_id, sent_at DESC);

CREATE TABLE IF NOT EXISTS app_settings (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL,
  updated_at TEXT DEFAULT (datetime('now','utc'))
);

INSERT OR IGNORE INTO app_settings (key, value) VALUES ('inbox_llm_classifier_enabled', 'shadow');
INSERT OR IGNORE INTO app_settings (key, value) VALUES ('inbox_llm_daily_spend_cap_usd', '0.20');

-- source_scores.reads: differentiated counter for the 'read' signal (weight 0.1).
-- SQLite has permissive type affinity, so an INTEGER column accepts REAL values
-- transparently. No rebuild is needed.
-- ALTER TABLE ADD COLUMN is not idempotent by itself, but the migration runner
-- (api/db.py run_migrations) guarantees each versioned file is applied exactly once.
ALTER TABLE source_scores ADD COLUMN reads INTEGER NOT NULL DEFAULT 0;

INSERT OR IGNORE INTO schema_versions (version) VALUES (61);
