-- v1.0.0 - 2026-07-03 - P2 onboarding agent-native: per-user wizard state + profile capture.
-- Plan: docs/plans/2026-07-03-feat-onboarding-agent-native-plan.md (F1).
-- Number 164 reserved for P2 in the cross-plan scheme (163=P1 notifications, 165=P4).
-- ORDERING NOTE: the migration runner applies files with version > MAX(applied);
-- if this 164 is applied on a tenant before P1's 163, that 163 would be skipped
-- forever. Coordinate the fleet deploy so P1 (163) lands before P2 (164) — or P1
-- renumbers above 164. On a scratch/new tenant (no 163) this applies cleanly.
--
-- FK note: an interactive OAuth person can have NO users row (sync_oauth_user
-- inserts only when a mapped role claim is present, which interactive AuthKit
-- tokens lack). The wizard write path (onboarding_wizard use_case) ensures the
-- person's users row before inserting here, so the FK holds. Users are
-- soft-deleted, so ON DELETE CASCADE is for referential integrity, not cleanup.
PRAGMA foreign_keys=OFF;
BEGIN IMMEDIATE;

CREATE TABLE IF NOT EXISTS user_onboarding (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  workspace_id TEXT NOT NULL DEFAULT 'ws_default',
  user_id      TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  step_key     TEXT NOT NULL,
  status       TEXT NOT NULL CHECK(status IN ('done','snoozed','skipped')),
  answered_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  snooze_until TEXT,
  UNIQUE(workspace_id, user_id, step_key)
);
CREATE INDEX IF NOT EXISTS idx_user_onboarding_ws_user
  ON user_onboarding(workspace_id, user_id);

-- Profile captured by the welcome_profile step — ONLY after explicit consent in
-- the step. Deletable via onboarding_answer(step_key='welcome_profile',
-- action='delete_profile'). One row per (workspace_id, user_id).
CREATE TABLE IF NOT EXISTS user_profile (
  workspace_id   TEXT NOT NULL DEFAULT 'ws_default',
  user_id        TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  display_name   TEXT,
  role_title     TEXT,
  org_unit       TEXT,
  response_style TEXT CHECK(response_style IS NULL OR response_style IN ('concise','detailed')),
  created_at     TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  updated_at     TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  PRIMARY KEY (workspace_id, user_id)
);

INSERT OR IGNORE INTO schema_versions(version) VALUES (164);
COMMIT;
PRAGMA foreign_keys=ON;
