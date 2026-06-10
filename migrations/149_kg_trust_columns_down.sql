-- Down migration 149 — rollback of the KG trust columns (Fase C).
--
-- NON-DISTRUTTIVO di proposito, come il down di mig 067 per QUESTE STESSE tabelle.
-- Non facciamo `ALTER TABLE graph_nodes/graph_edges DROP COLUMN`:
--   * SQLite, su DROP COLUMN, ri-valida TUTTI i trigger della tabella; graph_nodes
--     porta i trigger di sync FTS (graph_nodes_fts_*) e il DB prod usa l'estensione
--     vettoriale vec0 → un DROP COLUMN che fallisse a meta' lascerebbe lo schema in
--     stato peggiore del problema che risolve.
--   * Le due colonne sono nullable e non lette da nessun path con flag off → tenerle
--     e' a costo zero (stesso ragionamento di mig 067: "lasciare le colonne NULL e'
--     zero-cost, quindi non includiamo un down distruttivo").
--
-- Rollback effettivo = togliere il marcatore di versione; le colonne restano NULL e
-- inerti. Per una rimozione forzata delle colonne, eseguire il table-rebuild manuale
-- nel contesto completo (estensioni + FTS) caricato dal processo pir-api.

DELETE FROM schema_versions WHERE version = 149;
