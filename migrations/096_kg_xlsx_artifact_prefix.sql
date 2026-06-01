-- Migration 096 — KG XLSX artifact prefix (code-level).
--
-- Story E4.2 indexes approved `.xlsx` files as:
--   xlsx:artifact:<sha256>                  (workbook parent)
--   xlsx:sheet:<sha256>.<index>.<sheet>     (sheet child)
--
-- SQLite does not enforce the node-id prefix/kind regex; that lives in
-- api/services/graph_service.py::NODE_ID_PATTERN and mcp-pir/index.mjs. The
-- graph_nodes.type column remains `file` for both workbook and sheet rows, so
-- no CHECK rebuild is required here. This migration pins the code-level schema
-- change in schema_versions for audit/deploy ordering.

BEGIN IMMEDIATE;

INSERT OR IGNORE INTO schema_versions (version) VALUES (96);

COMMIT;
