-- Migration 161 — user provisioning queue (RBAC F3, plan d33292f0).
--
-- add_user requests wait here for the root provisioner worker (WORKOS_API_KEY
-- never enters the tenant process). No lease/processing state: the worker is
-- a systemd oneshot timer (no-overlap by design); a crash mid-item leaves the
-- row queued and it is retried (WorkOS branches are idempotent). attempts
-- increments only on a completed error; 3 = poison. Admins are NEVER minted
-- through this queue (console create_user only) — hence the CHECK.

BEGIN IMMEDIATE;

CREATE TABLE user_provisioning_queue (
  id TEXT PRIMARY KEY,
  email TEXT NOT NULL,
  requested_role TEXT NOT NULL CHECK(requested_role IN ('operator','viewer')),
  teams_json TEXT,
  requester_id TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'queued' CHECK(status IN ('queued','done','failed','rejected')),
  attempts INTEGER NOT NULL DEFAULT 0,
  error TEXT,
  workos_user_id TEXT,
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  processed_at TEXT
);
CREATE UNIQUE INDEX idx_upq_email_pending ON user_provisioning_queue(email) WHERE status = 'queued';

INSERT OR IGNORE INTO schema_versions(version) VALUES (161);

COMMIT;
