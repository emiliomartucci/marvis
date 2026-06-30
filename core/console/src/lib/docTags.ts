// v1.0.0 - 2026-04-22 - Single source of truth per doc-tag colors + kind extraction.
//
// Consolidates palettes previously duplicated in:
//   - components/graph/universe/UniverseSidebar.tsx (Okabe-Ito hex)
//   - components/shared/DocSection.tsx (brief-style HSL)
//
// Source palette: Okabe-Ito CVD-safe (per .impeccable.md §KG invariant).
// Includes `task` (success green) for PR #13 spiral satellite colors + `docs`
// fallback. `as const satisfies` Record guard catches missing kinds at compile.

/**
 * Canonical doc subfolders — matches docs/<kind>/ filesystem layout.
 * @public — consumed by UniverseSpiralLayer (PR #13) for kind chip filters.
 */
export const DOC_KINDS = [
  "plans",
  "brainstorms",
  "solutions",
  "audits",
  "research",
  "guides",
  "rubrics",
  "analysis",
  "briefs",
  "spikes",
] as const;

/** @public — consumed by UniverseSpiralLayer (PR #13) for type-safe satellite kinds. */
export type DocKind = (typeof DOC_KINDS)[number];

/** Activity kinds extend DocKind with `task` (completed tasks) and `docs` fallback. */
export type ActivityKind = DocKind | "docs" | "task";

/** @public — consumed by UniverseSpiralLayer (PR #13) for Sigma node fill typing. */
export interface TagColor {
  /** Background rgba tint with 0.14 alpha. */
  bg: string;
  /** Foreground hex — also used as Sigma node fill for WebGL rendering. */
  fg: string;
}

/**
 * Okabe-Ito palette (CVD-safe, 8-hue set) + pragmatic extensions.
 * @public — consumed by UniverseSpiralLayer (PR #13) for satellite node fill.
 */
export const DOC_TAG_COLORS = {
  plans:       { bg: "rgba(86, 180, 233, 0.14)",  fg: "#56B4E9" }, // sky blue
  brainstorms: { bg: "rgba(204, 121, 167, 0.14)", fg: "#CC79A7" }, // reddish purple
  solutions:   { bg: "rgba(213, 94, 0, 0.14)",    fg: "#D55E00" }, // vermillion
  audits:      { bg: "rgba(0, 158, 115, 0.14)",   fg: "#009E73" }, // bluish green
  research:    { bg: "rgba(138, 111, 191, 0.14)", fg: "#8A6FBF" }, // violet
  guides:      { bg: "rgba(95, 158, 160, 0.14)",  fg: "#5F9EA0" }, // cadet blue
  rubrics:     { bg: "rgba(176, 196, 222, 0.14)", fg: "#B0C4DE" }, // light steel
  analysis:    { bg: "rgba(100, 149, 237, 0.14)", fg: "#6495ED" }, // cornflower
  briefs:      { bg: "rgba(240, 230, 140, 0.14)", fg: "#DAA520" }, // goldenrod
  spikes:      { bg: "rgba(255, 99, 71, 0.14)",   fg: "#FF6347" }, // tomato
  task:        { bg: "rgba(86, 221, 143, 0.14)",  fg: "#56DD8F" }, // success green
  docs:        { bg: "rgba(230, 159, 0, 0.14)",   fg: "#E69F00" }, // amber fallback
} as const satisfies Record<ActivityKind, TagColor>;

/** Lookup palette for an activity kind. Always safe — unknown keys fall back to `docs`. */
export function docTagColor(kind: string): TagColor {
  if (kind in DOC_TAG_COLORS) return DOC_TAG_COLORS[kind as ActivityKind];
  return DOC_TAG_COLORS.docs;
}

/** Derive an ActivityKind from a filesystem path.
 *
 * Accepts both `docs/<kind>/<name>.md` and `<kind>/<name>.md`. Walks segments
 * and returns the first that matches a known kind. Falls back to `"docs"`.
 *
 * `"docs"` è chiave di DOC_TAG_COLORS (fallback) → deve essere ignorato durante
 * il walk per lasciare che la kind specifica successiva vinca. Altrimenti
 * `docs/brainstorms/foo.md` ritornerebbe `"docs"` invece di `"brainstorms"`.
 *
 * Examples:
 *   - `docs/plans/2026-04-22-foo.md` → `"plans"`
 *   - `research/x.md`                → `"research"`
 *   - `docs/2026-04.md`              → `"docs"` (no matching segment)
 */
export function kindFromFilename(filename: string): ActivityKind {
  const parts = filename.split("/");
  for (const part of parts) {
    const normalised = part.toLowerCase();
    if (normalised === "docs") continue; // fallback slot — preferisci kind specifico
    if (normalised in DOC_TAG_COLORS) return normalised as ActivityKind;
  }
  return "docs";
}
