-- Rollback per migration 101 (local_llm_shadow_comparisons).
--
-- Tabella nuova introdotta da 101: rollback drop puro e' sicuro perche' la
-- tabella non ha foreign-key incoming e i dati shadow sono retention-7gg
-- per design (judge sample manuale post-week 1 chiude il loop).

BEGIN IMMEDIATE;

DROP INDEX IF EXISTS idx_shadow_item;
DROP INDEX IF EXISTS idx_shadow_feature_created;
DROP TABLE IF EXISTS local_llm_shadow_comparisons;

DELETE FROM schema_versions WHERE version = 101;

COMMIT;
