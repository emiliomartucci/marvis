-- Migration 129: Brain v1 Memory Operations (sub-03 L4)
-- Date: 2026-05-16
-- Plan: docs/plans/sub/2026-05-15-brain-v1-03-memory-operations.md
-- Parent: docs/plans/2026-05-15-feat-brain-v1-mielinizzazione-plan.md
-- Author: brain-v1
--
-- Adds memory operations storage layered on sub-01 substrate + sub-02 drift signals:
--   brain_memory_operations         -- BLAKE2b-stable mielinizzazione proposals
--   brain_memory_operation_states   -- append-only lifecycle history
--   brain_memory_operation_evidence -- queryable join (cite digest/journal/drift)
--
-- Invariants (parent §9, sub-03 §4 / §10.X):
--   * operation_id is BLAKE2b-16 stable hash. EXCLUDES severity, confidence,
--     summary, approval_state, owner_hint, suggested_artifact.
--   * run_id FK ON DELETE RESTRICT (operations semantically outlive runs).
--   * No FK into mutable substrate (tasks/pull_requests/handoffs/learnings).
--   * requires_approval = 1 CHECK constraint enforced (v1 invariant).
--   * 8 active operation_type CHECK enum (M1-M7 + cascade_rollup +
--     compression_candidate per §11.5 CE3). 2 reserved literals deferred
--     (deduplicate, promotion_candidate) — not in CHECK.
--   * 8 target_type CHECK enum: none/task/guide/adr/learning/kg_edge_metric
--     /doc_patch + context_md_append (CE3 M8 new).
--   * Self-loop CHECK rejects consolidate(X,X) / supersede_candidate(X,X).
--   * Trigger prevents DELETE of applied/reverted rows.
--   * Composite UK on natural key for INSERT OR IGNORE idempotency.
--
-- Rollback: migrations/129_brain_memory_operations_down.sql
-- Apply:    sqlite3 /data/pir/console.db < migrations/129_brain_memory_operations.sql

BEGIN IMMEDIATE;

-- ------------------------------------------------------------------
-- brain_memory_operations : append-only mielinizzazione proposals
-- ------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS brain_memory_operations (
    operation_id                TEXT PRIMARY KEY,
    run_id                      TEXT NOT NULL,
    cycle_key                   TEXT NOT NULL,
    detected_at                 TEXT NOT NULL
                                CHECK(detected_at LIKE '%Z' OR detected_at LIKE '%+00:00'),
    operation_type              TEXT NOT NULL
                                CHECK(operation_type IN (
                                    'reinforce',
                                    'consolidate',
                                    'supersede_candidate',
                                    'provenance_hardening',
                                    'orphan_detected',
                                    'contradiction_detected',
                                    'cascade_rollup',
                                    'compression_candidate'
                                )),
    schema_version              INTEGER NOT NULL DEFAULT 1,
    scope_type                  TEXT NOT NULL
                                CHECK(scope_type IN (
                                    'company','program','project','artifact'
                                )),
    scope_key                   TEXT NOT NULL,
    program_key                 TEXT,
    source_ref                  TEXT NOT NULL,
    target_ref                  TEXT NOT NULL DEFAULT '',
    -- NaN guard: score = score is false only when value is NaN.
    score                       REAL NOT NULL
                                CHECK(score BETWEEN 0.0 AND 1.0
                                      AND score = score),
    recurrence_key              TEXT NOT NULL,
    recurrence_count            INTEGER NOT NULL DEFAULT 1
                                CHECK(recurrence_count >= 1),
    first_seen_cycle_key        TEXT NOT NULL,
    last_seen_cycle_key         TEXT NOT NULL,
    involved_projects_json      TEXT NOT NULL DEFAULT '[]'
                                CHECK(json_valid(involved_projects_json)),
    evidence_hash               TEXT NOT NULL
                                CHECK(length(evidence_hash) = 64),
    summary                     TEXT NOT NULL
                                CHECK(length(summary) <= 2000),
    proposed_write_target_type  TEXT NOT NULL
                                CHECK(proposed_write_target_type IN (
                                    'none',
                                    'task',
                                    'guide',
                                    'adr',
                                    'learning',
                                    'kg_edge_metric',
                                    'doc_patch',
                                    'context_md_append'
                                )),
    proposed_write_json         TEXT NOT NULL DEFAULT '{}'
                                CHECK(json_valid(proposed_write_json)),
    -- v1 invariant: requires_approval is always 1. Relax via future migration.
    requires_approval           INTEGER NOT NULL DEFAULT 1
                                CHECK(requires_approval = 1),
    approval_state              TEXT NOT NULL DEFAULT 'pending'
                                CHECK(approval_state IN (
                                    'pending','approved','rejected',
                                    'dismissed','superseded','expired',
                                    'applied','reverted'
                                )),
    expires_at                  TEXT NOT NULL,
    superseded_by_operation_id  TEXT,
    applied_at                  TEXT,
    applied_by_user_id          TEXT,
    applied_artifact_ref        TEXT,
    created_at                  TEXT NOT NULL
                                DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    updated_at                  TEXT NOT NULL
                                DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    -- Self-loop guard: consolidate/supersede_candidate cannot target their own source.
    CHECK(
        target_ref = ''
        OR target_ref != source_ref
        OR operation_type NOT IN ('consolidate', 'supersede_candidate')
    ),
    FOREIGN KEY (run_id) REFERENCES brain_runs(run_id) ON DELETE RESTRICT,
    FOREIGN KEY (superseded_by_operation_id)
        REFERENCES brain_memory_operations(operation_id) ON DELETE SET NULL
);

-- ------------------------------------------------------------------
-- Indexes (sub-03 §10.X)
-- ------------------------------------------------------------------

-- Composite natural key — drives INSERT OR IGNORE on recompute.
CREATE UNIQUE INDEX IF NOT EXISTS uk_brain_mo_natural
    ON brain_memory_operations(
        cycle_key, operation_type, scope_type, scope_key,
        source_ref, target_ref, evidence_hash
    );

-- Primary read path: latest active ops per scope.
CREATE INDEX IF NOT EXISTS idx_brain_mo_scope_cycle
    ON brain_memory_operations(scope_type, scope_key, cycle_key DESC)
    WHERE superseded_by_operation_id IS NULL;

-- Triage queue (pending operations).
CREATE INDEX IF NOT EXISTS idx_brain_mo_pending
    ON brain_memory_operations(approval_state, created_at DESC)
    WHERE approval_state = 'pending';

-- Cross-cycle continuation (recurrence dedup).
CREATE INDEX IF NOT EXISTS idx_brain_mo_recurrence
    ON brain_memory_operations(recurrence_key, cycle_key DESC);

-- Type-faceted dashboards.
CREATE INDEX IF NOT EXISTS idx_brain_mo_type_cycle
    ON brain_memory_operations(operation_type, cycle_key DESC);

-- Run-scoped queries.
CREATE INDEX IF NOT EXISTS idx_brain_mo_run
    ON brain_memory_operations(run_id);

-- ------------------------------------------------------------------
-- Triggers
-- ------------------------------------------------------------------

CREATE TRIGGER IF NOT EXISTS trg_brain_mo_updated_at
    AFTER UPDATE ON brain_memory_operations
    FOR EACH ROW
    WHEN NEW.updated_at = OLD.updated_at
BEGIN
    UPDATE brain_memory_operations
       SET updated_at = strftime('%Y-%m-%dT%H:%M:%fZ','now')
     WHERE operation_id = NEW.operation_id;
END;

-- Audit immutability: applied/reverted ops cannot be deleted.
CREATE TRIGGER IF NOT EXISTS trg_brain_mo_no_delete_applied
    BEFORE DELETE ON brain_memory_operations
    FOR EACH ROW
    WHEN OLD.approval_state IN ('applied', 'reverted')
BEGIN
    SELECT RAISE(ABORT, 'cannot delete applied or reverted memory operation');
END;

-- ------------------------------------------------------------------
-- brain_memory_operation_states : append-only lifecycle history
-- ------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS brain_memory_operation_states (
    state_id              TEXT PRIMARY KEY,
    operation_id          TEXT NOT NULL,
    from_state            TEXT,
    to_state              TEXT NOT NULL
                          CHECK(to_state IN (
                              'pending','approved','rejected',
                              'dismissed','superseded','expired',
                              'applied','reverted'
                          )),
    actor_user_id         TEXT,
    reason                TEXT CHECK(reason IS NULL OR length(reason) <= 500),
    applied_artifact_ref  TEXT,
    created_at            TEXT NOT NULL
                          DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    FOREIGN KEY (operation_id)
        REFERENCES brain_memory_operations(operation_id) ON DELETE RESTRICT
);

CREATE INDEX IF NOT EXISTS idx_brain_mo_states_op
    ON brain_memory_operation_states(operation_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_brain_mo_states_actor
    ON brain_memory_operation_states(actor_user_id, created_at DESC);

-- ------------------------------------------------------------------
-- brain_memory_operation_evidence : queryable join
-- ------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS brain_memory_operation_evidence (
    operation_id    TEXT NOT NULL,
    position        INTEGER NOT NULL,
    evidence_kind   TEXT NOT NULL
                    CHECK(evidence_kind IN (
                        'digest_event','journal_entry','drift_signal',
                        'kg_node','handoff','learning','audit_log',
                        'task','pr','commit'
                    )),
    evidence_ref    TEXT NOT NULL,
    weight          REAL NOT NULL DEFAULT 1.0
                    CHECK(weight BETWEEN 0.0 AND 1.0
                          AND weight = weight),
    cycle_key       TEXT,
    PRIMARY KEY (operation_id, position),
    FOREIGN KEY (operation_id)
        REFERENCES brain_memory_operations(operation_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_brain_mo_evidence_lookup
    ON brain_memory_operation_evidence(evidence_kind, evidence_ref);

CREATE INDEX IF NOT EXISTS idx_brain_mo_evidence_op
    ON brain_memory_operation_evidence(operation_id);

INSERT OR IGNORE INTO schema_versions(version) VALUES (129);

COMMIT;
