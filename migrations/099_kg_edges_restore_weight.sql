-- Migration 099 — Hotfix regression migration 098: restore graph_edges columns
--
-- Migration 098 ha ricostruito graph_edges usando come template lo schema della
-- migration 091. Quel template era pre-095, quindi mancavano le colonne aggiunte
-- successivamente:
--   `weight REAL NOT NULL DEFAULT 1.0`   (mig 095)
--   `last_touched_at TEXT`               (mig 095)
--
-- insert_saga._ensure_kg_edge include `weight, last_touched_at` nel INSERT,
-- quindi post-098 saltava con:
--   sqlite3.OperationalError: table graph_edges has no column named weight
--
-- Live failure: two policy files → status=parse_error.
--
-- Fix: ALTER TABLE ADD COLUMN (le 2 colonne mancanti) + UPDATE FROM la backup
-- table `graph_edges_backup_098` (creata dalla forward 098, contiene i valori
-- originali). Nessun rebuild necessario.
--
-- DEPLOY PROCEDURE (auto via run_migrations al startup):
--   1. Deploy api → /data/pir/migrations/099*.sql sincronizzata
--   2. systemctl --user restart pir-api.service
--   3. run_migrations() vede 99 > current (98) → applica.
--
-- Reversibile: vedi migrations/099_kg_edges_restore_weight_down.sql.

BEGIN IMMEDIATE;

-- ---- Add columns lost by migration 098 -----------------------------------
ALTER TABLE graph_edges ADD COLUMN weight REAL NOT NULL DEFAULT 1.0;
ALTER TABLE graph_edges ADD COLUMN last_touched_at TEXT;

-- ---- Restore valori dalla backup table -----------------------------------
-- graph_edges_backup_098 contiene la copia pre-098 (con weight + last_touched_at).
-- Restoriamo i valori per ogni edge che esiste ancora.
UPDATE graph_edges
   SET weight          = COALESCE((
           SELECT bk.weight
             FROM graph_edges_backup_098 bk
            WHERE bk.id = graph_edges.id
       ), 1.0),
       last_touched_at = (
           SELECT bk.last_touched_at
             FROM graph_edges_backup_098 bk
            WHERE bk.id = graph_edges.id
       )
 WHERE EXISTS (
       SELECT 1
         FROM graph_edges_backup_098 bk
        WHERE bk.id = graph_edges.id
 );

-- Per le row inserite post-098 (non in backup) o nuove, weight ha default 1.0
-- e last_touched_at NULL (saga lo popolera' al prossimo INSERT con
-- ON CONFLICT DO UPDATE).

INSERT OR IGNORE INTO schema_versions (version) VALUES (99);

COMMIT;
