-- 145: make audit_log append-only via ABORT triggers.
--
-- audit_log is the cross-system action trail (get_audit_log). It must be
-- tamper-evident: rows are only ever INSERTed by the application; the codebase
-- never issues UPDATE or DELETE against it (verified: no UPDATE/DELETE FROM
-- audit_log anywhere in core/). These triggers enforce that invariant at the
-- storage layer so a compromised path (or a stray manual query) cannot rewrite
-- or erase history. INSERT is intentionally left unguarded.
-- Reversibile: see 145_audit_log_immutable_down.sql

CREATE TRIGGER IF NOT EXISTS audit_log_no_update
BEFORE UPDATE ON audit_log
BEGIN
    SELECT RAISE(ABORT, 'audit_log is append-only');
END;

CREATE TRIGGER IF NOT EXISTS audit_log_no_delete
BEFORE DELETE ON audit_log
BEGIN
    SELECT RAISE(ABORT, 'audit_log is append-only');
END;

INSERT OR IGNORE INTO schema_versions (version) VALUES (145);
