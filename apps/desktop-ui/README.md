# MarvisX Console
Mission Control Portal for MarvisX Team

## API types codegen

Zod schemas and TS types for `/api/v1/graph/*` endpoints are generated from the backend Pydantic models:

```bash
npm run gen:types
```

Regenerate whenever `api/models/graph_ux.py` or graph endpoint response shapes change.
Commit `console/src/generated/api.ts` — it is the source of truth for frontend types.

**How it works:** the script imports Pydantic models directly via Python (bypassing the broken
`/openapi.json` live endpoint) and runs `openapi-zod-client` with the schemas-only template.
Output is post-processed to add `Schema` suffix + `z.infer<>` type exports.

**Not impacted:** MCP tools (`mcp-pir/index.mjs`) — keep manual Zod schemas there.

**Import surface:** always import from `@/lib/graphTypes` (re-exports from generated + keeps
custom types like `GraphKgNode`, `NodeId`, `OrphanNode`). Never import from `@/generated/api` directly.
