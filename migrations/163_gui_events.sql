-- Dedicated GUI analytics ledger. This intentionally does not reuse the
-- operational events outbox, which can be dispatched to downstream automations.
CREATE TABLE IF NOT EXISTS gui_events (
    id              TEXT PRIMARY KEY DEFAULT (lower(hex(randomblob(16)))),
    workspace_id    TEXT NOT NULL,
    event_name      TEXT NOT NULL CHECK(event_name IN ('gui_first_value')),
    surface         TEXT NOT NULL CHECK(surface IN ('brain_diario')),
    route           TEXT NOT NULL,
    actor_id        TEXT,
    entry_id        TEXT NOT NULL,
    run_id          TEXT NOT NULL,
    cycle_key       TEXT NOT NULL,
    registri_count  INTEGER NOT NULL CHECK(registri_count > 0),
    payload_json    TEXT NOT NULL DEFAULT '{}' CHECK(json_valid(payload_json)),
    seen_count      INTEGER NOT NULL DEFAULT 1 CHECK(seen_count >= 1),
    first_seen_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    last_seen_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    UNIQUE(workspace_id, event_name)
);

CREATE INDEX IF NOT EXISTS idx_gui_events_workspace_seen
    ON gui_events(workspace_id, first_seen_at DESC);

INSERT OR IGNORE INTO schema_versions (version) VALUES (163);
