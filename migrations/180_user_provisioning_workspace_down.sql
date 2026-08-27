-- Migration 180 rollback. This intentionally fails if multiple workspaces now
-- have queued rows for one email: the old global uniqueness contract cannot
-- represent that state without deleting or merging tenant data.

BEGIN IMMEDIATE;

DROP TRIGGER IF EXISTS user_provisioning_workspace_required_insert;
DROP TRIGGER IF EXISTS user_provisioning_workspace_required_update;
DROP TRIGGER IF EXISTS user_provisioning_workspace_immutable;
DROP INDEX IF EXISTS idx_upq_workspace_email_pending;
DROP INDEX IF EXISTS idx_upq_workspace_status_created;
DROP INDEX IF EXISTS idx_upq_workspace_requester_created;

CREATE TABLE user_provisioning_queue_v180_down (
  id TEXT PRIMARY KEY,
  email TEXT NOT NULL,
  requested_role TEXT NOT NULL CHECK(requested_role IN ('operator','viewer')),
  teams_json TEXT,
  requester_id TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'queued'
    CHECK(status IN ('queued','done','failed','rejected')),
  attempts INTEGER NOT NULL DEFAULT 0,
  error TEXT,
  workos_user_id TEXT,
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  processed_at TEXT
);

INSERT INTO user_provisioning_queue_v180_down
  (id,email,requested_role,teams_json,requester_id,status,attempts,error,
   workos_user_id,created_at,processed_at)
SELECT id,email,requested_role,teams_json,requester_id,status,attempts,error,
       workos_user_id,created_at,processed_at
FROM user_provisioning_queue;

CREATE UNIQUE INDEX idx_upq_email_pending_v180_down
ON user_provisioning_queue_v180_down(email) WHERE status = 'queued';

DROP TABLE user_provisioning_queue;
ALTER TABLE user_provisioning_queue_v180_down RENAME TO user_provisioning_queue;
DROP INDEX idx_upq_email_pending_v180_down;
CREATE UNIQUE INDEX idx_upq_email_pending
ON user_provisioning_queue(email) WHERE status = 'queued';

DELETE FROM schema_versions WHERE version = 180;

COMMIT;
