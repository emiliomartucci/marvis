-- Migration 076 — KG watcher state (observability tabella per Phase 2 daemon)
-- Reversibile: vedi 076_kg_watcher_state_down.sql
-- Dipendenze: nessuna (tabella standalone)
--
-- Scopo: esporre stato del kg-watcher daemon (Phase 2) al monitoring/session-check
-- senza dover parsare log. sd_notify gestisce la LIVENESS (systemd watchdog).
-- Questa tabella contiene i RICH DATA: last_flush_at + ring buffer recenti
-- (fino a N entries) di skipped e flushes per debug agent-accessible.
--
-- Single-row table: CHECK(id=1) enforce un'unica riga. Il daemon fa UPSERT
-- sulla stessa row ad ogni flush. Evita crescita illimitata e rende le query
-- O(1) (seek per PK).
--
-- Pattern (kb/agent-native-parity.md): un agent deve poter rispondere a
-- "stato del watcher?" via mcp__pir__get_monitoring senza toccare file.

CREATE TABLE IF NOT EXISTS kg_watcher_state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    last_flush_at TEXT,
    last_flush_files_processed INTEGER NOT NULL DEFAULT 0,
    last_flush_edges_written INTEGER NOT NULL DEFAULT 0,
    last_flush_duration_ms REAL NOT NULL DEFAULT 0.0,
    recent_skipped TEXT NOT NULL DEFAULT '[]',
    recent_flushes TEXT NOT NULL DEFAULT '[]',
    total_flushes INTEGER NOT NULL DEFAULT 0,
    total_files_processed INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Seed single row so UPSERT by id=1 always finds a target.
INSERT OR IGNORE INTO kg_watcher_state (id) VALUES (1);

INSERT OR IGNORE INTO schema_versions(version) VALUES (76);
