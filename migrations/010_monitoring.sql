-- 010_monitoring.sql: Server monitoring tables (metrics, candles, events)

-- Raw metrics: 10s collection interval, retained 24h
CREATE TABLE IF NOT EXISTS monitoring_metrics (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp INTEGER NOT NULL,
    metric    TEXT    NOT NULL,
    value     REAL    NOT NULL,
    metadata  TEXT    NOT NULL DEFAULT ''
);

-- Covering index for sparkline queries (metric + timestamp range + value)
CREATE INDEX IF NOT EXISTS idx_metrics_lookup
    ON monitoring_metrics (metric, timestamp, value);

-- For retention cleanup
CREATE INDEX IF NOT EXISTS idx_metrics_cleanup
    ON monitoring_metrics (timestamp);

-- Aggregated candles: 1-min intervals, retained 30d
CREATE TABLE IF NOT EXISTS monitoring_candles (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp INTEGER NOT NULL,
    metric    TEXT    NOT NULL,
    open      REAL    NOT NULL,
    high      REAL    NOT NULL,
    low       REAL    NOT NULL,
    close     REAL    NOT NULL,
    metadata  TEXT    NOT NULL DEFAULT ''
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_candles_unique
    ON monitoring_candles (metric, timestamp, metadata);

CREATE INDEX IF NOT EXISTS idx_candles_lookup
    ON monitoring_candles (metric, timestamp);

CREATE INDEX IF NOT EXISTS idx_candles_cleanup
    ON monitoring_candles (timestamp);

-- Security events: SSH logins, bans, console access, retained 30d
CREATE TABLE IF NOT EXISTS monitoring_events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp  INTEGER NOT NULL,
    event_type TEXT    NOT NULL,
    source_ip  TEXT,
    username   TEXT,
    details    TEXT
);

CREATE INDEX IF NOT EXISTS idx_events_type_ts
    ON monitoring_events (event_type, timestamp);

CREATE INDEX IF NOT EXISTS idx_events_cleanup
    ON monitoring_events (timestamp);
