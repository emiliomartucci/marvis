-- Migration 149 — KG freshness/trust (Fase C): superseded_by + last_verified_at.
--
-- ADDITIVE, costo costante: 2 ALTER ADD COLUMN nullable (no default-espressione,
-- no backfill, no index). Nessuna riga viene riscritta → sicura anche a milioni
-- di righe. VERIFY prod schema_versions max prima del merge: prod = 148, repo
-- max = 148 → questo file e' 149 (il runner applica solo version > MAX).
--
-- Il grafo ha gia' l'asse temporale (mig 067): graph_edges.valid_until (NULL =
-- valido), graph_nodes.deprecated_at (NULL = vivo), first_seen_at/last_seen_at,
-- + il filtro dedicato _temporal_where_and_params (graph_service.py, sempre
-- attivo). Questa migration NON ridefinisce quel modello: aggiunge SOLO i due
-- attributi che mancano per il manutentore Brain (Fase D) e per il surface di
-- freschezza (Fase A):
--
--   * graph_edges.superseded_by  — puntatore di audit (id dell'arco che rimpiazza
--     questo). Modello SOFT, in-place: l'invalidazione resta valid_until (mig 067),
--     superseded_by registra solo la catena. NON serve far coesistere due righe per
--     la stessa tripla (la UNIQUE(source,target,relation) resta intatta → niente
--     table rebuild): una sostituzione su target diverso e' gia' una tripla diversa
--     = riga distinta; un'invalidazione pura setta valid_until sulla riga esistente.
--   * graph_nodes.last_verified_at — ISO-8601 UTC, quando il Brain ha ri-confermato
--     il nodo (REINFORCE). La freschezza read-time fara' COALESCE(last_verified_at,
--     last_seen_at, ...) — wiring in Fase D, insieme allo scrittore.
--
-- Semantica NULL = "mai superato / mai verificato esplicitamente" (fast path).
-- Backfill: NESSUNO (legacy resta NULL; copiare created_at sarebbe ingest-time, non
-- validita' reale, + mass-UPDATE inutile). Mirror del pattern audit di mig 148.
--
-- Gate: nessun read/write path consuma queste colonne finche' Fase D non atterra,
-- e tutto e' dietro MARVIS_TEMPORAL_MEMORY → flag-off invariato (le colonne stanno
-- NULL). Reversibile: 149_kg_trust_columns_down.sql (DROP COLUMN, SQLite >= 3.35).
--
-- BEGIN IMMEDIATE wrap (come mig 067, stesse tabelle): 2 ALTER atomici, niente
-- mezza-migration su crash.

BEGIN IMMEDIATE;

ALTER TABLE graph_edges ADD COLUMN superseded_by TEXT;     -- id dell'arco che lo rimpiazza (audit chain); NULL = non superato
ALTER TABLE graph_nodes ADD COLUMN last_verified_at TEXT;  -- ISO-8601 UTC: ultima ri-conferma Brain; NULL = mai verificato

INSERT OR IGNORE INTO schema_versions (version) VALUES (149);

COMMIT;
