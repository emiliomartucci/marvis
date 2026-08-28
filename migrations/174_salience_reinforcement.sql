-- Migration 174 — Fase 2 mielinizzazione: salience reinforcement ledger
--
-- Plan 2026-08-16 "Fase 2 mielinizzazione minima" (v3), unit U1. Two additive
-- tables, zero ALTER on documents (the mig-136 documents_fts triggers must
-- never fire from this feature — R3: zero writes to documents anywhere in the
-- reinforcement/decay flow).
--
-- salience_boosts — APPEND-ONLY ledger (R1/KTD5). One row per accepted boost:
--   * actor            = authenticated principal (NOT NULL, never a tool
--                        parameter);
--   * agent_name       = optional self-declared label, telemetry only,
--                        UNTRUSTED;
--   * provenance       CHECK ∈ {human, agent, outcome} — retrieval traffic is
--                        NOT a provenance by design (KTD1);
--   * weight           = signed contribution (misled < 0);
--   * doc_content_hash = documents.content_hash at boost time (the doc-version
--                        snapshot: documents has NO updated_at). The read path
--                        excludes stale MISLED rows whose recorded hash no
--                        longer matches the doc (R10); a confidential purge
--                        NULLs the doc hash, so pre-purge misled decays out of
--                        the read path automatically.
--   * note             = optional caller note, ALREADY REDACTED at write time
--                        (R5: max 300 chars, secret-like spans replaced with
--                        [REDACTED] by the U3 gate). Surfaced by the U5
--                        per-doc audit as an untrusted quote with
--                        actor+timestamp — never re-rendered as instructions.
--   No UPDATE ever; DELETE only from the retention sweep and from the
--   purge/confidential + learning-delete cascades (same writer tx as the other
--   derivatives — see confidential_files._purge_index_rows and
--   learnings._prune_search_index).
--
-- Read-path decay (2^(−age_days/half_life)) is computed in APPLICATION code on
-- the candidate set only — no pow() in SQLite on the hot path (U2). The
-- (doc_id, created_at) index serves that candidate-set aggregation; the
-- (actor, created_at) index serves the sliding-window anti-gaming caps (R7:
-- 3/principal/hour, 1/doc/day/principal, 3 distinct principals/doc/day), which
-- are all derived by COUNT queries over THIS table — accepted boosts are the
-- only rows that consume cap. No separate counter table is needed for caps.
--
-- boost_rejects — REJECTS ONLY (design decision, documented per plan U1):
-- since every ACCEPTED boost is already a salience_boosts row, all three cap
-- windows are derivable from salience_boosts with the indexes above. The
-- reject log therefore records only the over-cap rejections ("risposta ok,
-- nessuna riga boost, rigetto contato" — R7) so the R13 metrics (reject
-- counts, per-doc audit) and the reconciliation accepted−appended−rejected=0
-- have a durable source. reject_reason is free text owned by the U3 gate
-- (expected values: agent_hourly_cap, agent_doc_daily_cap,
-- doc_distinct_daily_cap, human_rate_limited) — no CHECK so adding a reason
-- never needs a migration.
--
-- NAMING: the plan drafted this table as ``boost_log``, but ``boost_log``
-- ALREADY EXISTS since migration 046 (doc_id, caller_id, boosted_at) and is
-- LIVE as the rate-limit marker for the legacy REST boost/decay endpoints
-- (core/api/routers/documents.py) — the very "endpoint REST residuo" the plan
-- flags for U3 preflight. An additive-only migration cannot redefine its
-- shape, so the Fase-2 reject log lands as ``boost_rejects`` and the legacy
-- table stays untouched until U3 migrates that rate-limit.
--
-- Reversible: migrations/174_salience_reinforcement_down.sql.
-- Ships in the same PR as the CANONICAL_* bump in
-- core/hosted_deploy/schema_preflight.py (155 / 162 / 174).

BEGIN IMMEDIATE;

CREATE TABLE IF NOT EXISTS salience_boosts (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    doc_id           INTEGER NOT NULL,
    actor            TEXT    NOT NULL,
    agent_name       TEXT,
    provenance       TEXT    NOT NULL CHECK (provenance IN ('human', 'agent', 'outcome')),
    weight           REAL    NOT NULL,
    doc_content_hash TEXT,
    note             TEXT,
    created_at       TEXT    NOT NULL DEFAULT (datetime('now', 'utc'))
);

CREATE INDEX IF NOT EXISTS idx_salience_boosts_doc_created
    ON salience_boosts (doc_id, created_at);

CREATE INDEX IF NOT EXISTS idx_salience_boosts_actor_created
    ON salience_boosts (actor, created_at);

CREATE TABLE IF NOT EXISTS boost_rejects (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    doc_id        INTEGER NOT NULL,
    actor         TEXT    NOT NULL,
    agent_name    TEXT,
    provenance    TEXT    NOT NULL CHECK (provenance IN ('human', 'agent', 'outcome')),
    reject_reason TEXT    NOT NULL,
    created_at    TEXT    NOT NULL DEFAULT (datetime('now', 'utc'))
);

CREATE INDEX IF NOT EXISTS idx_boost_rejects_doc_created
    ON boost_rejects (doc_id, created_at);

INSERT OR IGNORE INTO schema_versions (version) VALUES (174);

COMMIT;
