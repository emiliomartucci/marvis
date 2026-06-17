-- Rollback per migration 137 (promote llm_costs).
--
-- SQLite pre-3.35 non supporta DROP COLUMN; per rollback canonico delle 3
-- nuove colonne (tier_logical, fallback_used, litellm_request_id) servirebbe
-- drop+recreate della tabella. Pero' llm_costs contiene dati operativi
-- (cost tracking inbox classifier, ~1500 row/mese) e ricrearla rischierebbe
-- perdita di dati. Per rollback al volo, basta rimuovere il version bump:
-- le colonne extra restano fisicamente in tabella ma il codice pre-137
-- semplicemente non le legge ne' scrive (NULL default).
--
-- Per rollback completo (drop colonne + ripristino lazy-create), servirebbe
-- una migration successiva con CREATE TABLE _new + INSERT SELECT + DROP +
-- RENAME. Non lo facciamo qui — questa down-migration e' "version bump only".

BEGIN IMMEDIATE;

DELETE FROM schema_versions WHERE version = 137;

COMMIT;
