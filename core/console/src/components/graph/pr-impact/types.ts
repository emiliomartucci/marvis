// v3.0.0 - 2026-05-17 - Codex types + cluster colors (match codex-canvas.jsx canonical)
import { z } from "zod";

export const PrArtifactIdZ = z
  .string()
  .regex(/^pr:artifact:[0-9a-f-]{36}$/)
  .max(64);

export const TouchKindZ = z.enum(["add", "modify", "delete"]);
export type TouchKind = z.infer<typeof TouchKindZ>;

export const ReviewStateZ = z.enum(["draft", "open", "merging", "merged", "closed"]);

export const PrMetadataZ = z.object({
  title: z.string().nullable(),
  branch: z.string(),
  review_state: ReviewStateZ,
  head_sha: z.string().nullable().optional(),
  base_sha: z.string().default("main"),
  populator_status: z.enum(["pending", "processed", "failed", "unknown"]).default("unknown"),
  function_nodes_returned: z.number().int().nonnegative(),
  function_cap_threshold: z.number().int().positive().default(800),
  function_nodes_capped: z.boolean().default(false),
});
export type PrMetadata = z.infer<typeof PrMetadataZ>;

export const ModifiedFunctionItemZ = z.object({
  node_id: z.string(),
  qualified_name_snapshot: z.string(),
  source_file: z.string(),
  touch_kind: TouchKindZ,
  lines_added: z.number().int().nonnegative(),
  lines_removed: z.number().int().nonnegative(),
  weight: z.number().min(0).max(1),
  blame_author: z.string().nullable().optional(),
  node_missing: z.boolean().default(false),
  first_seen_at: z.string().datetime().nullable().optional(),
});
export type ModifiedFunctionItem = z.infer<typeof ModifiedFunctionItemZ>;

export const TransitiveImpactItemZ = z.object({
  node_id: z.string(),
  depth: z.number().int().min(1).max(4),
  via_edge: z.enum(["calls", "imports", "defines"]),
});
export type TransitiveImpactItem = z.infer<typeof TransitiveImpactItemZ>;

export const VisibilityFooterZ = z.object({
  redacted_count: z.number().int().nonnegative().default(0),
});

export const PrImpactResponseZ = z.object({
  pr_id: PrArtifactIdZ,
  pr_metadata: PrMetadataZ,
  modified_functions: z.array(ModifiedFunctionItemZ),
  transitive_impact: z.array(TransitiveImpactItemZ),
  involved_projects: z.array(z.string()),
  visibility: VisibilityFooterZ,
  next_offset: z.number().int().nullable().optional(),
  total_estimate: z.number().int().nonnegative(),
  schema_version: z.literal("1.0"),
});
export type PrImpactResponse = z.infer<typeof PrImpactResponseZ>;

export const BranchItemZ = z.object({
  name: z.string(),
  head_sha: z.string().nullable().optional(),
  head_commit_at: z.string().nullable().optional(),
  is_main: z.boolean(),
  is_stale: z.boolean(),
  open_pr_ids: z.array(PrArtifactIdZ).default([]),
  age_days: z.number().int().nonnegative().nullable().optional(),
});
export type BranchItem = z.infer<typeof BranchItemZ>;

export const BranchesResponseZ = z.object({
  branches: z.array(BranchItemZ),
  main_head: z.string().nullable().optional(),
  main_head_at: z.string().nullable().optional(),
  next_offset: z.number().int().nullable().optional(),
  total_estimate: z.number().int().nonnegative(),
  schema_version: z.literal("1.0"),
});
export type BranchesResponse = z.infer<typeof BranchesResponseZ>;

export const ConflictPairZ = z.object({
  pr_ids: z.array(PrArtifactIdZ).min(2),
  shared_function_id: z.string(),
  shared_qualified_name: z.string(),
  touch_kinds: z.array(TouchKindZ),
});
export type ConflictPair = z.infer<typeof ConflictPairZ>;

export const ConflictsResponseZ = z.object({
  conflicts: z.array(ConflictPairZ),
  pr_ids_examined: z.array(PrArtifactIdZ),
  total: z.number().int().nonnegative(),
  schema_version: z.literal("1.0"),
});
export type ConflictsResponse = z.infer<typeof ConflictsResponseZ>;

// --- Codex modules + edges --------------------------------------------------

export const CodexClusterIdZ = z.enum([
  "auth", "db", "api", "ui", "parse", "search", "graph", "shared",
]);
export type CodexClusterId = z.infer<typeof CodexClusterIdZ>;

export const CodexModuleItemZ = z.object({
  slug: z.string().max(128),
  cluster: CodexClusterIdZ,
  label: z.string().max(64),
  function_count: z.number().int().nonnegative(),
  file_count: z.number().int().nonnegative(),
  degree: z.number().int().nonnegative().default(0),
  top_functions: z.array(z.string()).max(10).default([]),
  top_paths: z.array(z.string()).max(10).default([]),
  semantic_label: z.string().nullable().optional(),
  ratified: z.boolean().default(false),
  drift: z.number().int().nonnegative().default(0),
});
export type CodexModuleItem = z.infer<typeof CodexModuleItemZ>;

export const CodexModuleEdgeItemZ = z.object({
  source: z.string().max(128),
  target: z.string().max(128),
  relation: z.enum(["calls", "imports", "depends_on", "mentions"]),
  weight: z.number().int().nonnegative(),
  hot: z.boolean().default(false),
});
export type CodexModuleEdgeItem = z.infer<typeof CodexModuleEdgeItemZ>;

export const CodexModulesResponseZ = z.object({
  modules: z.array(CodexModuleItemZ),
  edges: z.array(CodexModuleEdgeItemZ).default([]),
  project: z.string(),
  total_estimate: z.number().int().nonnegative(),
  schema_version: z.literal("1.0"),
});
export type CodexModulesResponse = z.infer<typeof CodexModulesResponseZ>;

export const CodexFunctionItemZ = z.object({
  node_id: z.string(),
  qualified_name: z.string(),
  file_path: z.string().nullable().optional(),
  line_number: z.number().int().nonnegative().nullable().optional(),
  touch_count_7d: z.number().int().nonnegative(),
  touch_count_30d: z.number().int().nonnegative(),
});
export type CodexFunctionItem = z.infer<typeof CodexFunctionItemZ>;

export const CodexFunctionsResponseZ = z.object({
  functions: z.array(CodexFunctionItemZ),
  project: z.string(),
  module: z.string(),
  total_estimate: z.number().int().nonnegative(),
  schema_version: z.literal("1.0"),
});
export type CodexFunctionsResponse = z.infer<typeof CodexFunctionsResponseZ>;

/** 8 cluster colors — Okabe-Ito CVD-safe, match codex-page.jsx canonical. */
export const CLUSTER_COLORS: Record<CodexClusterId, { hue: number; sat: number; light: number; label: string }> = {
  auth:   { hue: 0,   sat: 70, light: 56, label: "auth" },
  db:     { hue: 204, sat: 70, light: 56, label: "db" },
  api:    { hue: 18,  sat: 95, light: 54, label: "api" },
  ui:     { hue: 290, sat: 60, light: 62, label: "ui" },
  parse:  { hue: 38,  sat: 90, light: 56, label: "parse" },
  search: { hue: 180, sat: 55, light: 50, label: "search" },
  graph:  { hue: 156, sat: 55, light: 42, label: "graph" },
  shared: { hue: 30,  sat: 8,  light: 60, label: "shared" },
};

export function clusterColor(id: CodexClusterId, alpha = 1): string {
  const c = CLUSTER_COLORS[id] ?? CLUSTER_COLORS.shared;
  return `hsl(${c.hue} ${c.sat}% ${c.light}% / ${alpha})`;
}

export const CLUSTER_HSL: Record<CodexClusterId, string> = {
  auth:   clusterColor("auth"),
  db:     clusterColor("db"),
  api:    clusterColor("api"),
  ui:     clusterColor("ui"),
  parse:  clusterColor("parse"),
  search: clusterColor("search"),
  graph:  clusterColor("graph"),
  shared: clusterColor("shared"),
};
