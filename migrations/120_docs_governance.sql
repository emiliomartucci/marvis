-- v1.0.0 - 2026-05-10 - Docs governance durable decision table
--
-- Plan 0 reserves migration slot 120 to avoid the historical 100_* collision.
-- Trust-score history and docs_kg_facts are deferred to Plan 0.5; this MVP
-- stores deterministic governance PR label decisions only.

BEGIN IMMEDIATE;

CREATE TABLE IF NOT EXISTS docs_triage_decisions (
    id TEXT PRIMARY KEY,
    pr_id TEXT,
    commit_sha TEXT,
    layer TEXT NOT NULL CHECK(layer IN (
        'api',
        'mcp',
        'llm-gateway',
        'kg',
        'code-examples',
        'narrative',
        'concept'
    )),
    decision TEXT NOT NULL DEFAULT 'draft_pr' CHECK(decision IN ('draft_pr')),
    score REAL NOT NULL CHECK(score BETWEEN 0.0 AND 1.0),
    pr_label TEXT NOT NULL,
    change_type TEXT NOT NULL,
    hard_gate_failures TEXT NOT NULL DEFAULT '[]',
    enrichment_md TEXT,
    kg_stale INTEGER NOT NULL DEFAULT 0 CHECK(kg_stale IN (0, 1)),
    corpus_state TEXT NOT NULL DEFAULT 'ready' CHECK(corpus_state IN ('bootstrap', 'ready')),
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    merged_at TEXT,
    reverted_at TEXT,
    reverted_within_window INTEGER NOT NULL DEFAULT 0 CHECK(reverted_within_window IN (0, 1)),
    metadata TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_docs_triage_layer_created
    ON docs_triage_decisions(layer, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_docs_triage_pr
    ON docs_triage_decisions(pr_id)
    WHERE pr_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_docs_triage_commit
    ON docs_triage_decisions(commit_sha)
    WHERE commit_sha IS NOT NULL;

INSERT OR IGNORE INTO schema_versions (version) VALUES (120);

COMMIT;
