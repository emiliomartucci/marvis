-- Plan 2 (code-graph senza codice sul tenant) U1: graph ingest provenance.
--
-- The graph itself lives in graph_nodes/graph_edges. This table records, per
-- project, HOW the currently-active graph arrived: was it attested by a local
-- client session or signed by the user's CI, at which commit, with a dirty
-- working tree or not. Freshness is DERIVED from this row, never synthesized —
-- a consumer that wants "fresh" must check source+commit here, it is never
-- assumed. One row per project (the active provenance); ingest upserts it.
--
-- The security invariant of the plan is structural: the tenant stores the graph
-- (nodes/edges) plus this small provenance ledger, and NEVER the source itself.
CREATE TABLE IF NOT EXISTS graph_ingest_provenance (
    project_id     TEXT PRIMARY KEY,
    source         TEXT NOT NULL CHECK(source IN ('client-attested', 'ci-signed')),
    commit_sha     TEXT,
    dirty          INTEGER NOT NULL DEFAULT 0 CHECK(dirty IN (0, 1)),
    parser_version TEXT,
    node_count     INTEGER NOT NULL DEFAULT 0 CHECK(node_count >= 0),
    edge_count     INTEGER NOT NULL DEFAULT 0 CHECK(edge_count >= 0),
    generated_at   TEXT,
    ingested_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

INSERT OR IGNORE INTO schema_versions (version) VALUES (172);
