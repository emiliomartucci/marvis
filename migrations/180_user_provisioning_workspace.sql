-- Migration 180: make every provisioning request workspace-owned.
--
-- Existing queue rows predate workspace ownership. They cannot be attributed
-- safely from email/requester alone, so they intentionally keep NULL and are
-- inert in the v180 service. New rows must carry a non-empty workspace_id.

BEGIN IMMEDIATE;

ALTER TABLE user_provisioning_queue ADD COLUMN workspace_id TEXT;

DROP INDEX IF EXISTS idx_upq_email_pending;

CREATE UNIQUE INDEX idx_upq_workspace_email_pending
ON user_provisioning_queue(workspace_id, email)
WHERE status = 'queued' AND workspace_id IS NOT NULL;

CREATE INDEX idx_upq_workspace_status_created
ON user_provisioning_queue(workspace_id, status, created_at);

CREATE INDEX idx_upq_workspace_requester_created
ON user_provisioning_queue(workspace_id, requester_id, created_at DESC);

CREATE TRIGGER user_provisioning_workspace_required_insert
BEFORE INSERT ON user_provisioning_queue
WHEN NEW.workspace_id IS NULL OR length(trim(NEW.workspace_id)) = 0
BEGIN
  SELECT RAISE(ABORT, 'user provisioning workspace_id required');
END;

CREATE TRIGGER user_provisioning_workspace_required_update
BEFORE UPDATE ON user_provisioning_queue
WHEN NEW.workspace_id IS NULL OR length(trim(NEW.workspace_id)) = 0
BEGIN
  SELECT RAISE(ABORT, 'user provisioning workspace_id required');
END;

CREATE TRIGGER user_provisioning_workspace_immutable
BEFORE UPDATE OF workspace_id ON user_provisioning_queue
WHEN OLD.workspace_id IS NOT NULL AND OLD.workspace_id != NEW.workspace_id
BEGIN
  SELECT RAISE(ABORT, 'user provisioning workspace_id immutable');
END;

INSERT OR IGNORE INTO schema_versions(version) VALUES (180);

COMMIT;
