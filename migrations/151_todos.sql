-- Migration 151 - todos subsystem backend (GUI v1 P0).
--
-- ADDITIVE, costo costante: 1 CREATE TABLE + 3 indici. Nessuna riga esistente
-- toccata. VERIFY prod schema_versions max prima del merge: prod = 150 -> questo file e' 151.
--
-- Modello: una riga = un todo persistito leggero. Gli item "approva" sono
-- proiezioni virtuali read-only dalle code reali, quindi la tabella non diventa
-- un secondo source of truth per PR/finding/memory-op in attesa.

CREATE TABLE IF NOT EXISTS todos (
    id             TEXT PRIMARY KEY,
    type           TEXT NOT NULL CHECK (type IN (
                     'promemoria','azione','idea','decidi','approva','rivedi'
                   )),
    family         TEXT NOT NULL CHECK (family IN ('captured','system')),
    status         TEXT NOT NULL DEFAULT 'aperto',
    text           TEXT NOT NULL,
    payload        TEXT,
    fu             TEXT NOT NULL,
    project        TEXT,
    source         TEXT NOT NULL DEFAULT 'user',
    source_ref     TEXT,
    doer           TEXT CHECK (doer IN ('human','agent','hybrid')),
    linked_task_id TEXT,
    created_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL,
    resolved_at    TEXT
);

CREATE INDEX IF NOT EXISTS idx_todos_open ON todos(status, fu);
CREATE INDEX IF NOT EXISTS idx_todos_project ON todos(project);
CREATE UNIQUE INDEX IF NOT EXISTS idx_todos_dedup
    ON todos(source, source_ref) WHERE source_ref IS NOT NULL;
