CREATE TABLE IF NOT EXISTS finder_pins (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    path TEXT NOT NULL,
    label TEXT,
    position INTEGER NOT NULL DEFAULT 0,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(user_id, path)
);
CREATE INDEX IF NOT EXISTS idx_finder_pins_user ON finder_pins(user_id, position);
