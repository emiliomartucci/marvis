-- Migration 133: Brain v1.2 Direction Integration — Schema Foundation
-- Date: 2026-05-18
-- Plan: docs/plans/2026-05-17-feat-brain-v1-2-direction-integration-plan.md
-- Author: brain-v1-2
--
-- Schema delta required for direction integration:
--   1) brain_drift_signals: extend CHECK enums
--      rule_id IN (DR1..DR7) -> (DR1..DR8)
--      signal_type adds 'direction_misalignment'
--      (table-swap because SQLite cannot ALTER CHECK)
--   2) brain_findings: extend CHECK enums + add columns
--      finding_type IN (idea|task_candidate|open_question|scope_gap|
--                       procedure_change|contradiction) -> +direction_drift,
--                                                          +direction_bootstrap
--      approval_state IN (open|approved|dismissed|resolved|superseded|expired)
--                       -> +pending_bootstrap, +applied
--      ADD COLUMN urgency_score INTEGER NOT NULL DEFAULT 1
--      ADD COLUMN entity_ref TEXT
--      ADD COLUMN proposed_payload_json TEXT
--      (table-swap because of CHECK extension)
--   3) NEW table project_directions (DB cache of hybrid storage)
--   4) NEW table direction_changelog (append-only history)
--
-- Schema rules preserved verbatim from production introspection
-- (sqlite3 .schema brain_drift_signals / brain_findings on 2026-05-18):
--   - All existing columns kept exactly as before
--   - All existing FK constraints preserved
--   - All existing indices recreated post-swap
--   - All existing triggers recreated; terminal_forward_only updated
--     to allow pending_bootstrap -> applied | dismissed and
--     applied -> superseded
--
-- Rollback: migrations/133_brain_v1_2_direction_schema_down.sql
-- Apply:    sqlite3 /data/pir/console.db < migrations/133_brain_v1_2_direction_schema.sql

PRAGMA foreign_keys = OFF;

BEGIN IMMEDIATE;

-- =============================================================================
-- PART 1: brain_drift_signals — extend rule_id + signal_type CHECK
-- =============================================================================

-- Drop trigger first (will be recreated post-swap)
DROP TRIGGER IF EXISTS trg_brain_drift_signals_updated_at;

-- Drop indices (will be recreated post-swap)
DROP INDEX IF EXISTS idx_brain_drift_signals_scope_cycle;
DROP INDEX IF EXISTS idx_brain_drift_signals_scope_type;
DROP INDEX IF EXISTS idx_brain_drift_signals_type_cycle;
DROP INDEX IF EXISTS idx_brain_drift_signals_observed_ref;
DROP INDEX IF EXISTS idx_brain_drift_signals_run;
DROP INDEX IF EXISTS idx_brain_drift_signals_recurrence;
DROP INDEX IF EXISTS idx_brain_drift_signals_open_severity;
DROP INDEX IF EXISTS idx_brain_drift_signals_lookback;
DROP INDEX IF EXISTS idx_brain_drift_signals_axis;

CREATE TABLE brain_drift_signals_new (
    signal_id                   TEXT PRIMARY KEY,
    run_id                      TEXT NOT NULL,
    cycle_key                   TEXT NOT NULL,
    detected_at                 TEXT NOT NULL
                                CHECK(detected_at LIKE '%Z' OR detected_at LIKE '%+00:00'),
    rule_id                     TEXT NOT NULL
                                CHECK(rule_id IN ('DR1','DR2','DR3','DR4','DR5','DR6','DR7','DR8')),
    schema_version              INTEGER NOT NULL DEFAULT 1,
    scope_type                  TEXT NOT NULL
                                CHECK(scope_type IN ('company','program','project')),
    scope_key                   TEXT NOT NULL,
    program_key                 TEXT,
    signal_type                 TEXT NOT NULL
                                CHECK(signal_type IN (
                                    'activity_without_status',
                                    'decision_without_adr',
                                    'playbook_changed',
                                    'stale_open_loop',
                                    'docs_governance_drift',
                                    'external_update_unpropagated',
                                    'claimed_decision_gap',
                                    'direction_misalignment'
                                )),
    knowledge_form              TEXT NOT NULL
                                CHECK(knowledge_form IN (
                                    'adr','spec','playbook','tribal_memory',
                                    'external_update','claimed_decision','unknown'
                                )),
    classifier_version          INTEGER NOT NULL DEFAULT 1,
    expected_direction_source   TEXT NOT NULL
                                CHECK(expected_direction_source IN (
                                    'journal','project_status','handoff','doc',
                                    'brainstorm','meeting_transcript','none',
                                    'task','pr','commit'
                                )),
    expected_direction_ref      TEXT,
    observed_direction_ref      TEXT NOT NULL,
    observed_delta              TEXT NOT NULL
                                CHECK(length(observed_delta) <= 2000),
    evidence_json               TEXT NOT NULL DEFAULT '[]'
                                CHECK(json_valid(evidence_json)),
    evidence_hash               TEXT NOT NULL
                                CHECK(length(evidence_hash) = 64),
    severity                    TEXT NOT NULL
                                CHECK(severity IN ('low','medium','high','critical')),
    confidence                  REAL NOT NULL
                                CHECK(confidence BETWEEN 0.0 AND 1.0
                                      AND confidence = confidence),
    recurrence_key              TEXT NOT NULL,
    involved_projects_json      TEXT NOT NULL DEFAULT '[]'
                                CHECK(json_valid(involved_projects_json)),
    state                       TEXT NOT NULL DEFAULT 'open'
                                CHECK(state IN ('open','superseded','resolved','dismissed')),
    superseded_by_signal_id     TEXT,
    resolved_at                 TEXT,
    dismissed_at                TEXT,
    dismissed_by                TEXT,
    dismiss_reason              TEXT,
    drift_axis                  TEXT
                                CHECK(drift_axis IS NULL OR drift_axis IN ('intent','context','both')),
    created_at                  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    updated_at                  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    FOREIGN KEY (run_id) REFERENCES brain_runs(run_id) ON DELETE RESTRICT,
    FOREIGN KEY (superseded_by_signal_id) REFERENCES brain_drift_signals_new(signal_id)
);

INSERT INTO brain_drift_signals_new SELECT * FROM brain_drift_signals;
DROP TABLE brain_drift_signals;
ALTER TABLE brain_drift_signals_new RENAME TO brain_drift_signals;

-- Recreate ALL indices verbatim
CREATE INDEX idx_brain_drift_signals_scope_cycle
    ON brain_drift_signals(scope_type, scope_key, cycle_key DESC);
CREATE INDEX idx_brain_drift_signals_scope_type
    ON brain_drift_signals(scope_type, scope_key, cycle_key DESC, signal_type);
CREATE INDEX idx_brain_drift_signals_type_cycle
    ON brain_drift_signals(signal_type, cycle_key DESC);
CREATE INDEX idx_brain_drift_signals_observed_ref
    ON brain_drift_signals(observed_direction_ref, signal_type, cycle_key DESC);
CREATE INDEX idx_brain_drift_signals_run
    ON brain_drift_signals(run_id);
CREATE INDEX idx_brain_drift_signals_recurrence
    ON brain_drift_signals(recurrence_key, cycle_key DESC);
CREATE INDEX idx_brain_drift_signals_open_severity
    ON brain_drift_signals(severity, cycle_key DESC)
    WHERE state = 'open' AND superseded_by_signal_id IS NULL;
CREATE INDEX idx_brain_drift_signals_lookback
    ON brain_drift_signals(scope_type, scope_key, signal_type, resolved_at)
    WHERE resolved_at IS NULL AND state = 'open';
CREATE INDEX idx_brain_drift_signals_axis
    ON brain_drift_signals(cycle_key, drift_axis)
    WHERE drift_axis IS NOT NULL;

-- Recreate trigger verbatim
CREATE TRIGGER trg_brain_drift_signals_updated_at
    AFTER UPDATE ON brain_drift_signals
    FOR EACH ROW
    WHEN NEW.updated_at = OLD.updated_at
BEGIN
    UPDATE brain_drift_signals
       SET updated_at = strftime('%Y-%m-%dT%H:%M:%fZ','now')
     WHERE signal_id = NEW.signal_id;
END;

-- =============================================================================
-- PART 2: brain_findings — extend finding_type + approval_state CHECK + 3 cols
-- =============================================================================

-- Drop triggers (recreated post-swap)
DROP TRIGGER IF EXISTS trg_brain_findings_updated_at;
DROP TRIGGER IF EXISTS trg_brain_findings_no_delete_resolved;
DROP TRIGGER IF EXISTS trg_brain_findings_terminal_forward_only;

-- Drop indices (recreated post-swap)
DROP INDEX IF EXISTS uk_brain_findings_natural;
DROP INDEX IF EXISTS idx_brain_findings_scope_cycle;
DROP INDEX IF EXISTS idx_brain_findings_open;
DROP INDEX IF EXISTS idx_brain_findings_fingerprint;
DROP INDEX IF EXISTS idx_brain_findings_type_cycle;
DROP INDEX IF EXISTS idx_brain_findings_run;
DROP INDEX IF EXISTS idx_brain_findings_regression;

CREATE TABLE brain_findings_new (
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
                                    'contradiction',
                                    'direction_drift',
                                    'direction_bootstrap'
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
    confidence                  TEXT NOT NULL
                                CHECK(confidence IN ('low','medium','high')),
    approval_state              TEXT NOT NULL DEFAULT 'open'
                                CHECK(approval_state IN (
                                    'open','approved','dismissed',
                                    'resolved','superseded','expired',
                                    'pending_bootstrap','applied'
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
    -- NEW columns for Brain v1.2 direction integration
    urgency_score               INTEGER NOT NULL DEFAULT 1
                                CHECK(urgency_score >= 1),
    entity_ref                  TEXT,
    proposed_payload_json       TEXT
                                CHECK(proposed_payload_json IS NULL
                                      OR json_valid(proposed_payload_json)),
    FOREIGN KEY (run_id) REFERENCES brain_runs(run_id) ON DELETE RESTRICT,
    FOREIGN KEY (regression_of_finding_id)
        REFERENCES brain_findings_new(finding_id) ON DELETE SET NULL,
    FOREIGN KEY (superseded_by_finding_id)
        REFERENCES brain_findings_new(finding_id) ON DELETE SET NULL
);

INSERT INTO brain_findings_new (
    finding_id, run_id, cycle_key, detected_at, finding_type, schema_version,
    scope_type, scope_key, program_key, title, summary, why_now, evidence_hash,
    involved_projects_json, suggested_artifact, owner_hint_json,
    closure_condition_kind, closure_condition_json, closure_condition_human,
    severity, confidence, approval_state, regression_of_finding_id,
    proposal_fingerprint, recurrence_count, first_seen_cycle_key,
    last_seen_cycle_key, applied_artifact_ref, applied_at, applied_by_user_id,
    expires_at, superseded_by_finding_id, created_at, updated_at,
    urgency_score, entity_ref, proposed_payload_json
)
SELECT
    finding_id, run_id, cycle_key, detected_at, finding_type, schema_version,
    scope_type, scope_key, program_key, title, summary, why_now, evidence_hash,
    involved_projects_json, suggested_artifact, owner_hint_json,
    closure_condition_kind, closure_condition_json, closure_condition_human,
    severity, confidence, approval_state, regression_of_finding_id,
    proposal_fingerprint, recurrence_count, first_seen_cycle_key,
    last_seen_cycle_key, applied_artifact_ref, applied_at, applied_by_user_id,
    expires_at, superseded_by_finding_id, created_at, updated_at,
    1, NULL, NULL
FROM brain_findings;

DROP TABLE brain_findings;
ALTER TABLE brain_findings_new RENAME TO brain_findings;

-- Recreate ALL existing indices verbatim
CREATE UNIQUE INDEX uk_brain_findings_natural
    ON brain_findings(
        cycle_key, finding_type, scope_type, scope_key,
        evidence_hash, closure_condition_kind
    );
CREATE INDEX idx_brain_findings_scope_cycle
    ON brain_findings(scope_type, scope_key, cycle_key DESC)
    WHERE superseded_by_finding_id IS NULL;
CREATE INDEX idx_brain_findings_open
    ON brain_findings(approval_state, created_at DESC)
    WHERE approval_state = 'open';
CREATE INDEX idx_brain_findings_fingerprint
    ON brain_findings(proposal_fingerprint, cycle_key DESC);
CREATE INDEX idx_brain_findings_type_cycle
    ON brain_findings(finding_type, cycle_key DESC);
CREATE INDEX idx_brain_findings_run
    ON brain_findings(run_id);
CREATE INDEX idx_brain_findings_regression
    ON brain_findings(regression_of_finding_id)
    WHERE regression_of_finding_id IS NOT NULL;

-- NEW indices for v1.2 direction (no-flood dedup + triage ordering)
CREATE INDEX idx_brain_findings_entity_open
    ON brain_findings(finding_type, entity_ref)
    WHERE approval_state IN ('open', 'pending_bootstrap');
CREATE INDEX idx_brain_findings_urgency
    ON brain_findings(urgency_score DESC, last_seen_cycle_key DESC)
    WHERE approval_state IN ('open', 'pending_bootstrap');

-- Recreate triggers (updated_at + no_delete_resolved same as before)
CREATE TRIGGER trg_brain_findings_updated_at
    AFTER UPDATE ON brain_findings
    FOR EACH ROW
    WHEN NEW.updated_at = OLD.updated_at
BEGIN
    UPDATE brain_findings
       SET updated_at = strftime('%Y-%m-%dT%H:%M:%fZ','now')
     WHERE finding_id = NEW.finding_id;
END;

CREATE TRIGGER trg_brain_findings_no_delete_resolved
    BEFORE DELETE ON brain_findings
    FOR EACH ROW
    WHEN OLD.approval_state = 'resolved'
BEGIN
    SELECT RAISE(ABORT, 'cannot delete resolved finding');
END;

-- terminal_forward_only updated: pending_bootstrap and applied are NOT terminal.
-- Forward-only transitions allowed:
--   open -> approved | dismissed | pending_bootstrap | applied | superseded | expired
--   approved -> applied | superseded | resolved
--   pending_bootstrap -> applied | dismissed
--   applied -> superseded
--   Terminal (forward-only blocked): dismissed | resolved | expired | superseded
CREATE TRIGGER trg_brain_findings_terminal_forward_only
    BEFORE UPDATE OF approval_state ON brain_findings
    FOR EACH ROW
    WHEN OLD.approval_state IN ('dismissed','resolved','expired','superseded')
         AND NEW.approval_state <> OLD.approval_state
BEGIN
    SELECT RAISE(ABORT, 'terminal finding state is forward-only');
END;

-- =============================================================================
-- PART 2b: brain_finding_states — extend to_state CHECK
-- =============================================================================
-- The states table tracks finding lifecycle history. Must include
-- pending_bootstrap + applied so emit_finding_dedup state inserts succeed.

DROP INDEX IF EXISTS idx_brain_findings_states_finding;
DROP INDEX IF EXISTS idx_brain_findings_states_actor;

CREATE TABLE brain_finding_states_new (
    state_id              TEXT PRIMARY KEY,
    finding_id            TEXT NOT NULL,
    from_state            TEXT,
    to_state              TEXT NOT NULL
                          CHECK(to_state IN (
                              'open','approved','dismissed',
                              'resolved','superseded','expired',
                              'pending_bootstrap','applied'
                          )),
    actor_user_id         TEXT,
    reason                TEXT CHECK(reason IS NULL OR length(reason) <= 500),
    applied_artifact_ref  TEXT,
    created_at            TEXT NOT NULL
                          DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    FOREIGN KEY (finding_id) REFERENCES brain_findings(finding_id) ON DELETE RESTRICT
);

INSERT INTO brain_finding_states_new SELECT * FROM brain_finding_states;
DROP TABLE brain_finding_states;
ALTER TABLE brain_finding_states_new RENAME TO brain_finding_states;

CREATE INDEX idx_brain_findings_states_finding
    ON brain_finding_states(finding_id, created_at DESC);
CREATE INDEX idx_brain_findings_states_actor
    ON brain_finding_states(actor_user_id, created_at DESC);

-- =============================================================================
-- PART 3: NEW table project_directions (DB cache of hybrid storage)
-- =============================================================================
CREATE TABLE IF NOT EXISTS project_directions (
    project_slug          TEXT PRIMARY KEY,
    summary               TEXT NOT NULL
                          CHECK(length(summary) <= 4000),
    out_of_scope          TEXT NOT NULL
                          CHECK(length(out_of_scope) <= 2000),
    last_updated_at       TEXT NOT NULL
                          CHECK(last_updated_at LIKE '%Z' OR last_updated_at LIKE '%+00:00'),
    last_updated_by       TEXT,
    source_drift_signal   TEXT,
    source_finding_id     TEXT,
    schema_version        INTEGER NOT NULL DEFAULT 1,
    created_at            TEXT NOT NULL
                          DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    updated_at            TEXT NOT NULL
                          DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

CREATE INDEX IF NOT EXISTS idx_project_directions_updated
    ON project_directions(last_updated_at DESC);

CREATE TRIGGER IF NOT EXISTS trg_project_directions_updated_at
    AFTER UPDATE ON project_directions
    FOR EACH ROW
    WHEN NEW.updated_at = OLD.updated_at
BEGIN
    UPDATE project_directions
       SET updated_at = strftime('%Y-%m-%dT%H:%M:%fZ','now')
     WHERE project_slug = NEW.project_slug;
END;

-- =============================================================================
-- PART 4: NEW table direction_changelog (append-only history)
-- =============================================================================
CREATE TABLE IF NOT EXISTS direction_changelog (
    changelog_id            TEXT PRIMARY KEY,
    project_slug            TEXT NOT NULL,
    applied_at              TEXT NOT NULL
                            CHECK(applied_at LIKE '%Z' OR applied_at LIKE '%+00:00'),
    applied_by              TEXT NOT NULL,
    change_type             TEXT NOT NULL
                            CHECK(change_type IN ('bootstrap','direction_update','manual_edit')),
    old_summary             TEXT,
    new_summary             TEXT NOT NULL
                            CHECK(length(new_summary) <= 4000),
    old_out_of_scope        TEXT,
    new_out_of_scope        TEXT NOT NULL
                            CHECK(length(new_out_of_scope) <= 2000),
    source_finding_id       TEXT,
    source_drift_signal_id  TEXT,
    rationale               TEXT
                            CHECK(rationale IS NULL OR length(rationale) <= 1000),
    created_at              TEXT NOT NULL
                            DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

CREATE INDEX IF NOT EXISTS idx_direction_changelog_slug_applied
    ON direction_changelog(project_slug, applied_at DESC);

CREATE INDEX IF NOT EXISTS idx_direction_changelog_finding
    ON direction_changelog(source_finding_id)
    WHERE source_finding_id IS NOT NULL;

-- =============================================================================
-- PART 5: Update schema_versions
-- =============================================================================
INSERT OR IGNORE INTO schema_versions(version) VALUES (133);

COMMIT;

PRAGMA foreign_keys = ON;
