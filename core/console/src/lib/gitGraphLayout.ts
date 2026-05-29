import type { GitGraphCommit, GraphNode, GraphEdge } from "./types";

export const COMMIT_SPACING = 36;
export const LANE_SPACING = 24;
export const NODE_RADIUS = 5;
export const GRAPH_PADDING = 16;

const LANE_COLORS = [
  "hsl(212 100% 67%)",  // accent blue
  "hsl(140 60% 50%)",   // green
  "hsl(270 60% 65%)",   // purple
  "hsl(180 60% 50%)",   // cyan
  "hsl(40 80% 50%)",    // amber
  "hsl(330 70% 60%)",   // pink
  "hsl(20 80% 55%)",    // orange
  "hsl(160 50% 55%)",   // teal
];

export interface LayoutResult {
  nodes: GraphNode[];
  edges: GraphEdge[];
  maxLane: number;
}

/**
 * Compute graph layout from topologically-sorted commits.
 * Assigns lanes (X position) and computes edges between commits.
 */
export function computeLayout(commits: GitGraphCommit[]): LayoutResult {
  if (commits.length === 0) {
    return { nodes: [], edges: [], maxLane: 0 };
  }

  const hashToRow = new Map<string, number>();
  commits.forEach((c, i) => hashToRow.set(c.hash, i));

  // lanes[i] = hash of commit this lane is "waiting for" (reserved by a child)
  const lanes: (string | null)[] = [];
  const nodes: GraphNode[] = [];
  const edges: GraphEdge[] = [];
  let maxLane = 0;

  for (let row = 0; row < commits.length; row++) {
    const commit = commits[row];

    // Find lane: check if any lane is reserved for this commit
    let assignedLane = -1;
    for (let l = 0; l < lanes.length; l++) {
      if (lanes[l] === commit.hash) {
        assignedLane = l;
        break;
      }
    }

    if (assignedLane === -1) {
      // New branch head: find first free lane or create new one
      const freeLane = lanes.indexOf(null);
      assignedLane = freeLane >= 0 ? freeLane : lanes.length;
      if (assignedLane >= lanes.length) {
        lanes.push(null);
      }
    }

    const color = LANE_COLORS[assignedLane % LANE_COLORS.length];

    nodes.push({
      commit,
      row,
      lane: assignedLane,
      color,
    });

    if (maxLane < assignedLane) maxLane = assignedLane;

    // First parent: continues in same lane
    if (commit.parents.length > 0) {
      const firstParent = commit.parents[0];
      const parentRow = hashToRow.get(firstParent);

      // Check if another lane is already reserved for this parent
      let parentAlreadyReserved = false;
      for (let l = 0; l < lanes.length; l++) {
        if (l !== assignedLane && lanes[l] === firstParent) {
          // Merge: another child already reserved this parent in a different lane
          parentAlreadyReserved = true;
          edges.push({
            fromHash: commit.hash,
            toHash: firstParent,
            fromLane: assignedLane,
            toLane: l,
            fromRow: row,
            toRow: parentRow ?? row + 1,
            color,
            type: "merge",
          });
          // Free this lane since it converges
          lanes[assignedLane] = null;
          break;
        }
      }

      if (!parentAlreadyReserved) {
        lanes[assignedLane] = firstParent;
        edges.push({
          fromHash: commit.hash,
          toHash: firstParent,
          fromLane: assignedLane,
          toLane: assignedLane,
          fromRow: row,
          toRow: parentRow ?? row + 1,
          color,
          type: "branch",
        });
      }
    } else {
      // Root commit: free the lane
      lanes[assignedLane] = null;
    }

    // Additional parents (merge commits)
    for (let p = 1; p < commit.parents.length; p++) {
      const mergeParent = commit.parents[p];
      const parentRow = hashToRow.get(mergeParent);

      // Find which lane this parent lives in
      let parentLane = -1;
      for (let l = 0; l < lanes.length; l++) {
        if (lanes[l] === mergeParent) {
          parentLane = l;
          break;
        }
      }

      // If parent not yet assigned to a lane, find its node
      if (parentLane === -1 && parentRow !== undefined) {
        const parentNode = nodes.find((n) => n.commit.hash === mergeParent);
        parentLane = parentNode ? parentNode.lane : assignedLane;
      }

      if (parentLane === -1) parentLane = assignedLane;

      edges.push({
        fromHash: commit.hash,
        toHash: mergeParent,
        fromLane: assignedLane,
        toLane: parentLane,
        fromRow: row,
        toRow: parentRow ?? row + 1,
        color: LANE_COLORS[parentLane % LANE_COLORS.length],
        type: "merge",
      });
    }
  }

  return { nodes, edges, maxLane };
}

/** SVG path for a Bezier curve connecting two points (for merge edges). */
export function bezierPath(
  x1: number, y1: number,
  x2: number, y2: number,
): string {
  if (x1 === x2) {
    // Same lane: straight vertical line
    return `M ${x1} ${y1} L ${x2} ${y2}`;
  }
  const cy1 = y1 + (y2 - y1) * 0.4;
  const cy2 = y1 + (y2 - y1) * 0.6;
  return `M ${x1} ${y1} C ${x1} ${cy1}, ${x2} ${cy2}, ${x2} ${y2}`;
}

/** Convert lane index to X pixel coordinate. */
export function laneToX(lane: number): number {
  return GRAPH_PADDING + lane * LANE_SPACING + LANE_SPACING / 2;
}

/** Convert row index to Y pixel coordinate. */
export function rowToY(row: number): number {
  return COMMIT_SPACING / 2 + row * COMMIT_SPACING;
}
