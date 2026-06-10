// v1.2.0 - 2026-04-24 - ProjectZ.degree float (SUM weight aggregate, was int count).
// v1.1.0 - 2026-04-24 - SatelliteItemZ + SatelliteSummaryZ.items per file-dot cliccabili.
// v1.0.0 - 2026-04-24 - Types + Zod schemas per Graph Cosmo Canvas (PR #1 foundation).
//
// Single source of truth per types grafo Cosmo. Il design del canvas
// (sostituisce UniverseCanvas Sigma.js) deriva TS types da Zod via z.infer
// (H-10 piano): schema e' la sorgente, TS segue. Edge e' un oggetto
// `{source, target, relation, weight}` (M-BE-07 + M-FE-09 piano), non tuple —
// rename silenzioso su tuple = bug; rename su object = compile error.
//
// NOTA: ViewStateZ / ProgramZ / KindZ sono strict (no `.catch(DEFAULT)` qui).
// Il fallback con `safeParse` + `console.warn` vive nel caller hook
// `useGraphViewState` (PR #2, M-FE-10 piano). Qui schema puro, nessuna politica.
//
// File framework-free: zero import React. Consumabile da Node per i test.

import { z } from "zod";

// -----------------------------------------------------------------------------
// Enums dominio
// -----------------------------------------------------------------------------

/** Tipi di artefatto che un progetto puo' "irradiare" come satellite. */
export const KindZ = z.enum([
  "plan",
  "brainstorm",
  "solution",
  "audit",
  "research",
  "handoff",
  "task",
  "learning",
]);
export type Kind = z.infer<typeof KindZ>;

/**
 * Programmi / macro-aree progetto.
 *
 * Il set di programmi conosciuti dal frontend è volutamente minimo e
 * neutro. `.catch("personal")` (forward-compat): qualunque program che il
 * backend introduca e che il frontend non conosce ancora ricade in modo
 * sicuro su `personal` invece di far fallire il parse. Programmi specifici
 * di un deployment sono gestiti da questo fallback, non hardcodati qui.
 */
export const ProgramZ = z
  .enum([
    "marvis",
    "personal",
  ])
  .catch("personal");
export type Program = z.infer<typeof ProgramZ>;

/**
 * Relazioni cross-project aggregate che il canvas disegna come edge.
 * @lintignore — consumato da BE adapter PR #3 (knip skip).
 */
export const EdgeRelationZ = z.enum(["mentions", "depends_on"]);
/** @lintignore — consumato da BE adapter PR #3. */
export type EdgeRelation = z.infer<typeof EdgeRelationZ>;

/**
 * Modalita Beautify. Aggiornata 2026-05-17 (PR4): `force` rimossa, sostituita
 * dai 2 preset specifici `grappolo` (dense cluster) e `sistema-solare`
 * (orbite larghe). Constellation + galaxy invariati, ora con zero overlap.
 * @public
 */
export type BeautifyKind =
  | "constellation"
  | "galaxy"
  | "grappolo"
  | "sistema-solare"
  | "reset";

// -----------------------------------------------------------------------------
// Schemi dati dominio (progetti + edges)
// -----------------------------------------------------------------------------

/**
 * Singolo artifact dentro un satellite (file-dot cliccabile).
 *
 * Mirror Pydantic `api.models.graph_cosmo.SatelliteItem` (BE v1.2.0):
 *   - `id` = `gn.id` graph_node (es. "plan:artifact:abc"); usato per share URL
 *   - `title` = `gn.name` per tooltip
 *   - `latest_at` = ISO timestamp; guida color-tier (fresh < 7d → accent;
 *     warm < 30d → bone-300; cold → bone-500/0.6)
 *   - `importance` = incoming edge degree (cites/mentions/refers_to/
 *     applies_to/similar_to); guida radius-tier log-scale
 *   - `path` = file_path opzionale; se null → no click handler.
 */
export const SatelliteItemZ = z.object({
  id: z.string().min(1),
  title: z.string().min(1),
  latest_at: z.string(),
  importance: z.number().int().nonnegative(),
  path: z.string().nullable().optional(),
});
export type SatelliteItem = z.infer<typeof SatelliteItemZ>;

/**
 * Riassunto kind-aggregato di un satellite di project.
 *
 * Mirror Pydantic `api.models.graph_cosmo.SatelliteSummary` (BE v1.2.0):
 * `count` = numero artifact totali del kind nel project (no top-8 cap interno),
 * `latest_at` = ISO timestamp dell'artifact piu' recente (null se 0 — mai
 * possibile via Q2, reso nullable per defense-in-depth).
 * `items` = top-12 SatelliteItem (ordinati per latest_at DESC). Default `[]`
 * per BE pre-v1.2.0.
 *
 * `kind` usa `KindZ.catch('task')` (H-12 piano): se il backend introduce un
 * kind nuovo, fallback a 'task' invece di crash parse.
 */
export const SatelliteSummaryZ = z.object({
  kind: KindZ.catch("task" as const),
  count: z.number().int().nonnegative(),
  latest_at: z.string().nullable().optional(),
  items: z.array(SatelliteItemZ).optional(),
});
export type SatelliteSummary = z.infer<typeof SatelliteSummaryZ>;

/**
 * Super-nodo progetto: slug stabile + program + degree + satellites top-N.
 *
 * `satellites` e' una lista di `SatelliteSummary` (kind + count + latest_at):
 * il canvas usa `count` per N file-dot semantici e `latest_at` per accent
 * burning fresh-recent (<7 giorni). Pre-v1.1.0 era `list[Kind]` puro.
 *
 * `degree` e' un float continuo (BE v1.3.0): SUM(weight) outgoing aggregate
 * edges (post weight-recency-decay populator). Storicamente int count plain.
 * `projectRadius(degree)` (forceLayoutHelpers) accetta float senza modifiche.
 */
export const ProjectZ = z.object({
  slug: z.string().min(1),
  program: ProgramZ,
  degree: z.number().nonnegative(),
  satellites: z.array(SatelliteSummaryZ),
});
export type Project = z.infer<typeof ProjectZ>;

/**
 * Edge cross-project aggregato.
 * Shape volutamente object (non tuple) per match BE Pydantic `Edge` e ridurre
 * il rischio di bug da rename silenzioso (M-BE-07 + M-FE-09 piano).
 */
export const EdgeZ = z.object({
  source: z.string().min(1),
  target: z.string().min(1),
  relation: EdgeRelationZ,
  // weight e' float continuo post v1.4 populator
  // (decay + type-w + sqrt-normalize). graphCanvas thickness/opacity
  // gia' lavora con float — vedi forceLayout::applyEdgeSpring.
  weight: z.number().nonnegative(),
});
export type Edge = z.infer<typeof EdgeZ>;

/**
 * Bundle cosmo completo — shape atteso da `GET /api/v1/graph/cosmo` (PR #3).
 *
 * Caps difensivi (`.max`) per evitare payload malevoli che bloccherebbero
 * il browser: 500 project e 5000 edge coprono con margine i ~80 project /
 * ~300 edge attesi oggi.
 */
export const GraphCosmoZ = z.object({
  projects: z.array(ProjectZ).max(500),
  edges: z.array(EdgeZ).max(5000),
});
/** @lintignore — consumato da cosmoApi PR #3 (knip skip). */
export type GraphCosmo = z.infer<typeof GraphCosmoZ>;

// -----------------------------------------------------------------------------
// Tipi runtime canvas (posizionamento + fisica)
// -----------------------------------------------------------------------------

/**
 * Progetto posizionato dopo il layout. NON include vx/vy: la fisica e'
 * interna a `forceLayout` e non deve uscire dal modulo (vedi SimNode).
 */
export interface PlacedNode extends Project {
  x: number;
  y: number;
  r: number;
}

// SimNode rimosso 2026-05-17 — il loop fisico vive in `_engine/forceLayout.ts`
// con SimNode<T> interno generico. PlacedNode resta l'unico tipo runtime esposto.

/** Override posizione salvato in LS per un singolo slug (Alt+drag pin). */
export const OverrideZ = z.object({
  x: z.number().finite(),
  y: z.number().finite(),
});
export type Override = z.infer<typeof OverrideZ>;

// -----------------------------------------------------------------------------
// View state persistente (localStorage `marvisx.graph.v1`)
// -----------------------------------------------------------------------------

/**
 * Schema strict per view state LS. Clamping + fallback sono responsabilita'
 * del caller hook (M-FE-10): qui vogliamo un parse che fallisce esplicitamente
 * se i dati sono corrotti, cosi' il console.warn puntuale sul campo rotto.
 *
 * Bounds zoom: [0.05, 100] intenzionalmente largo — il canvas poi clampa a
 * [0.2, 24] tramite sanity-check (piano §animateView).
 */
/** @public — consumato da `useGraphViewState`. */
export const ViewStateZ = z.object({
  zoom: z.number().min(0.05).max(100),
  pan: z.object({
    x: z.number().finite(),
    y: z.number().finite(),
  }),
  nodeOverrides: z.record(z.string(), OverrideZ),
});
/** @public — consumato da `useGraphViewState`. */
export type ViewState = z.infer<typeof ViewStateZ>;

/** Default sicuro: graph centrato, nessun pin utente. @public */
export const DEFAULT_VIEW_STATE: ViewState = {
  zoom: 1,
  pan: { x: 0, y: 0 },
  nodeOverrides: {},
};
