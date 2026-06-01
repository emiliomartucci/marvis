#!/usr/bin/env node
// v1.0.0 - 2026-04-20 - Generate Zod schemas + TS types from backend Pydantic models
//
// Usage:
//   npm run gen:types
//
// What it does:
//   1. Generates a minimal OpenAPI spec from the backend Pydantic graph models
//      (avoids the broken /openapi.json endpoint caused by from __future__ annotations + Request ForwardRef)
//   2. Runs openapi-zod-client (schemas-only template) to produce src/generated/api.ts
//   3. Post-processes the output to add TS type exports and proper Schema name suffixes
//
// Regenerate whenever core/api/models/graph_ux.py or graph endpoint response shapes change.
// Commit src/generated/api.ts — do NOT add to .gitignore.

import { execSync, spawnSync } from "child_process";
import { writeFileSync, mkdirSync, readFileSync, existsSync } from "fs";
import { join, dirname } from "path";
import { fileURLToPath } from "url";

const __dirname = dirname(fileURLToPath(import.meta.url));
// Resolve monorepo root — works from worktrees (../../../) and from main workspace (../../)
function findMonorepoRoot(startDir) {
  let d = startDir;
  for (let i = 0; i < 5; i++) {
    if (existsSync(join(d, "core/api/models/graph_ux.py"))) return d + "/";
    d = dirname(d);
  }
  throw new Error("Cannot find monorepo root (core/api/models/graph_ux.py not found)");
}
const ROOT = findMonorepoRoot(__dirname);
const CONSOLE_DIR = join(__dirname, "..");
const GENERATED_DIR = join(CONSOLE_DIR, "src/generated");
const OUTPUT_FILE = join(GENERATED_DIR, "api.ts");
const SPEC_TMP = "/tmp/pir-graph-openapi.json";
const TEMPLATE = join(
  CONSOLE_DIR,
  "node_modules/openapi-zod-client/src/templates/schemas-only.hbs"
);

// Generate OpenAPI spec via Python (avoids broken live endpoint)
console.log("Generating OpenAPI spec from Pydantic models...");
const pyScript = `
import sys, json
sys.path.insert(0, '${ROOT}')
from fastapi import FastAPI
from core.api.models.graph_ux import (
    LandingBundle, HotspotItem, RecentItem, PinOut, PinIn,
    OverviewBundle, OverviewNode, OverviewEdge,
    OrphansBundle, OrphanFile, OrphanSubCluster, ResolveOut
)
from pydantic import BaseModel
from typing import Optional, Any

class NeighborEdge(BaseModel):
    relation: str
    direction: str
    source_file: Optional[str] = None
    source_line: Optional[int] = None

class NeighborNode(BaseModel):
    id: str
    type: str
    name: str
    qualified_name: str
    file_path: Optional[str] = None
    line_number: Optional[int] = None
    metadata: Optional[dict[str, Any]] = None
    edge: Optional[NeighborEdge] = None
    score: Optional[float] = None
    classification: Optional[str] = None
    signals: Optional[dict[str, Any]] = None

class NeighborsResponse(BaseModel):
    node_id: str
    neighbors: list[NeighborNode]
    count: int

class ImpactSummary(BaseModel):
    suspect: int
    uncertain: int
    legitimate: int
    direct: int
    transitive: int
    truncated: bool

class ImpactResponse(BaseModel):
    target: str
    direct_callers: list[NeighborNode]
    transitive_callers: list[NeighborNode]
    summary: ImpactSummary

class ContextArtifact(BaseModel):
    id: str
    type: str
    name: str
    qualified_name: str
    file_path: Optional[str] = None
    line_number: Optional[int] = None
    metadata: Optional[dict[str, Any]] = None

class ContextCounts(BaseModel):
    commits: int
    prs: int
    tasks: int
    handoffs: int
    learnings: int

class ContextChain(BaseModel):
    node: ContextArtifact
    commits: list[ContextArtifact]
    prs: list[ContextArtifact]
    tasks: list[ContextArtifact]
    handoffs: list[ContextArtifact]
    learnings: list[ContextArtifact]
    counts: ContextCounts

class DirectHotspotItem(BaseModel):
    id: str
    type: str
    name: str
    qualified_name: str
    file_path: Optional[str] = None
    touch_count_total: int
    touch_count_7d: int
    touch_count_30d: int
    touch_authors: list[str]
    touch_last_at: Optional[str] = None

class HotspotsResponse(BaseModel):
    window: str
    type_filter: Optional[str] = None
    project: Optional[str] = None
    hotspots: list[DirectHotspotItem]
    count: int

app = FastAPI(title='PiR API', version='1.0.0')

@app.get('/api/v1/graph/landing', response_model=LandingBundle)
async def landing(): pass
@app.get('/api/v1/graph/pins', response_model=list[PinOut])
async def pins(): pass
@app.post('/api/v1/graph/pins', response_model=PinOut)
async def create_pin(body: PinIn): pass
@app.delete('/api/v1/graph/pins/{node_id}', response_model=dict)
async def delete_pin(node_id: str): pass
@app.get('/api/v1/graph/resolve', response_model=ResolveOut)
async def resolve(path: str): pass
@app.get('/api/v1/graph/overview', response_model=OverviewBundle)
async def overview(): pass
@app.get('/api/v1/graph/orphans', response_model=OrphansBundle)
async def orphans(): pass
@app.get('/api/v1/graph/neighbors/{node_id}', response_model=NeighborsResponse)
async def neighbors(node_id: str): pass
@app.get('/api/v1/graph/hotspots', response_model=HotspotsResponse)
async def hotspots(): pass
@app.get('/api/v1/graph/impact/{node_id}', response_model=ImpactResponse)
async def impact(node_id: str): pass
@app.get('/api/v1/graph/context/{node_id}', response_model=ContextChain)
async def context(node_id: str): pass

print(json.dumps(app.openapi()))
`;

const pyResult = spawnSync("python3", ["-c", pyScript], {
  encoding: "utf8",
  maxBuffer: 10 * 1024 * 1024,
});

if (pyResult.status !== 0) {
  console.error("Python OpenAPI generation failed:");
  console.error(pyResult.stderr);
  process.exit(1);
}

writeFileSync(SPEC_TMP, pyResult.stdout.trim());
console.log(`  OpenAPI spec written to ${SPEC_TMP}`);

// Run openapi-zod-client with schemas-only template
console.log("Running openapi-zod-client...");
mkdirSync(GENERATED_DIR, { recursive: true });

const rawOutput = join(GENERATED_DIR, "_api_raw.ts");
const genResult = spawnSync(
  "npx",
  [
    "openapi-zod-client",
    SPEC_TMP,
    "-o",
    rawOutput,
    "--export-schemas",
    "-t",
    TEMPLATE,
  ],
  { encoding: "utf8", cwd: CONSOLE_DIR }
);

if (genResult.status !== 0) {
  console.error("openapi-zod-client failed:");
  console.error(genResult.stderr);
  process.exit(1);
}

console.log("  Raw schemas generated.");

// Post-process: add header, rename const X = z... to export const XSchema = z...,
// add TypeScript type exports, add named schema map
const raw = readFileSync(rawOutput, "utf8");

// Extract the import line + each schema block
const lines = raw.split("\n");
const importLine = lines[0]; // import { z } from "zod";

// Parse schema declarations: `const Foo = z...` (potentially multi-line)
// We'll do a text transform: rename consts + add type exports + Schema suffix
let processed = raw;

// Remove the trailing `export const schemas = { ... }` block (we'll rebuild it)
processed = processed.replace(/^export const schemas[\s\S]+$/m, "").trim();

// Find all top-level const names
const constNames = [];
const constRegex = /^const (\w+) = /gm;
let match;
while ((match = constRegex.exec(processed)) !== null) {
  constNames.push(match[1]);
}

// Rename: `const Foo = z` → `export const FooSchema = z`
// and build TS type: `export type Foo = z.infer<typeof FooSchema>;`
for (const name of constNames) {
  // Replace declaration
  processed = processed.replace(
    new RegExp(`^const ${name} = `, "m"),
    `export const ${name}Schema = `
  );
  // Fix internal references: `z.array(Foo)` → `z.array(FooSchema)` etc.
  // (use word boundary to avoid partial matches)
  processed = processed.replace(
    new RegExp(`(?<![A-Za-z])${name}(?![A-Za-z0-9_])(?!Schema)`, "g"),
    `${name}Schema`
  );
}

// Re-fix the import line (regex may have mangled z → zSchema)
processed = processed.replace(/^import \{ zSchema \}/m, `import { z }`);
// Fix any z.xxx that got mangled
processed = processed.replace(/\bzSchema\b/g, "z");

// Add type exports after each schema
const typeExports = constNames
  .map((name) => `export type ${name} = z.infer<typeof ${name}Schema>;`)
  .join("\n");

// Add named schemas map
const schemasMap =
  `\n// ---------------------------------------------------------------------------\n` +
  `// Named exports map\n` +
  `// ---------------------------------------------------------------------------\n\n` +
  `export const schemas = {\n` +
  constNames.map((n) => `  ${n}Schema,`).join("\n") +
  `\n};\n`;

const header = `// @generated — do not edit manually
// Regenerate with: npm run gen:types
// Source: backend Pydantic models in core/api/models/graph_ux.py + graph endpoint shapes
// Generator: openapi-zod-client v1.18.3 + schemas-only template
// Last generated: ${new Date().toISOString().slice(0, 10)}
`;

const final = `${header}\n${processed}\n\n${typeExports}\n${schemasMap}`;
writeFileSync(OUTPUT_FILE, final, "utf8");

// Clean up raw file
import { unlinkSync } from "fs";
unlinkSync(rawOutput);

console.log(`\nDone! Written to: src/generated/api.ts`);
console.log(`Schemas exported: ${constNames.join(", ")}`);
