-- Rollback per migration 099 (restore graph_edges.weight + last_touched_at).
--
-- SQLite non supporta ALTER TABLE DROP COLUMN < 3.35; per rollback canonico
-- serve drop+recreate. Pero', se serve solo "tornare" allo stato post-098
-- rotto, basta rimuovere il version bump (le colonne restano ma non sono
-- piu' tracciate dal schema_versions). Non ricreiamo la regression.

BEGIN IMMEDIATE;

DELETE FROM schema_versions WHERE version = 99;

COMMIT;
