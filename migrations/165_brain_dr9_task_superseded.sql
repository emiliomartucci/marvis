-- Migration 165 — P4 brain v2 F1 (PR-a): DR9 task_superseded + task_probably_done finding.
--
-- SQLite cannot ALTER an immutable CHECK, so this is a table-rebuild (pattern mig 133):
--   A. brain_drift_signals: rule_id CHECK += 'DR9'; signal_type CHECK += 'task_superseded'.
--   B. brain_findings:      finding_type CHECK += 'task_probably_done'.
--
-- The _new CREATE statements reproduce the LIVE schema VERBATIM (column order, defaults,
-- created_at/updated_at, brain_findings.authored_by_agent) so `INSERT ... SELECT *` is
-- column-aligned. Self-FK (superseded_by_*) references the FINAL table name and re-resolves
-- after RENAME; child FKs (brain_finding_states/evidence -> brain_findings) re-resolve by
-- name too. foreign_keys=OFF for the whole rebuild.
--
-- Reversibile: migrations/165_brain_dr9_task_superseded_down.sql.

PRAGMA foreign_keys = OFF;

BEGIN IMMEDIATE;

-- ============================================================ A. brain_drift_signals
CREATE TABLE brain_drift_signals_new (
    signal_id                   TEXT PRIMARY KEY,
    run_id                      TEXT NOT NULL,
    cycle_key                   TEXT NOT NULL,
    detected_at                 TEXT NOT NULL
                                CHECK(detected_at LIKE '%Z' OR detected_at LIKE '%+00:00'),
    rule_id                     TEXT NOT NULL
                                CHECK(rule_id IN ('DR1','DR2','DR3','DR4','DR5','DR6','DR7','DR8','DR9')),
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
                                    'direction_misalignment',
                                    'task_superseded'
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
    FOREIGN KEY (superseded_by_signal_id) REFERENCES "brain_drift_signals"(signal_id)
);

INSERT INTO brain_drift_signals_new SELECT * FROM brain_drift_signals;
DROP TABLE brain_drift_signals;
ALTER TABLE brain_drift_signals_new RENAME TO brain_drift_signals;

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

-- ============================================================ B. brain_findings
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
                                    'direction_bootstrap',
                                    'task_probably_done'
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
    urgency_score               INTEGER NOT NULL DEFAULT 1
                                CHECK(urgency_score >= 1),
    entity_ref                  TEXT,
    proposed_payload_json       TEXT
                                CHECK(proposed_payload_json IS NULL
                                      OR json_valid(proposed_payload_json)),
    authored_by_agent           TEXT,
    FOREIGN KEY (run_id) REFERENCES brain_runs(run_id) ON DELETE RESTRICT,
    FOREIGN KEY (regression_of_finding_id)
        REFERENCES "brain_findings"(finding_id) ON DELETE SET NULL,
    FOREIGN KEY (superseded_by_finding_id)
        REFERENCES "brain_findings"(finding_id) ON DELETE SET NULL
);

INSERT INTO brain_findings_new SELECT * FROM brain_findings;
DROP TABLE brain_findings;
ALTER TABLE brain_findings_new RENAME TO brain_findings;

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
CREATE INDEX idx_brain_findings_entity_open
    ON brain_findings(finding_type, entity_ref)
    WHERE approval_state IN ('open', 'pending_bootstrap');
CREATE INDEX idx_brain_findings_urgency
    ON brain_findings(urgency_score DESC, last_seen_cycle_key DESC)
    WHERE approval_state IN ('open', 'pending_bootstrap');

INSERT OR IGNORE INTO schema_versions (version) VALUES (165);

COMMIT;

PRAGMA foreign_keys = ON;
