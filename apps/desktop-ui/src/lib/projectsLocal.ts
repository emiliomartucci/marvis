import type {
  DocEntry,
  ManualProjectEdgeKind,
  ProgramInfo,
  ProjectDetail,
  ProjectInfo,
  ProjectKgNeighbor,
  TaskResponse,
} from "./types";

export const PROJECT_COLOR_CHANGED_EVENT = "marvis:project-color-changed";

export interface ProjectColorChangedDetail {
  slug: string;
  color: string | null;
}

export interface ProjectPulse {
  completed: number;
  total: number;
  open: number;
  review: number;
  percent: number;
  fraction: string;
}

export interface AdrDisplay {
  filename: string;
  title: string;
  date: string | null;
  status: "current" | "superseded";
  supersededBy: string | null;
  excerpt: string;
}

export interface ProjectRelation {
  slug: string;
  kind: ManualProjectEdgeKind;
}

type Frontmatter = Record<string, string>;

export function dispatchProjectColorChanged(detail: ProjectColorChangedDetail): void {
  window.dispatchEvent(new CustomEvent<ProjectColorChangedDetail>(PROJECT_COLOR_CHANGED_EVENT, {
    detail,
  }));
}

export function projectDisplayName(project: Pick<ProjectInfo, "name" | "slug">): string {
  return project.name || project.slug;
}

export function allProjects(programs: ProgramInfo[]): ProjectInfo[] {
  return programs.flatMap((program) => program.projects);
}

export function findProject(programs: ProgramInfo[], slug: string | null): ProjectInfo | null {
  if (!slug) return null;
  return allProjects(programs).find((project) => project.slug === slug) ?? null;
}

export function patchProgramProjectColor(
  programs: ProgramInfo[],
  slug: string,
  color: string | null,
): ProgramInfo[] {
  return programs.map((program) => ({
    ...program,
    projects: program.projects.map((project) =>
      project.slug === slug ? { ...project, color } : project
    ),
  }));
}

export function computeProjectPulse(tasks: TaskResponse[]): ProjectPulse {
  const counted = tasks.filter((task) => task.status !== "rejected");
  const completed = counted.filter((task) => task.status === "completed").length;
  const total = counted.length;
  const open = counted.filter((task) => task.status !== "completed").length;
  const review = counted.filter((task) => task.status === "review").length;
  const percent = total > 0 ? Math.round((completed / total) * 100) : 0;
  return {
    completed,
    total,
    open,
    review,
    percent,
    fraction: `${completed}/${total}`,
  };
}

function stripQuotes(value: string): string {
  return value.replace(/^['"]|['"]$/g, "").trim();
}

function parseFrontmatter(markdown: string): { data: Frontmatter; body: string } {
  if (!markdown.startsWith("---")) return { data: {}, body: markdown };
  const end = markdown.indexOf("\n---", 3);
  if (end < 0) return { data: {}, body: markdown };
  const block = markdown.slice(3, end).trim();
  const data: Frontmatter = {};
  for (const line of block.split(/\r?\n/)) {
    const match = line.match(/^([A-Za-z0-9_-]+):\s*(.*)$/);
    if (!match) continue;
    data[match[1]] = stripQuotes(match[2]);
  }
  return { data, body: markdown.slice(end + 4).trim() };
}

function titleFromFilename(filename: string): string {
  const leaf = filename.split("/").pop() ?? filename;
  return leaf.replace(/\.md$/i, "").replace(/^\d{4}-\d{2}-\d{2}-?/, "").replace(/[-_]+/g, " ");
}

export function parseAdrDisplay(doc: DocEntry, markdown: string): AdrDisplay {
  const { data, body } = parseFrontmatter(markdown);
  const supersededBy = data.superseded_by || data.supersededBy || null;
  const rawStatus = (data.status ?? "").toLowerCase();
  const status = supersededBy || rawStatus === "superseded" || rawStatus === "superata"
    ? "superseded"
    : "current";
  const title = data.title || doc.title || titleFromFilename(doc.filename);
  const excerpt = body
    .split(/\r?\n/)
    .map((line) => line.replace(/^#+\s*/, "").trim())
    .filter(Boolean)
    .slice(0, 2)
    .join(" ");

  return {
    filename: doc.filename,
    title,
    date: data.date || doc.date || null,
    status,
    supersededBy,
    excerpt,
  };
}

export function isDecisionDoc(doc: DocEntry): boolean {
  return doc.filename.startsWith("docs/decisions/") && doc.filename.endsWith(".md");
}

export function isMarkdownDoc(filename: string): boolean {
  return filename.toLowerCase().endsWith(".md");
}

export function docKind(doc: DocEntry): string {
  if (doc.category) return doc.category;
  const leaf = doc.filename.split("/").pop() ?? doc.filename;
  const ext = leaf.includes(".") ? leaf.split(".").pop() : null;
  return ext ? ext.toLowerCase() : "doc";
}

export function sortDocs(docs: DocEntry[]): DocEntry[] {
  return [...docs].sort((left, right) => {
    if (!left.date && !right.date) return left.filename.localeCompare(right.filename);
    if (!left.date) return 1;
    if (!right.date) return -1;
    return right.date.localeCompare(left.date);
  });
}

function slugFromProjectNode(neighbor: ProjectKgNeighbor): string | null {
  if (neighbor.project_id) return neighbor.project_id;
  if (neighbor.id.startsWith("project:artifact:")) {
    return neighbor.id.slice("project:artifact:".length);
  }
  return null;
}

export function relationsFromProjectDetail(detail: ProjectDetail): ProjectRelation[] {
  const neighbors = detail.kg_context?.neighbors ?? [];
  const relations: ProjectRelation[] = [];
  for (const neighbor of neighbors) {
    const relation = neighbor.edge?.relation ?? neighbor.relation;
    if (relation !== "related" && relation !== "depends_on") continue;
    const slug = slugFromProjectNode(neighbor);
    if (!slug || slug === detail.slug) continue;
    relations.push({ slug, kind: relation });
  }
  return relations;
}

export function hslStringToHex(input: string): string | null {
  const normalized = input.trim().replace(/,/g, " ");
  const match = normalized.match(/^(-?\d+(?:\.\d+)?)\s+(\d+(?:\.\d+)?)%\s+(\d+(?:\.\d+)?)%/);
  if (!match) return null;
  const h = (((Number(match[1]) % 360) + 360) % 360) / 360;
  const s = Number(match[2]) / 100;
  const l = Number(match[3]) / 100;
  const hueToRgb = (p: number, q: number, tValue: number) => {
    let t = tValue;
    if (t < 0) t += 1;
    if (t > 1) t -= 1;
    if (t < 1 / 6) return p + (q - p) * 6 * t;
    if (t < 1 / 2) return q;
    if (t < 2 / 3) return p + (q - p) * (2 / 3 - t) * 6;
    return p;
  };
  const q = l < 0.5 ? l * (1 + s) : l + s - l * s;
  const p = 2 * l - q;
  const channels = [
    hueToRgb(p, q, h + 1 / 3),
    hueToRgb(p, q, h),
    hueToRgb(p, q, h - 1 / 3),
  ].map((value) => Math.round(value * 255).toString(16).padStart(2, "0"));
  return `#${channels.join("")}`;
}
