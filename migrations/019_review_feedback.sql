-- v1 - 2026-02-28 - Add review_feedback to tasks + DB trigger enforce_review_requires_pr
-- NOTE: DO NOT modify schema_versions here — db.py runner handles INSERT OR IGNORE automatically

ALTER TABLE tasks ADD COLUMN review_feedback TEXT DEFAULT NULL;

-- DB-level trigger: enforce that status=review requires an active PR
CREATE TRIGGER IF NOT EXISTS enforce_review_requires_pr
BEFORE UPDATE OF status ON tasks
FOR EACH ROW
WHEN NEW.status = 'review'
BEGIN
    SELECT RAISE(ABORT, 'Cannot set status=review: no active PR for this task')
    WHERE NOT EXISTS (
        SELECT 1 FROM pull_requests
        WHERE task_id = NEW.id
        AND status IN ('draft', 'open', 'merging')
    );
END;
