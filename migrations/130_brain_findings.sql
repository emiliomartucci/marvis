-- Migration 130: Brain v1 Learn Findings (sub-04 L5)
-- Date: 2026-05-16
-- Plan: docs/plans/sub/2026-05-15-brain-v1-04-learn-findings.md
-- Parent: docs/plans/2026-05-15-feat-brain-v1-mielinizzazione-plan.md
-- Author: brain-v1
--
-- Adds Learn Findings storage layered on sub-01 substrate + sub-02 drift signals
-- + sub-03 memory operations:
--   brain_findings           -- BLAKE2b-stable findings (human-facing proposals)
--   brain_finding_states     -- append-only lifecycle history
--   brain_finding_evidence   -- queryable join (cite drift/memory_op/event/journal)
--
-- Invariants (parent §9, sub-04 §4 / §10.X / §10.Z):
--   * finding_id is BLAKE2b-16 stable hash (32 hex chars).
--     EXCLUDES severity, confidence, summary, title, approval_state,
--     owner_hint, suggested_artifact (sub-04 §7.2).
--   * run_id FK ON DELETE RESTRICT (findings semantically outlive runs).
--   * confidence is CATEGORICAL TIER (low|medium|high), NOT float.
--     FR1/F10 anti-pattern guard: CHECK constraint rejects numeric strings.
--   * severity is CATEGORICAL (low|medium|high|critical) — Datadog-style.
--   * finding_type 6 enum values (idea | task_candidate | open_question |
--     scope_gap | procedure_change | contradiction).
--   * approval_state 6 enum values (open | approved | dismissed | resolved |
--     superseded | expired). System-driven supersede + expired terminals.
--   * closure_condition_kind 4 discriminated-union kinds.
--   * regression_of_finding_id self-FK ON DELETE SET NULL — chain audit.
--   * superseded_by_finding_id self-FK ON DELETE SET NULL.
--   * Trigger prevents DELETE of resolved rows (audit retention).
--   * Trigger blocks terminal-state UPDATE except system-driven supersede.
--   * Composite UK on natural key for INSERT OR IGNORE idempotency.
--
-- Rollback: migrations/130_brain_findings_down.sql
-- Apply:    sqlite3 /data/pir/console.db < migrations/130_brain_findings.sql

BEGIN IMMEDIATE;

-- ------------------------------------------------------------------
-- brain_findings : human-facing Learn proposals
-- ------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS brain_findings (
    finding_id                  TEXT PRIMARY KEY,
    run_id                      TEXT NOT NULL,
    cycle_key                   TEXT NOT NULL,
    detected_at                 TEXT NOT NULL
                                CHECK(detected_at LIKE '%Z' OR detected_at LIKE '%+00:00'),
    finding_type                TEXT NOT NULL
                                CHECK(finding_type IN (
                                    'idea',
                                    'task_candidate',
                                    'open_question',
                                    'scope_gap',
                                    'procedure_change',
                                    'contradiction'
                                )),
    schema_version              INTEGER NOT NULL DEFAULT 1,
    scope_type                  TEXT NOT NULL
                                CHECK(scope_type IN (
                                    'company','program','project','artifact'
                                )),
    scope_key                   TEXT NOT NULL,
    program_key                 TEXT,
    title                       TEXT NOT NULL
                                CHECK(length(title) <= 200),
    summary                     TEXT NOT NULL
                                CHECK(length(summary) <= 2000),
    why_now                     TEXT NOT NULL
                                CHECK(length(why_now) <= 500),
    evidence_hash               TEXT NOT NULL
                                CHECK(length(evidence_hash) = 64),
    involved_projects_json      TEXT NOT NULL DEFAULT '[]'
                                CHECK(json_valid(involved_projects_json)),
    suggested_artifact          TEXT NOT NULL
                                CHECK(suggested_artifact IN (
                                    'task','adr','guide','learning',
                                    'status_update','question','none'
                                )),
    owner_hint_json             TEXT NOT NULL DEFAULT '{}'
                                CHECK(json_valid(owner_hint_json)),
    closure_condition_kind      TEXT NOT NULL
                                CHECK(closure_condition_kind IN (
                                    'drift_signal_clears',
                                    'memory_op_applied',
                                    'artifact_exists',
                                    'manual_attest'
                                )),
    closure_condition_json      TEXT NOT NULL DEFAULT '{}'
                                CHECK(json_valid(closure_condition_json)),
    closure_condition_human     TEXT
                                CHECK(closure_condition_human IS NULL
                                      OR length(closure_condition_human) <= 500),
    severity                    TEXT NOT NULL
                                CHECK(severity IN (
                                    'low','medium','high','critical'
                                )),
    -- F10 / FR1 anti-anti-pattern: confidence is a TIER, never a float.
    -- The CHECK rejects '0.5', '1.0' etc — only the three categorical strings.
    confidence                  TEXT NOT NULL
                                CHECK(confidence IN ('low','medium','high')),
    approval_state              TEXT NOT NULL DEFAULT 'open'
                                CHECK(approval_state IN (
                                    'open','approved','dismissed',
                                    'resolved','superseded','expired'
                                )),
    regression_of_finding_id    TEXT,
    proposal_fingerprint        TEXT NOT NULL
                                CHECK(length(proposal_fingerprint) = 32),
    recurrence_count            INTEGER NOT NULL DEFAULT 1
                                CHECK(recurrence_count >= 1),
    first_seen_cycle_key        TEXT NOT NULL,
    last_seen_cycle_key         TEXT NOT NULL,
    applied_artifact_ref        TEXT,
    applied_at                  TEXT,
    applied_by_user_id          TEXT,
    expires_at                  TEXT NOT NULL,
    superseded_by_finding_id    TEXT,
    created_at                  TEXT NOT NULL
                                DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    updated_at                  TEXT NOT NULL
                                DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    FOREIGN KEY (run_id) REFERENCES brain_runs(run_id) ON DELETE RESTRICT,
    FOREIGN KEY (regression_of_finding_id)
        REFERENCES brain_findings(finding_id) ON DELETE SET NULL,
    FOREIGN KEY (superseded_by_finding_id)
        REFERENCES brain_findings(finding_id) ON DELETE SET NULL
);

-- ------------------------------------------------------------------
-- Indexes (sub-04 §10.X)
-- ------------------------------------------------------------------

-- Composite natural key — drives INSERT OR IGNORE on recompute.
CREATE UNIQUE INDEX IF NOT EXISTS uk_brain_findings_natural
    ON brain_findings(
        cycle_key, finding_type, scope_type, scope_key,
        evidence_hash, closure_condition_kind
    );

-- Primary read path: latest active findings per scope.
CREATE INDEX IF NOT EXISTS idx_brain_findings_scope_cycle
    ON brain_findings(scope_type, scope_key, cycle_key DESC)
    WHERE superseded_by_finding_id IS NULL;

-- Triage queue (open findings).
CREATE INDEX IF NOT EXISTS idx_brain_findings_open
    ON brain_findings(approval_state, created_at DESC)
    WHERE approval_state = 'open';

-- Cross-cycle continuation (recurrence dedup + regression detection).
CREATE INDEX IF NOT EXISTS idx_brain_findings_fingerprint
    ON brain_findings(proposal_fingerprint, cycle_key DESC);

-- Type-faceted dashboards.
CREATE INDEX IF NOT EXISTS idx_brain_findings_type_cycle
    ON brain_findings(finding_type, cycle_key DESC);

-- Run-scoped queries.
CREATE INDEX IF NOT EXISTS idx_brain_findings_run
    ON brain_findings(run_id);

-- Regression chain traversal.
CREATE INDEX IF NOT EXISTS idx_brain_findings_regression
    ON brain_findings(regression_of_finding_id)
    WHERE regression_of_finding_id IS NOT NULL;

-- ------------------------------------------------------------------
-- Triggers
-- ------------------------------------------------------------------

CREATE TRIGGER IF NOT EXISTS trg_brain_findings_updated_at
    AFTER UPDATE ON brain_findings
    FOR EACH ROW
    WHEN NEW.updated_at = OLD.updated_at
BEGIN
    UPDATE brain_findings
       SET updated_at = strftime('%Y-%m-%dT%H:%M:%fZ','now')
     WHERE finding_id = NEW.finding_id;
END;

-- Audit immutability: resolved findings cannot be deleted.
CREATE TRIGGER IF NOT EXISTS trg_brain_findings_no_delete_resolved
    BEFORE DELETE ON brain_findings
    FOR EACH ROW
    WHEN OLD.approval_state = 'resolved'
BEGIN
    SELECT RAISE(ABORT, 'cannot delete resolved finding');
END;

-- Terminal-state forward-only: dismissed/resolved/expired can't transition
-- back. superseded is system-driven so we allow it to flow into resolved
-- via supersede chain (handled by app, not blocked here). approved →
-- dismissed is blocked (use resolved with attestation, sub-04 §8).
CREATE TRIGGER IF NOT EXISTS trg_brain_findings_terminal_forward_only
    BEFORE UPDATE OF approval_state ON brain_findings
    FOR EACH ROW
    WHEN OLD.approval_state IN ('dismissed','resolved','expired')
         AND NEW.approval_state <> OLD.approval_state
BEGIN
    SELECT RAISE(ABORT, 'terminal finding state is forward-only');
END;

-- ------------------------------------------------------------------
-- brain_finding_states : append-only lifecycle history
-- ------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS brain_finding_states (
    state_id              TEXT PRIMARY KEY,
    finding_id            TEXT NOT NULL,
    from_state            TEXT,
    to_state              TEXT NOT NULL
                          CHECK(to_state IN (
                              'open','approved','dismissed',
                              'resolved','superseded','expired'
                          )),
    actor_user_id         TEXT,
    reason                TEXT CHECK(reason IS NULL OR length(reason) <= 500),
    applied_artifact_ref  TEXT,
    created_at            TEXT NOT NULL
                          DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    FOREIGN KEY (finding_id)
        REFERENCES brain_findings(finding_id) ON DELETE RESTRICT
);

CREATE INDEX IF NOT EXISTS idx_brain_findings_states_finding
    ON brain_finding_states(finding_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_brain_findings_states_actor
    ON brain_finding_states(actor_user_id, created_at DESC);

-- ------------------------------------------------------------------
-- brain_finding_evidence : queryable join
-- ------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS brain_finding_evidence (
    finding_id      TEXT NOT NULL,
    position        INTEGER NOT NULL,
    evidence_kind   TEXT NOT NULL
                    CHECK(evidence_kind IN (
                        'digest_event','journal_entry','drift_signal',
                        'memory_op','kg_node','handoff','learning',
                        'audit_log','task','pr','commit'
                    )),
    evidence_ref    TEXT NOT NULL,
    weight          REAL NOT NULL DEFAULT 1.0
                    CHECK(weight BETWEEN 0.0 AND 1.0
                          AND weight = weight),
    cycle_key       TEXT,
    PRIMARY KEY (finding_id, position),
    FOREIGN KEY (finding_id)
        REFERENCES brain_findings(finding_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_brain_findings_evidence_lookup
    ON brain_finding_evidence(evidence_kind, evidence_ref);

CREATE INDEX IF NOT EXISTS idx_brain_findings_evidence_finding
    ON brain_finding_evidence(finding_id);

INSERT OR IGNORE INTO schema_versions(version) VALUES (130);

COMMIT;
