-- Down migration 090 — rollback inbox node prefix (code-level no-op).
--
-- Per rollback completo:
--   1. Revertire scripts/populate_inbox_nodes.py + NODE_PREFIXES change in
--      api/services/graph_service.py (rimuovere 'inbox').
--   2. Ripulire graph_nodes residui:
--        DELETE FROM graph_nodes WHERE id LIKE 'inbox:artifact:%';
--      Le edge `refers_to` vengono pulite automaticamente via FK CASCADE.
--   3. Decrementare schema_versions (la row 90 viene lasciata; la mossa e'
--      sufficiente per riportare lo schema al punto di partenza dal POV SQL).
--
-- Il down script qui non cancella i nodi (scelta conservativa: rollback
-- preserva dati indicizzati in caso di re-apply). Cancellazione manuale
-- richiesta operator-side se il rollback e' definitivo.

BEGIN IMMEDIATE;

DELETE FROM schema_versions WHERE version = 90;

COMMIT;
