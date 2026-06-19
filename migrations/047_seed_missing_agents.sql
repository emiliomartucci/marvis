-- Migration 047: Seed DevX, System Health, Reddit agents
-- All INSERTs in Python hook _seed_missing_agents() in db.py.
-- executescript() + PRAGMA foreign_keys=ON causes FK constraint errors
-- when inserting into agents (FK on users.id) in the same script.

INSERT OR IGNORE INTO schema_versions (version) VALUES (47);
