// @generated — do not edit manually
// Regenerate with: npm run gen:types
// Source: backend Pydantic models in api/models/graph_ux.py + graph endpoint shapes
// Generator: openapi-zod-client v1.18.3 + schemas-only template
// Last generated: 2026-04-20

import { z } from "zod";

export const HotspotItemSchema = z
  .object({
    node_id: z.string(),
    label: z.string(),
    kind: z.string(),
    touch_count: z.number().int(),
    authors: z.array(z.string()),
  })
  .passthrough();
export const RecentItemSchema = z
  .object({
    kind: z.enum(["commit", "pr", "task", "handoff"]),
    node_id: z.string(),
    label: z.string(),
    at: z.string().datetime({ offset: true }),
  })
  .passthrough();
export const PinOutSchema = z
  .object({
    node_id: z.string(),
    pinned_at: z.string().datetime({ offset: true }),
    note: z.union([z.string(), z.null()]).optional(),
    is_stale: z.boolean().optional().default(false),
  })
  .passthrough();
export const LandingBundleSchema = z
  .object({
    hotspots: z.array(HotspotItemSchema),
    recent: z.array(RecentItemSchema),
    saved_nodes: z.array(PinOutSchema),
  })
  .passthrough();
export const PinInSchema = z
  .object({
    node_id: z
      .string()
      .min(6)
      .max(256)
      .regex(/^[a-z]+:[a-z]+:.+$/),
    note: z.union([z.string(), z.null()]).optional(),
  })
  .passthrough();
export const ValidationErrorSchema = z
  .object({
    loc: z.array(z.union([z.string(), z.number()])),
    msg: z.string(),
    type: z.string(),
    input: z.unknown().optional(),
    ctx: z.object({}).partial().passthrough().optional(),
  })
  .passthrough();
export const HTTPValidationErrorSchema = z
  .object({ detail: z.array(ValidationErrorSchema) })
  .partial()
  .passthrough();
export const ResolveOutSchema = z
  .object({ node_id: z.string(), kind: z.string() })
  .passthrough();
export const OverviewNodeSchema = z
  .object({
    id: z.string(),
    type: z.string(),
    label: z.string(),
    sub_nodes: z.union([z.number(), z.null()]).optional(),
    metadata: z.object({}).partial().passthrough().optional(),
  })
  .passthrough();
export const OverviewEdgeSchema = z
  .object({
    source: z.string(),
    target: z.string(),
    relation: z.string(),
    weight: z.number().int().optional().default(1),
  })
  .passthrough();
export const OverviewBundleSchema = z
  .object({
    level: z.enum(["macro", "module"]),
    scope: z.union([z.string(), z.null()]).optional(),
    nodes: z.array(OverviewNodeSchema),
    edges: z.array(OverviewEdgeSchema),
    hidden_cross_project_count: z.number().int().optional().default(0),
  })
  .passthrough();
export const OrphanFileSchema = z
  .object({
    node_id: z.string(),
    label: z.string(),
    path: z.string(),
    last_modified: z.union([z.string(), z.null()]).optional(),
  })
  .passthrough();
export const OrphanSubClusterSchema = z
  .object({
    folder: z.string(),
    color: z.string(),
    count: z.number().int(),
    files: z.array(OrphanFileSchema),
    overflow_count: z.number().int().optional().default(0),
  })
  .passthrough();
export const OrphansBundleSchema = z
  .object({ scope: z.string(), sub_clusters: z.array(OrphanSubClusterSchema) })
  .passthrough();
export const NeighborEdgeSchema = z
  .object({
    relation: z.string(),
    direction: z.string(),
    source_file: z.union([z.string(), z.null()]).optional(),
    source_line: z.union([z.number(), z.null()]).optional(),
  })
  .passthrough();
export const NeighborNodeSchema = z
  .object({
    id: z.string(),
    type: z.string(),
    name: z.string(),
    qualified_name: z.string(),
    file_path: z.union([z.string(), z.null()]).optional(),
    line_number: z.union([z.number(), z.null()]).optional(),
    metadata: z
      .union([z.object({}).partial().passthrough(), z.null()])
      .optional(),
    edge: z.union([NeighborEdgeSchema, z.null()]).optional(),
    score: z.union([z.number(), z.null()]).optional(),
    classification: z.union([z.string(), z.null()]).optional(),
    signals: z
      .union([z.object({}).partial().passthrough(), z.null()])
      .optional(),
  })
  .passthrough();
export const NeighborsResponseSchema = z
  .object({
    node_id: z.string(),
    neighbors: z.array(NeighborNodeSchema),
    count: z.number().int(),
  })
  .passthrough();
export const DirectHotspotItemSchema = z
  .object({
    id: z.string(),
    type: z.string(),
    name: z.string(),
    qualified_name: z.string(),
    file_path: z.union([z.string(), z.null()]).optional(),
    touch_count_total: z.number().int(),
    touch_count_7d: z.number().int(),
    touch_count_30d: z.number().int(),
    touch_authors: z.array(z.string()),
    touch_last_at: z.union([z.string(), z.null()]).optional(),
  })
  .passthrough();
export const HotspotsResponseSchema = z
  .object({
    window: z.string(),
    type_filter: z.union([z.string(), z.null()]).optional(),
    project: z.union([z.string(), z.null()]).optional(),
    hotspots: z.array(DirectHotspotItemSchema),
    count: z.number().int(),
  })
  .passthrough();
export const ImpactSummarySchema = z
  .object({
    suspect: z.number().int(),
    uncertain: z.number().int(),
    legitimate: z.number().int(),
    direct: z.number().int(),
    transitive: z.number().int(),
    truncated: z.boolean(),
  })
  .passthrough();
export const ImpactResponseSchema = z
  .object({
    target: z.string(),
    direct_callers: z.array(NeighborNodeSchema),
    transitive_callers: z.array(NeighborNodeSchema),
    summary: ImpactSummarySchema,
  })
  .passthrough();
export const ContextArtifactSchema = z
  .object({
    id: z.string(),
    type: z.string(),
    name: z.string(),
    qualified_name: z.string(),
    file_path: z.union([z.string(), z.null()]).optional(),
    line_number: z.union([z.number(), z.null()]).optional(),
    metadata: z
      .union([z.object({}).partial().passthrough(), z.null()])
      .optional(),
  })
  .passthrough();
export const ContextCountsSchema = z
  .object({
    commits: z.number().int(),
    prs: z.number().int(),
    tasks: z.number().int(),
    handoffs: z.number().int(),
    learnings: z.number().int(),
  })
  .passthrough();
export const ContextChainSchema = z
  .object({
    node: ContextArtifactSchema,
    commits: z.array(ContextArtifactSchema),
    prs: z.array(ContextArtifactSchema),
    tasks: z.array(ContextArtifactSchema),
    handoffs: z.array(ContextArtifactSchema),
    learnings: z.array(ContextArtifactSchema),
    counts: ContextCountsSchema,
  })
  .passthrough();

export type HotspotItem = z.infer<typeof HotspotItemSchema>;
export type RecentItem = z.infer<typeof RecentItemSchema>;
export type PinOut = z.infer<typeof PinOutSchema>;
export type LandingBundle = z.infer<typeof LandingBundleSchema>;
export type PinIn = z.infer<typeof PinInSchema>;
export type ValidationError = z.infer<typeof ValidationErrorSchema>;
export type HTTPValidationError = z.infer<typeof HTTPValidationErrorSchema>;
export type ResolveOut = z.infer<typeof ResolveOutSchema>;
export type OverviewNode = z.infer<typeof OverviewNodeSchema>;
export type OverviewEdge = z.infer<typeof OverviewEdgeSchema>;
export type OverviewBundle = z.infer<typeof OverviewBundleSchema>;
export type OrphanFile = z.infer<typeof OrphanFileSchema>;
export type OrphanSubCluster = z.infer<typeof OrphanSubClusterSchema>;
export type OrphansBundle = z.infer<typeof OrphansBundleSchema>;
export type NeighborEdge = z.infer<typeof NeighborEdgeSchema>;
export type NeighborNode = z.infer<typeof NeighborNodeSchema>;
export type NeighborsResponse = z.infer<typeof NeighborsResponseSchema>;
export type DirectHotspotItem = z.infer<typeof DirectHotspotItemSchema>;
export type HotspotsResponse = z.infer<typeof HotspotsResponseSchema>;
export type ImpactSummary = z.infer<typeof ImpactSummarySchema>;
export type ImpactResponse = z.infer<typeof ImpactResponseSchema>;
export type ContextArtifact = z.infer<typeof ContextArtifactSchema>;
export type ContextCounts = z.infer<typeof ContextCountsSchema>;
export type ContextChain = z.infer<typeof ContextChainSchema>;

// ---------------------------------------------------------------------------
// Named exports map
// ---------------------------------------------------------------------------

export const schemas = {
  HotspotItemSchema,
  RecentItemSchema,
  PinOutSchema,
  LandingBundleSchema,
  PinInSchema,
  ValidationErrorSchema,
  HTTPValidationErrorSchema,
  ResolveOutSchema,
  OverviewNodeSchema,
  OverviewEdgeSchema,
  OverviewBundleSchema,
  OrphanFileSchema,
  OrphanSubClusterSchema,
  OrphansBundleSchema,
  NeighborEdgeSchema,
  NeighborNodeSchema,
  NeighborsResponseSchema,
  DirectHotspotItemSchema,
  HotspotsResponseSchema,
  ImpactSummarySchema,
  ImpactResponseSchema,
  ContextArtifactSchema,
  ContextCountsSchema,
  ContextChainSchema,
};
