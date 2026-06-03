-- 015_pull_requests_down.sql
-- ROLLBACK - execute only in emergency post-deploy

DROP TABLE IF EXISTS pull_requests;
DELETE FROM schema_versions WHERE version = 15;
