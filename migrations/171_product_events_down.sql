DROP INDEX IF EXISTS idx_product_event_outbox_due;
DROP TABLE IF EXISTS product_event_outbox;
DROP INDEX IF EXISTS idx_product_events_tenant_occurred;
DROP INDEX IF EXISTS uq_product_events_tenant_first_value;
DROP TABLE IF EXISTS product_events;
DELETE FROM schema_versions WHERE version = 171;
