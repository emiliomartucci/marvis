-- Migration 128: Brain v1 Drift Checker (sub-02 L3)
-- Date: 2026-05-16
-- Plan: docs/plans/sub/2026-05-15-brain-v1-02-drift-checker.md
-- Parent: docs/plans/2026-05-15-feat-brain-v1-mielinizzazione-plan.md
-- Author: brain-v1
--
-- Adds drift signal storage layered on sub-01 substrate (mig 127):
--   brain_drift_signals -- BLAKE2b-stable signals emitted by DR1-DR7 rules
--
-- Invariants (parent §9, sub-02 §4-§8):
--   * signal_id is BLAKE2b-16 stable hash. drift_axis EXCLUDED from hash (CE4 §11.5).
--   * run_id FK ON DELETE RESTRICT (signals semantically outlive runs).
--   * No FK into mutable substrate (tasks/pull_requests/handoffs/commits).
--   * State machine: open | superseded | resolved | dismissed (append-only on
--     state column; supersede chain via superseded_by_signal_id).
--   * drift_axis (CE4) is additive NULLable; partial index on non-NULL rows.
--   * expected_direction_source enum extends to 8 values: journal | project_status |
--     handoff | doc | brainstorm | meeting_transcript | none | task | pr | commit
--     (forward-compat for v1.2 meeting/brainstorm capture).
--
-- Rollback: migrations/128_brain_drift_signals_down.sql
-- Apply:    sqlite3 /data/pir/console.db < migrations/128_brain_drift_signals.sql

BEGIN IMMEDIATE;

-- ------------------------------------------------------------------
-- brain_drift_signals : append-only drift observations
-- ------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS brain_drift_signals (
    signal_id                   TEXT PRIMARY KEY,
    run_id                      TEXT NOT NULL,
    cycle_key                   TEXT NOT NULL,
    detected_at                 TEXT NOT NULL
                                CHECK(detected_at LIKE '%Z' OR detected_at LIKE '%+00:00'),
    rule_id                     TEXT NOT NULL
                                CHECK(rule_id IN ('DR1','DR2','DR3','DR4','DR5','DR6','DR7')),
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
                                    'claimed_decision_gap'
                                )),
    knowledge_form              TEXT NOT NULL
                                CHECK(knowledge_form IN (
                                    'adr','spec','playbook','tribal_memory',
                                    'external_update','claimed_decision','unknown'
                                )),
    classifier_version          INTEGER NOT NULL DEFAULT 1,
    -- CE4: enum extended for v1.2 brainstorm/meeting_transcript capture forward-compat.
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
    -- NaN guard: confidence = confidence is false only when value is NaN.
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
    -- CE4 §11.5: axis is derived deterministically from rule_id + source/ref kinds.
    -- NULLable: legacy rows pre-CE4 surface as `axis unknown` bucket (invariant 12).
    drift_axis                  TEXT
                                CHECK(drift_axis IS NULL OR drift_axis IN ('intent','context','both')),
    created_at                  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    updated_at                  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    FOREIGN KEY (run_id) REFERENCES brain_runs(run_id) ON DELETE RESTRICT,
    FOREIGN KEY (superseded_by_signal_id) REFERENCES brain_drift_signals(signal_id)
);

-- ------------------------------------------------------------------
-- Indexes (sub-02 §5.5)
-- ------------------------------------------------------------------

-- Primary read path: latest signals per scope.
CREATE INDEX IF NOT EXISTS idx_brain_drift_signals_scope_cycle
    ON brain_drift_signals(scope_type, scope_key, cycle_key DESC);

-- DR3 cross-cycle scan: by scope + type for ladder lookup.
CREATE INDEX IF NOT EXISTS idx_brain_drift_signals_scope_type
    ON brain_drift_signals(scope_type, scope_key, cycle_key DESC, signal_type);

-- Type-faceted dashboards.
CREATE INDEX IF NOT EXISTS idx_brain_drift_signals_type_cycle
    ON brain_drift_signals(signal_type, cycle_key DESC);

-- DR6 cross-cycle dedup by observed_direction_ref + type.
CREATE INDEX IF NOT EXISTS idx_brain_drift_signals_observed_ref
    ON brain_drift_signals(observed_direction_ref, signal_type, cycle_key DESC);

-- Run-scoped queries (cascade ops, drift phase aggregation).
CREATE INDEX IF NOT EXISTS idx_brain_drift_signals_run
    ON brain_drift_signals(run_id);

-- Cross-cycle continuation lookup (Memory-Ops consumer).
CREATE INDEX IF NOT EXISTS idx_brain_drift_signals_recurrence
    ON brain_drift_signals(recurrence_key, cycle_key DESC);

-- Open critical drift partial index.
CREATE INDEX IF NOT EXISTS idx_brain_drift_signals_open_severity
    ON brain_drift_signals(severity, cycle_key DESC)
    WHERE state = 'open' AND superseded_by_signal_id IS NULL;

-- DR3 lookback partial index (un-resolved per scope+type).
CREATE INDEX IF NOT EXISTS idx_brain_drift_signals_lookback
    ON brain_drift_signals(scope_type, scope_key, signal_type, resolved_at)
    WHERE resolved_at IS NULL AND state = 'open';

-- CE4 §11.5: axis-facet filtering on the read path. Partial: only non-NULL rows.
CREATE INDEX IF NOT EXISTS idx_brain_drift_signals_axis
    ON brain_drift_signals(cycle_key, drift_axis)
    WHERE drift_axis IS NOT NULL;

-- ------------------------------------------------------------------
-- Trigger: updated_at auto-refresh (mirror sub-01 pattern).
-- ------------------------------------------------------------------
CREATE TRIGGER IF NOT EXISTS trg_brain_drift_signals_updated_at
    AFTER UPDATE ON brain_drift_signals
    FOR EACH ROW
    WHEN NEW.updated_at = OLD.updated_at
BEGIN
    UPDATE brain_drift_signals
       SET updated_at = strftime('%Y-%m-%dT%H:%M:%fZ','now')
     WHERE signal_id = NEW.signal_id;
END;

INSERT OR IGNORE INTO schema_versions(version) VALUES (128);

COMMIT;
