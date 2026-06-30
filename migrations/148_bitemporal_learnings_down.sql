-- Down migration 148 — remove the bitemporal columns from learnings (Track 2 #1-S1).
-- VERIFY against prod schema_versions max before merge (see 148_bitemporal_learnings.sql header).
--
-- DROP COLUMN requires SQLite >= 3.35 (2021-03). If targeting an older runtime,
-- replace the ALTER ... DROP COLUMN lines with the 12-step table-rebuild dance
-- (CREATE TABLE learnings_new without the 4 columns → INSERT SELECT → DROP old →
-- RENAME → recreate indexes). Marvis runs on a modern SQLite, so DROP COLUMN is fine.

DROP INDEX IF EXISTS idx_learnings_asof;
DROP INDEX IF EXISTS idx_learnings_live;

ALTER TABLE learnings DROP COLUMN supersede_reason;
ALTER TABLE learnings DROP COLUMN superseded_by;
ALTER TABLE learnings DROP COLUMN invalid_at;
ALTER TABLE learnings DROP COLUMN valid_from;

DELETE FROM schema_versions WHERE version = 148;
