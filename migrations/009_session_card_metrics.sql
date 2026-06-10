-- 009: Session card metrics (CPU, RAM, working time tracking)
ALTER TABLE sessions_meta ADD COLUMN working_seconds INTEGER DEFAULT 0;
ALTER TABLE sessions_meta ADD COLUMN last_working_check REAL;
