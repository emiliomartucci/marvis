-- Down-migration 174 — remove the salience reinforcement ledger.
--
-- Drops the two additive tables from
-- migrations/174_salience_reinforcement.sql (their indexes drop with them).
-- documents is untouched (the migration never altered it). Any accumulated
-- ledger/reject rows are lost — acceptable: the ledger only ever ADJUSTS
-- ranking at read time; salience_base on documents is the floor and survives.

BEGIN IMMEDIATE;

DROP TABLE IF EXISTS salience_boosts;
DROP TABLE IF EXISTS boost_rejects;

DELETE FROM schema_versions WHERE version = 174;

COMMIT;
