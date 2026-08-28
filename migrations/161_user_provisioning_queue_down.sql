BEGIN IMMEDIATE;

DROP INDEX IF EXISTS idx_upq_email_pending;
DROP TABLE IF EXISTS user_provisioning_queue;
DELETE FROM schema_versions WHERE version = 161;

COMMIT;
