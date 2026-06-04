-- Migration 075 — KG file_state (recovery: 074 collision con 074_kg_infra_types)
-- Reversibile: vedi 075_kg_file_state_recovery_down.sql
-- Dipendenze: nessuna (tabella standalone)
--
-- Scopo: recovery migration per la collisione del numero 074. La 074 originale
-- (074_kg_file_state.sql del PR task 12550dc3) condivideva il numero con
-- 074_kg_infra_types.sql (PR task precedente, merged prima). Il runner
-- `api/db.py::run_migrations` filtra per `version > current_version` leggendo
-- MAX(version) PRIMA del loop → dopo l'applicazione della prima 074 il filtro
-- non considera piu' la seconda come pending. In prod infra_types ha vinto
-- (deployato prima) → file_state skippata → populate_*_incremental crashava
-- con `no such table: file_state`.
--
-- Pattern: learning 4b8466e3 (Digest schema skipped because migration versions
-- collided with existing KG migrations): "For late-discovered collisions on
-- already-upgraded DBs, ship idempotent recovery migrations with new higher
-- numbers."
--
-- Contenuto identico al 074 originale ora eliminato (PK composito path+populator,
-- indici, INSERT schema_versions). CREATE TABLE IF NOT EXISTS + INSERT OR IGNORE
-- rende la migration idempotente anche su DB dove eventualmente file_state fosse
-- stata creata manualmente in precedenza.

CREATE TABLE IF NOT EXISTS file_state (
    path TEXT NOT NULL,
    populator TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    indexed_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (path, populator)
);

CREATE INDEX IF NOT EXISTS idx_file_state_indexed_at ON file_state(indexed_at);
CREATE INDEX IF NOT EXISTS idx_file_state_path ON file_state(path);

INSERT OR IGNORE INTO schema_versions(version) VALUES (75);
