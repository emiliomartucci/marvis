// v1.0.0 - 2026-04-24 - Minimal graph resolve helper per useGraphNodeFromPath.
//
// Nato dal cleanup PR #2 graph-cosmo: graphApi.ts/graphTypes.ts/graphParse.ts
// sono rimossi insieme a graph/universe. Qui isoliamo SOLO il bridge
// path -> node_id usato dal pulsante "Open in Graph" del Finder.
import { fetchAPIValidated } from "./api";
import { ResolveOutSchema } from "@/generated/api";
import type { z } from "zod";

export type ResolveResult = z.infer<typeof ResolveOutSchema>;

/** Resolve filesystem path -> KG node_id (404 -> rejected). */
export async function getGraphResolve(
  path: string,
  opts?: { signal?: AbortSignal },
): Promise<ResolveResult> {
  const qs = new URLSearchParams({ path });
  return fetchAPIValidated<ResolveResult>(
    `/api/v1/graph/resolve?${qs.toString()}`,
    ResolveOutSchema,
    { signal: opts?.signal },
  );
}
