-- 023_devx_p1_gate.sql
-- P1 gate state machine: sostituisce asyncio.sleep(300) in task orchestration
-- Sprint 3 DevX Layer - 2026-03-02
ALTER TABLE tasks ADD COLUMN p1_gate_notified_at TEXT;
ALTER TABLE tasks ADD COLUMN p1_gate_blocked_by TEXT;

INSERT OR IGNORE INTO schema_versions (version) VALUES (23);
