CREATE TABLE IF NOT EXISTS product_events (
    id              TEXT PRIMARY KEY,
    subject_type    TEXT NOT NULL CHECK(subject_type IN ('signup_intent', 'tenant')),
    subject_id      TEXT NOT NULL,
    tenant_id       TEXT,
    event_name      TEXT NOT NULL CHECK(event_name IN (
        'free_signup_started',
        'identity_verified',
        'tenant_ready',
        'paid_interest_submitted',
        'paid_interest_updated',
        'demo_served',
        'demo_first_view',
        'agent_connected',
        'customer_project_created',
        'customer_first_value'
    )),
    event_key       TEXT NOT NULL,
    occurred_at     TEXT NOT NULL,
    source          TEXT NOT NULL,
    payload_json    TEXT NOT NULL DEFAULT '{}' CHECK(json_valid(payload_json)),
    created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    CHECK(event_name != 'customer_first_value' OR tenant_id IS NOT NULL),
    UNIQUE(subject_type, subject_id, event_name, event_key)
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_product_events_tenant_first_value
    ON product_events(tenant_id, event_name)
    WHERE tenant_id IS NOT NULL AND event_name = 'customer_first_value';

CREATE INDEX IF NOT EXISTS idx_product_events_tenant_occurred
    ON product_events(tenant_id, occurred_at DESC);

CREATE TABLE IF NOT EXISTS product_event_outbox (
    event_id         TEXT PRIMARY KEY REFERENCES product_events(id) ON DELETE CASCADE,
    attempt_count    INTEGER NOT NULL DEFAULT 0 CHECK(attempt_count >= 0),
    next_attempt_at  TEXT NOT NULL,
    delivered_at     TEXT,
    last_error_code  TEXT,
    created_at       TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    updated_at       TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

CREATE INDEX IF NOT EXISTS idx_product_event_outbox_due
    ON product_event_outbox(delivered_at, next_attempt_at, event_id);

INSERT OR IGNORE INTO schema_versions (version) VALUES (171);
