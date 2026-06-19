// v1.0.0 - 2026-04-22 - Resolve filesystem path to KG node_id for Finder v2 viewer toolbar
"use client";

import { useEffect, useState } from "react";
import { getGraphResolve } from "@/lib/graphResolveApi";

export type GraphResolveState = "unknown" | "found" | "not_found";

interface GraphNodeResult {
  state: GraphResolveState;
  nodeId: string | null;
}

/**
 * Resolve a filesystem path to a Knowledge Graph node_id.
 * Returns "unknown" while resolving, "found" with node_id if indexed,
 * "not_found" if backend returns 404 (path not indexed / visibility miss).
 *
 * No-op (state="unknown") when graph UX flag is off — callers should
 * render the button hidden/disabled in that case.
 */
export function useGraphNodeFromPath(path: string | null): GraphNodeResult {
  const [state, setState] = useState<GraphResolveState>("unknown");
  const [nodeId, setNodeId] = useState<string | null>(null);

  useEffect(() => {
    if (!path) {
      setState("unknown");
      setNodeId(null);
      return;
    }
    if (process.env.NEXT_PUBLIC_ENABLE_GRAPH_UX !== "true") {
      // Flag off — skip network, stay "unknown" (viewer hides button)
      return;
    }
    const ctrl = new AbortController();
    setState("unknown");
    setNodeId(null);
    getGraphResolve(path, { signal: ctrl.signal })
      .then((res) => {
        setNodeId(res.node_id);
        setState("found");
      })
      .catch((e) => {
        if ((e as Error).name !== "AbortError") {
          setState("not_found");
          setNodeId(null);
        }
      });
    return () => ctrl.abort();
  }, [path]);

  return { state, nodeId };
}
