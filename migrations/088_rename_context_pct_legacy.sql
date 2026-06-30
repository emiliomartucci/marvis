-- Migration 088 — Rename sessions_meta.last_context_pct → last_context_pct_legacy
--
-- Piano di riferimento: docs/plans/2026-04-22-feat-metrics-provider-consistency-plan.md (PR3 D6).
--
-- Goal: force compile-time catch of stray writers to the legacy column.
-- PR2 introduced `last_context_pct_real` (true ratio) and `_scaled` (Claude
-- auto-compact alias). The legacy column stayed dual-written during rollout
-- for back-compat. PR3 removes all writers + renames the column so any
-- residual writer fails loudly instead of silently staling.
--
-- Reads: SELECT aliases `last_context_pct_real AS last_context_pct` in
-- `api/routers/sessions.py` to preserve the Pydantic/TS frontend contract
-- without a @computed_field (PR2 avoided computed_fields due to
-- construction conflicts in existing call sites).
--
-- SQLite 3.45+ supports native `ALTER TABLE ... RENAME COLUMN`.

BEGIN IMMEDIATE;

ALTER TABLE sessions_meta RENAME COLUMN last_context_pct TO last_context_pct_legacy;

INSERT OR IGNORE INTO schema_versions (version) VALUES (88);
COMMIT;
