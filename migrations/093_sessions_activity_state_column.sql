-- Migration 093 — sessions_meta.activity_state (PR1 hotfix)
--
-- Hotfix per PR1 (migration 092 + commit 4bbdb86): aggiungeva solo la
-- colonna `activity_state_updated_at` al DB, ma il codice di
-- `list_sessions` (api/routers/sessions.py:764) accede a
-- `db_row["activity_state"]` per il fallback gate event-driven. La
-- colonna `activity_state` esisteva solo come campo Pydantic in-memory
-- in SessionInfo, mai persisted. Live API solleva:
--   IndexError: No item with that key
-- su ogni GET /sessions dopo deploy.
--
-- Inoltre `record_state_event` UPDATE su `activity_state` non scriveva
-- (la colonna non esisteva), quindi l'endpoint POST /sessions/{name}/state
-- ritornava 204 ma silenziosamente non aggiornava lo stato.
--
-- Fix: ADD COLUMN activity_state TEXT NULL. Default NULL = nessun event
-- ancora ricevuto, fallback gate in list_sessions cade su scraping TUI
-- come prima del deploy.
--
-- Reversibile: vedi migrations/093_sessions_activity_state_column_down.sql.

BEGIN IMMEDIATE;

ALTER TABLE sessions_meta
    ADD COLUMN activity_state TEXT NULL;

INSERT OR IGNORE INTO schema_versions (version) VALUES (93);

COMMIT;
