-- Migration 090 — KG: add 'inbox' node prefix (code-level)
--
-- Obiettivo: indicizzare `inbox_items` WHERE treatment IN ('save','read_save')
-- come nodi `inbox:artifact:<id>` nel KG, con edge `refers_to` verso i
-- project super-nodes via topic → project mapping statico.
--
-- Perche' no-op SQL: il vincolo di formato su `graph_nodes.id` (prefix
-- whitelist) vive in api/services/graph_service.py::NODE_PREFIXES (pattern
-- regex NODE_ID_PATTERN derivato). Lo schema SQLite ha solo
-- CHECK(type IN (...)) su graph_nodes.type, e gli inbox nodes riusano
-- `type='artifact'` (gia' consentito). Il prefix 'inbox' appare solo
-- nell'ID string, colonna TEXT senza constraint regex.
--
-- Questa migration serve solo a bumpare schema_versions → 90 cosi' il
-- code-level change e' rintracciabile nel ledger (coerente con convenzione
-- usata dai KG-populate: la logica vive in Python + `schema_versions`
-- tiene il pin di versione per audit/deploy).
--
-- Popolamento: scripts/populate_inbox_nodes.py (nuovo).
-- Rollback: 090_kg_inbox_node_type_down.sql (rimuove prefix + nodi).

BEGIN IMMEDIATE;

INSERT OR IGNORE INTO schema_versions (version) VALUES (90);

COMMIT;
