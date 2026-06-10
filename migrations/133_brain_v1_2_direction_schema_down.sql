-- Migration 133 DOWN: Revert Brain v1.2 Direction Integration schema
-- Date: 2026-05-18
--
-- WARNING: Down migration may lose data if production rows used new enum
-- values (direction_drift / direction_bootstrap finding_type, DR8 rule_id,
-- direction_misalignment signal_type, pending_bootstrap / applied
-- approval_state). The down script will REMOVE such rows before swap.

PRAGMA foreign_keys = OFF;

BEGIN IMMEDIATE;

-- =============================================================================
-- 1. Drop new tables
-- =============================================================================
DROP TABLE IF EXISTS direction_changelog;
DROP TRIGGER IF EXISTS trg_project_directions_updated_at;
DROP TABLE IF EXISTS project_directions;

-- =============================================================================
-- 2. Revert brain_findings (remove new enum values + new columns)
-- =============================================================================

-- Remove rows that depend on new enum values (data loss accepted on rollback)
DELETE FROM brain_findings
 WHERE finding_type IN ('direction_drift', 'direction_bootstrap')
    OR approval_state IN ('pending_bootstrap', 'applied');

DROP TRIGGER IF EXISTS trg_brain_findings_updated_at;
DROP TRIGGER IF EXISTS trg_brain_findings_no_delete_resolved;
DROP TRIGGER IF EXISTS trg_brain_findings_terminal_forward_only;

DROP INDEX IF EXISTS uk_brain_findings_natural;
DROP INDEX IF EXISTS idx_brain_findings_scope_cycle;
DROP INDEX IF EXISTS idx_brain_findings_open;
DROP INDEX IF EXISTS idx_brain_findings_fingerprint;
DROP INDEX IF EXISTS idx_brain_findings_type_cycle;
DROP INDEX IF EXISTS idx_brain_findings_run;
DROP INDEX IF EXISTS idx_brain_findings_regression;
DROP INDEX IF EXISTS idx_brain_findings_entity_open;
DROP INDEX IF EXISTS idx_brain_findings_urgency;

CREATE TABLE brain_findings_old (
    finding_id                  TEXT PRIMARY KEY,
    run_id                      TEXT NOT NULL,
    cycle_key                   TEXT NOT NULL,
    detected_at                 TEXT NOT NULL
                                CHECK(detected_at LIKE '%Z' OR detected_at LIKE '%+00:00'),
    finding_type                TEXT NOT NULL
                                CHECK(finding_type IN (
                                    'idea','task_candidate','open_question',
                                    'scope_gap','procedure_change','contradiction'
                                )),
    schema_version              INTEGER NOT NULL DEFAULT 1,
    scope_type                  TEXT NOT NULL
                                CHECK(scope_type IN ('company','program','project','artifact')),
    scope_key                   TEXT NOT NULL,
    program_key                 TEXT,
    title                       TEXT NOT NULL CHECK(length(title) <= 200),
    summary                     TEXT NOT NULL CHECK(length(summary) <= 2000),
    why_now                     TEXT NOT NULL CHECK(length(why_now) <= 500),
    evidence_hash               TEXT NOT NULL CHECK(length(evidence_hash) = 64),
    involved_projects_json      TEXT NOT NULL DEFAULT '[]' CHECK(json_valid(involved_projects_json)),
    suggested_artifact          TEXT NOT NULL
                                CHECK(suggested_artifact IN (
                                    'task','adr','guide','learning','status_update','question','none'
                                )),
    owner_hint_json             TEXT NOT NULL DEFAULT '{}' CHECK(json_valid(owner_hint_json)),
    closure_condition_kind      TEXT NOT NULL
                                CHECK(closure_condition_kind IN (
                                    'drift_signal_clears','memory_op_applied','artifact_exists','manual_attest'
                                )),
    closure_condition_json      TEXT NOT NULL DEFAULT '{}' CHECK(json_valid(closure_condition_json)),
    closure_condition_human     TEXT CHECK(closure_condition_human IS NULL OR length(closure_condition_human) <= 500),
    severity                    TEXT NOT NULL CHECK(severity IN ('low','medium','high','critical')),
    confidence                  TEXT NOT NULL CHECK(confidence IN ('low','medium','high')),
    approval_state              TEXT NOT NULL DEFAULT 'open'
                                CHECK(approval_state IN ('open','approved','dismissed','resolved','superseded','expired')),
    regression_of_finding_id    TEXT,
    proposal_fingerprint        TEXT NOT NULL CHECK(length(proposal_fingerprint) = 32),
    recurrence_count            INTEGER NOT NULL DEFAULT 1 CHECK(recurrence_count >= 1),
    first_seen_cycle_key        TEXT NOT NULL,
    last_seen_cycle_key         TEXT NOT NULL,
    applied_artifact_ref        TEXT,
    applied_at                  TEXT,
    applied_by_user_id          TEXT,
    expires_at                  TEXT NOT NULL,
    superseded_by_finding_id    TEXT,
    created_at                  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    updated_at                  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    FOREIGN KEY (run_id) REFERENCES brain_runs(run_id) ON DELETE RESTRICT,
    FOREIGN KEY (regression_of_finding_id) REFERENCES brain_findings_old(finding_id) ON DELETE SET NULL,
    FOREIGN KEY (superseded_by_finding_id) REFERENCES brain_findings_old(finding_id) ON DELETE SET NULL
);

INSERT INTO brain_findings_old (
    finding_id, run_id, cycle_key, detected_at, finding_type, schema_version,
    scope_type, scope_key, program_key, title, summary, why_now, evidence_hash,
    involved_projects_json, suggested_artifact, owner_hint_json,
    closure_condition_kind, closure_condition_json, closure_condition_human,
    severity, confidence, approval_state, regression_of_finding_id,
    proposal_fingerprint, recurrence_count, first_seen_cycle_key,
    last_seen_cycle_key, applied_artifact_ref, applied_at, applied_by_user_id,
    expires_at, superseded_by_finding_id, created_at, updated_at
)
SELECT
    finding_id, run_id, cycle_key, detected_at, finding_type, schema_version,
    scope_type, scope_key, program_key, title, summary, why_now, evidence_hash,
    involved_projects_json, suggested_artifact, owner_hint_json,
    closure_condition_kind, closure_condition_json, closure_condition_human,
    severity, confidence, approval_state, regression_of_finding_id,
    proposal_fingerprint, recurrence_count, first_seen_cycle_key,
    last_seen_cycle_key, applied_artifact_ref, applied_at, applied_by_user_id,
    expires_at, superseded_by_finding_id, created_at, updated_at
FROM brain_findings;

DROP TABLE brain_findings;
ALTER TABLE brain_findings_old RENAME TO brain_findings;

CREATE UNIQUE INDEX uk_brain_findings_natural
    ON brain_findings(cycle_key, finding_type, scope_type, scope_key, evidence_hash, closure_condition_kind);
CREATE INDEX idx_brain_findings_scope_cycle
    ON brain_findings(scope_type, scope_key, cycle_key DESC) WHERE superseded_by_finding_id IS NULL;
CREATE INDEX idx_brain_findings_open
    ON brain_findings(approval_state, created_at DESC) WHERE approval_state = 'open';
CREATE INDEX idx_brain_findings_fingerprint
    ON brain_findings(proposal_fingerprint, cycle_key DESC);
CREATE INDEX idx_brain_findings_type_cycle
    ON brain_findings(finding_type, cycle_key DESC);
CREATE INDEX idx_brain_findings_run
    ON brain_findings(run_id);
CREATE INDEX idx_brain_findings_regression
    ON brain_findings(regression_of_finding_id) WHERE regression_of_finding_id IS NOT NULL;

CREATE TRIGGER trg_brain_findings_updated_at
    AFTER UPDATE ON brain_findings FOR EACH ROW WHEN NEW.updated_at = OLD.updated_at
BEGIN
    UPDATE brain_findings SET updated_at = strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE finding_id = NEW.finding_id;
END;
CREATE TRIGGER trg_brain_findings_no_delete_resolved
    BEFORE DELETE ON brain_findings FOR EACH ROW WHEN OLD.approval_state = 'resolved'
BEGIN
    SELECT RAISE(ABORT, 'cannot delete resolved finding');
END;
CREATE TRIGGER trg_brain_findings_terminal_forward_only
    BEFORE UPDATE OF approval_state ON brain_findings FOR EACH ROW
    WHEN OLD.approval_state IN ('dismissed','resolved','expired') AND NEW.approval_state <> OLD.approval_state
BEGIN
    SELECT RAISE(ABORT, 'terminal finding state is forward-only');
END;

-- =============================================================================
-- 2b. Revert brain_finding_states to_state CHECK
-- =============================================================================

DELETE FROM brain_finding_states WHERE to_state IN ('pending_bootstrap', 'applied');

DROP INDEX IF EXISTS idx_brain_findings_states_finding;
DROP INDEX IF EXISTS idx_brain_findings_states_actor;

CREATE TABLE brain_finding_states_old (
    state_id              TEXT PRIMARY KEY,
    finding_id            TEXT NOT NULL,
    from_state            TEXT,
    to_state              TEXT NOT NULL
                          CHECK(to_state IN ('open','approved','dismissed','resolved','superseded','expired')),
    actor_user_id         TEXT,
    reason                TEXT CHECK(reason IS NULL OR length(reason) <= 500),
    applied_artifact_ref  TEXT,
    created_at            TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    FOREIGN KEY (finding_id) REFERENCES brain_findings(finding_id) ON DELETE RESTRICT
);

INSERT INTO brain_finding_states_old SELECT * FROM brain_finding_states;
DROP TABLE brain_finding_states;
ALTER TABLE brain_finding_states_old RENAME TO brain_finding_states;

CREATE INDEX idx_brain_findings_states_finding ON brain_finding_states(finding_id, created_at DESC);
CREATE INDEX idx_brain_findings_states_actor ON brain_finding_states(actor_user_id, created_at DESC);

-- =============================================================================
-- 3. Revert brain_drift_signals (remove DR8 + direction_misalignment)
-- =============================================================================

DELETE FROM brain_drift_signals
 WHERE rule_id = 'DR8' OR signal_type = 'direction_misalignment';

DROP TRIGGER IF EXISTS trg_brain_drift_signals_updated_at;
DROP INDEX IF EXISTS idx_brain_drift_signals_scope_cycle;
DROP INDEX IF EXISTS idx_brain_drift_signals_scope_type;
DROP INDEX IF EXISTS idx_brain_drift_signals_type_cycle;
DROP INDEX IF EXISTS idx_brain_drift_signals_observed_ref;
DROP INDEX IF EXISTS idx_brain_drift_signals_run;
DROP INDEX IF EXISTS idx_brain_drift_signals_recurrence;
DROP INDEX IF EXISTS idx_brain_drift_signals_open_severity;
DROP INDEX IF EXISTS idx_brain_drift_signals_lookback;
DROP INDEX IF EXISTS idx_brain_drift_signals_axis;

CREATE TABLE brain_drift_signals_old (
    signal_id                   TEXT PRIMARY KEY,
    run_id                      TEXT NOT NULL,
    cycle_key                   TEXT NOT NULL,
    detected_at                 TEXT NOT NULL CHECK(detected_at LIKE '%Z' OR detected_at LIKE '%+00:00'),
    rule_id                     TEXT NOT NULL CHECK(rule_id IN ('DR1','DR2','DR3','DR4','DR5','DR6','DR7')),
    schema_version              INTEGER NOT NULL DEFAULT 1,
    scope_type                  TEXT NOT NULL CHECK(scope_type IN ('company','program','project')),
    scope_key                   TEXT NOT NULL,
    program_key                 TEXT,
    signal_type                 TEXT NOT NULL
                                CHECK(signal_type IN (
                                    'activity_without_status','decision_without_adr','playbook_changed',
                                    'stale_open_loop','docs_governance_drift','external_update_unpropagated',
                                    'claimed_decision_gap'
                                )),
    knowledge_form              TEXT NOT NULL
                                CHECK(knowledge_form IN ('adr','spec','playbook','tribal_memory','external_update','claimed_decision','unknown')),
    classifier_version          INTEGER NOT NULL DEFAULT 1,
    expected_direction_source   TEXT NOT NULL
                                CHECK(expected_direction_source IN (
                                    'journal','project_status','handoff','doc','brainstorm','meeting_transcript','none','task','pr','commit'
                                )),
    expected_direction_ref      TEXT,
    observed_direction_ref      TEXT NOT NULL,
    observed_delta              TEXT NOT NULL CHECK(length(observed_delta) <= 2000),
    evidence_json               TEXT NOT NULL DEFAULT '[]' CHECK(json_valid(evidence_json)),
    evidence_hash               TEXT NOT NULL CHECK(length(evidence_hash) = 64),
    severity                    TEXT NOT NULL CHECK(severity IN ('low','medium','high','critical')),
    confidence                  REAL NOT NULL CHECK(confidence BETWEEN 0.0 AND 1.0 AND confidence = confidence),
    recurrence_key              TEXT NOT NULL,
    involved_projects_json      TEXT NOT NULL DEFAULT '[]' CHECK(json_valid(involved_projects_json)),
    state                       TEXT NOT NULL DEFAULT 'open' CHECK(state IN ('open','superseded','resolved','dismissed')),
    superseded_by_signal_id     TEXT,
    resolved_at                 TEXT,
    dismissed_at                TEXT,
    dismissed_by                TEXT,
    dismiss_reason              TEXT,
    drift_axis                  TEXT CHECK(drift_axis IS NULL OR drift_axis IN ('intent','context','both')),
    created_at                  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    updated_at                  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    FOREIGN KEY (run_id) REFERENCES brain_runs(run_id) ON DELETE RESTRICT,
    FOREIGN KEY (superseded_by_signal_id) REFERENCES brain_drift_signals_old(signal_id)
);

INSERT INTO brain_drift_signals_old SELECT * FROM brain_drift_signals;
DROP TABLE brain_drift_signals;
ALTER TABLE brain_drift_signals_old RENAME TO brain_drift_signals;

CREATE INDEX idx_brain_drift_signals_scope_cycle ON brain_drift_signals(scope_type, scope_key, cycle_key DESC);
CREATE INDEX idx_brain_drift_signals_scope_type ON brain_drift_signals(scope_type, scope_key, cycle_key DESC, signal_type);
CREATE INDEX idx_brain_drift_signals_type_cycle ON brain_drift_signals(signal_type, cycle_key DESC);
CREATE INDEX idx_brain_drift_signals_observed_ref ON brain_drift_signals(observed_direction_ref, signal_type, cycle_key DESC);
CREATE INDEX idx_brain_drift_signals_run ON brain_drift_signals(run_id);
CREATE INDEX idx_brain_drift_signals_recurrence ON brain_drift_signals(recurrence_key, cycle_key DESC);
CREATE INDEX idx_brain_drift_signals_open_severity ON brain_drift_signals(severity, cycle_key DESC)
    WHERE state = 'open' AND superseded_by_signal_id IS NULL;
CREATE INDEX idx_brain_drift_signals_lookback ON brain_drift_signals(scope_type, scope_key, signal_type, resolved_at)
    WHERE resolved_at IS NULL AND state = 'open';
CREATE INDEX idx_brain_drift_signals_axis ON brain_drift_signals(cycle_key, drift_axis) WHERE drift_axis IS NOT NULL;

CREATE TRIGGER trg_brain_drift_signals_updated_at
    AFTER UPDATE ON brain_drift_signals FOR EACH ROW WHEN NEW.updated_at = OLD.updated_at
BEGIN
    UPDATE brain_drift_signals SET updated_at = strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE signal_id = NEW.signal_id;
END;

-- =============================================================================
-- 4. Remove schema_versions entry
-- =============================================================================
DELETE FROM schema_versions WHERE version = 133;

COMMIT;

PRAGMA foreign_keys = ON;
