-- Down 151 - drop todos subsystem table.
DROP INDEX IF EXISTS idx_todos_dedup;
DROP INDEX IF EXISTS idx_todos_project;
DROP INDEX IF EXISTS idx_todos_open;
DROP TABLE IF EXISTS todos;
