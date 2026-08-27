-- Migration 162 — owner-confidential per-file (RBAC F4, plan d33292f0).
--
-- file_meta is the AUTHORITATIVE confidentiality record: the frontmatter can
-- only ADD secrecy (OR semantics), never remove it — stripping the marker
-- from the file body no longer declassifies. Identities are canonical
-- user_id values (never emails). documents.confidential mirrors the flag so
-- the search candidate SQL can exclude purged docs the same way archived
-- works; the embed upsert never touches it.

BEGIN IMMEDIATE;

CREATE TABLE file_meta (
  id TEXT PRIMARY KEY,
  project_slug TEXT NOT NULL,
  rel_path TEXT NOT NULL,
  owner_user_id TEXT,
  confidential INTEGER NOT NULL DEFAULT 0 CHECK(confidential IN (0, 1)),
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  UNIQUE(project_slug, rel_path)
);
CREATE INDEX idx_file_meta_confidential ON file_meta(project_slug) WHERE confidential = 1;

CREATE TABLE file_acl (
  file_id TEXT NOT NULL REFERENCES file_meta(id) ON DELETE CASCADE,
  identity TEXT NOT NULL,
  granted_by TEXT,
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  PRIMARY KEY (file_id, identity)
);

ALTER TABLE documents ADD COLUMN confidential INTEGER NOT NULL DEFAULT 0;

INSERT OR IGNORE INTO schema_versions(version) VALUES (162);

COMMIT;
