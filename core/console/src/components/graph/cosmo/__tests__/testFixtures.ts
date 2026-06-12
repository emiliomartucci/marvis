// v1.1.0 - 2026-04-24 - SatelliteSummary fixture shape (BE v1.1.0)
// v1.0.0 - 2026-04-24 - Test fixture unico (post mockGraph removal, PR #3)
//
// Dataset in-memory condiviso dai test dei layout + canvas. NON e' un mock
// runtime — solo input deterministico per unit test. Shape identico a quello
// che il backend serve via `/graph/cosmo` (Project + Edge object).
//
// Dimensioni: 28 project (hub marvisx + satelliti + indirect) e 28 edge
// parametrizzati per produrre >=2 orbit depth in layoutConstellation e
// degree=142 su marvisx (test canvas).

import type { Edge, Kind, Program, Project, SatelliteSummary } from "../types";

/** Helper per costruire un SatelliteSummary con default count=1. */
function sat(kind: Kind, count = 1, latest_at: string | null = null): SatelliteSummary {
  return { kind, count, latest_at };
}

function makeProject(
  slug: string,
  program: Program,
  degree: number,
  satellites: Project["satellites"] = [],
  color: string | null = null,
): Project {
  return { slug, program, color, degree, satellites };
}

// --- Direct neighbours of marvisx (depth 1) ---
const DIRECT_SLUGS: ReadonlyArray<readonly [string, Program]> = [
  ["brain", "marvis"],
  ["acme-site", "personal"],
  ["acme-tool", "personal"],
  ["team-a", "personal"],
  ["aut-lab", "personal"],
  ["beta-core", "personal"],
  ["gamma-site", "personal"],
  ["delta-app", "personal"],
  ["epsilon", "personal"],
  ["expense-tracker", "personal"],
  ["zeta-core", "personal"],
  ["eta-app", "personal"],
];

// --- Indirect neighbours (depth 2) ---
const INDIRECT_SLUGS: ReadonlyArray<readonly [string, Program, string]> = [
  ["brain-sub-1", "marvis", "brain"],
  ["brain-sub-2", "marvis", "brain"],
  ["acme-sub-1", "personal", "acme-site"],
  ["acme-sub-2", "personal", "acme-site"],
  ["acme-tool-sub-1", "personal", "acme-tool"],
  ["beta-sub-1", "personal", "beta-core"],
  ["gamma-sub-1", "personal", "gamma-site"],
];

// --- Unreachable nodes (depth 3) ---
const UNREACHABLE_SLUGS: ReadonlyArray<readonly [string, Program]> = [
  ["isolated-1", "personal"],
  ["isolated-2", "personal"],
  ["isolated-3", "marvis"],
  ["isolated-4", "personal"],
  ["isolated-5", "personal"],
  ["isolated-6", "personal"],
  ["isolated-7", "personal"],
  ["isolated-8", "personal"],
];

/**
 * 28 project: marvisx hub (degree=142 per match atteso dal test canvas) +
 * 12 direct + 7 indirect + 8 unreachable. Totale 28.
 */
export const FIXTURE_PROJECTS: readonly Project[] = [
  makeProject(
    "marvisx",
    "marvis",
    142,
    [
      sat("plan", 5, "2026-04-22T10:00:00Z"),
      sat("task", 12, "2026-04-21T10:00:00Z"),
      sat("handoff", 3, "2026-04-20T10:00:00Z"),
      sat("solution", 2, "2026-04-19T10:00:00Z"),
    ],
    "#56b4e9",
  ),
  ...DIRECT_SLUGS.map(([slug, prog]) =>
    makeProject(slug, prog, 5, [sat("task", 1, null)]),
  ),
  ...INDIRECT_SLUGS.map(([slug, prog]) => makeProject(slug, prog, 2, [])),
  ...UNREACHABLE_SLUGS.map(([slug, prog]) => makeProject(slug, prog, 0, [])),
];

/**
 * Edge list:
 * - 12 spoke marvisx → direct (mentions, weight 1-3)
 * - 7 spoke direct-parent → indirect (depends_on, weight 1)
 * - 0 isolated/unreachable edges (collocati su orbit 3 dal layout)
 */
const SPOKE_EDGES: Edge[] = DIRECT_SLUGS.map(([slug], i) => ({
  source: "marvisx",
  target: slug,
  relation: i % 2 === 0 ? "mentions" : "depends_on",
  weight: (i % 3) + 1,
}));

const INDIRECT_EDGES: Edge[] = INDIRECT_SLUGS.map(([slug, , parent]) => ({
  source: parent,
  target: slug,
  relation: "depends_on" as const,
  weight: 1,
}));

export const FIXTURE_EDGES: readonly Edge[] = [
  ...SPOKE_EDGES,
  ...INDIRECT_EDGES,
];
