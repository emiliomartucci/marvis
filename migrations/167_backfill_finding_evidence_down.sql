-- Revert migration 167 — no-op on data.
--
-- This was a one-way data heal (synthetic evidence rows for evidence-less
-- findings). The backfilled rows are indistinguishable from legitimate
-- kg_node evidence at position 0, so we do NOT delete them on down (that would
-- risk removing real evidence). Only the schema_versions marker is removed.

DELETE FROM schema_versions WHERE version = 167;
