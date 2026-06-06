-- v1.0.0 - 2026-04-11 - Fix inbox_sources backfill: use real URL domains
--
-- Problem: migration 061 backfill used inbox_items.source as the source key
-- (generic strings like "rss-marvisx", "gmail-marvisx"), but source_scores
-- keys by URL domain via _update_source_score in inbox_triage.py. The two
-- tables never joined correctly, so the Sources Dashboard showed 3 generic
-- entries instead of the real domains (notboring.co, simonwillison.net, etc)
-- and every metric rendered as zero.
--
-- Fix strategy:
--   1) Soft-delete the 061 rows (active=0, source_type='legacy' is already
--      set by 061, so we just flip active). This preserves audit history
--      and keeps the Dashboard default filter (active=1) clean.
--   2) Re-backfill inbox_sources from DISTINCT inbox_items.url domains via
--      a Python post-hook (_migration_062_backfill_from_urls in api/db.py),
--      using the same urlparse + removeprefix("www.") + lower normalization.
--
-- The Python post-hook is idempotent thanks to the existing
-- UNIQUE (workspace_id, source_key) index on inbox_sources, so a re-run
-- (e.g. after a DB restore) is safe.

-- Soft-delete every legacy row that 061 created. We match by source_type
-- because 061 always wrote source_type='legacy' (regardless of the raw value
-- of inbox_items.source). This is the safest WHERE clause and also cleans
-- up any future migration that might tag rows as legacy.
UPDATE inbox_sources
SET active = 0,
    source_type = 'legacy',
    updated_at = datetime('now', 'utc')
WHERE source_type = 'legacy';

-- The real backfill lives in the Python post-hook
-- (_migration_062_backfill_from_urls in api/db.py) because urlparse + netloc
-- normalization is not expressible in plain SQLite. The hook runs once, on
-- the next API startup after this migration is applied, and is idempotent.

INSERT OR IGNORE INTO schema_versions (version) VALUES (62);
