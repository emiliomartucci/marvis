-- Migration 046: Add salience, archived, salience_updated_at to documents + boost_log table
-- Columns added via Python hook in db.py (_add_salience_columns) AFTER this SQL runs.
-- Indexes created in Python hook after columns exist.

-- boost_log for rate limiting boost operations
CREATE TABLE IF NOT EXISTS boost_log (
    doc_id INTEGER NOT NULL,
    caller_id TEXT NOT NULL,
    boosted_at TEXT NOT NULL DEFAULT (datetime('now','utc'))
);
CREATE INDEX IF NOT EXISTS idx_boost_log_rate ON boost_log(doc_id, caller_id, boosted_at);

INSERT OR IGNORE INTO schema_versions (version) VALUES (46);
