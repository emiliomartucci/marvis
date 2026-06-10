// v1.1.0 - 2026-04-24 - DirInspector: branch per kind su endpoint giusto
//                       (handoff/task/learning non vivono su /plans) +
//                       normalizzazione a shape unificata per il render.
// v1.0.0 - 2026-04-24 - Side panel destro canvas Cosmo (2 mode: project / dir).
//
// Porta di reference-graph-inspector.jsx (761 LOC) in TSX con i fix M-FE-14:
//  - Nessun alias `useStateI/useMemoI` — hook React diretti.
//  - Tipi espliciti su ogni sub-component.
//  - ZERO mock lorem-ipsum: fetch reali via `lib/api.ts` (pattern UniverseSidebar).
//  - `DOC_TAG_COLORS` da `lib/docTags.ts` (single source of truth).
//  - `navigator.clipboard?.writeText().catch(() => {})` (M-FE-14 no unhandled).
//  - Files cap 50 + "Carica altri" (D-02).
//  - safeHref + encodeURIComponent sugli slug (M-FE-12).
"use client";

import {
  memo,
  useCallback,
  useEffect,
  useMemo,
  useState,
  type CSSProperties,
  type ReactNode,
} from "react";
import {
  fetchAPIValidated,
  getProjectDetail,
  getProjectDocs,
  getProjectGitLog,
  getProjectHandoffs,
  listTasks,
} from "@/lib/api";
import { DOC_TAG_COLORS, docTagColor, kindFromFilename, type ActivityKind } from "@/lib/docTags";
import { COSMO_KIND_TO_DOC_KIND } from "./kindLabels";
import type { Kind } from "./types";
import type {
  DocEntry,
  GitCommit,
  HandoffEntry,
  ProjectDetail,
  TaskResponse,
} from "@/lib/types";
import type { SelectedDir } from "./GraphCanvas";

// -----------------------------------------------------------------------------
// Utilities
// -----------------------------------------------------------------------------

/** safeHref — filtra href per evitare XSS da slug malformati (M-FE-12). */
function safeHref(raw: string): string {
  return /^[/?#]|^https?:/.test(raw) ? raw : "#";
}

function finderUrl(slug: string, relativePath: string): string {
  const cleaned = relativePath.replace(/^\/+/, "");
  const rel = `projects/${encodeURIComponent(slug)}/${cleaned}`;
  const idx = rel.lastIndexOf("/");
  if (idx <= 0) return safeHref(`/finder/?path=${encodeURIComponent(rel)}`);
  const parent = rel.slice(0, idx);
  const name = rel.slice(idx + 1);
  return safeHref(
    `/finder/?path=${encodeURIComponent(parent)}&highlight=${encodeURIComponent(name)}`,
  );
}

function downloadUrl(slug: string, relativePath: string): string {
  const cleaned = relativePath.replace(/^\/+/, "");
  return safeHref(
    `/api/v1/finder/download?path=${encodeURIComponent(
      `projects/${slug}/${cleaned}`,
    )}`,
  );
}

function byDateDesc<T extends { date?: string | null }>(items: T[]): T[] {
  return [...items].sort((a, b) => (b.date ?? "").localeCompare(a.date ?? ""));
}

function copy(text: string): void {
  navigator.clipboard?.writeText(text).catch(() => undefined);
}

interface LearningEntry {
  id: string;
  title: string;
  category?: string | null;
  severity?: string | null;
  created_at?: string | null;
}

async function getProjectLearnings(
  slug: string,
  opts?: { signal?: AbortSignal },
): Promise<LearningEntry[]> {
  return fetchAPIValidated<LearningEntry[]>(
    `/api/v1/learnings?project=${encodeURIComponent(slug)}&limit=50`,
    { parse: (d) => d as LearningEntry[] },
    { signal: opts?.signal },
  );
}

// -----------------------------------------------------------------------------
// Styles (CSSProperties, tokens via var(--pir-*) / hsl(var(--bone-*)))
// -----------------------------------------------------------------------------

const PANEL_W = 380;
const INITIAL_LIMIT = 3;
const FILES_PAGE = 50;

const styles = {
  panel: {
    position: "absolute",
    top: 0,
    right: 0,
    width: PANEL_W,
    height: "100%",
    background: "hsl(var(--pir-surface-0) / 0.96)",
    backdropFilter: "blur(12px)",
    borderLeft: "1px solid var(--pir-border)",
    boxShadow: "-12px 0 32px hsl(0 0% 0% / 0.3)",
    overflowY: "auto",
    overflowX: "hidden",
    fontFamily: "var(--pir-font-sans)",
    color: "var(--pir-text-primary)",
    animation: "cosmo-sidebar-in 220ms cubic-bezier(0.25,1,0.5,1) forwards",
    zIndex: 20,
  },
  header: {
    position: "sticky",
    top: 0,
    background: "hsl(var(--pir-surface-0) / 0.98)",
    borderBottom: "1px solid var(--pir-border)",
    padding: "14px 16px 12px",
    display: "flex",
    alignItems: "flex-start",
    justifyContent: "space-between",
    gap: 12,
    zIndex: 2,
  },
  title: {
    fontSize: 14,
    fontWeight: 600,
    color: "hsl(var(--pir-accent))",
    wordBreak: "break-word",
    lineHeight: 1.25,
    fontFamily: "var(--pir-font-mono)",
  },
  subtitle: {
    fontSize: 10,
    color: "var(--pir-text-tertiary)",
    marginTop: 4,
    letterSpacing: "0.12em",
    textTransform: "uppercase",
    fontFamily: "var(--pir-font-mono)",
  },
  closeBtn: {
    background: "transparent",
    border: "1px solid var(--pir-border)",
    color: "var(--pir-text-tertiary)",
    cursor: "pointer",
    fontSize: 14,
    lineHeight: 1,
    padding: "2px 8px",
    borderRadius: 2,
    fontFamily: "var(--pir-font-mono)",
  },
  section: {
    padding: "12px 16px",
    borderBottom: "1px solid var(--pir-border)",
  },
  sectionTitle: {
    fontSize: 9,
    color: "var(--pir-text-tertiary)",
    letterSpacing: "0.16em",
    textTransform: "uppercase",
    marginBottom: 8,
    display: "flex",
    alignItems: "center",
    gap: 6,
    fontFamily: "var(--pir-font-mono)",
    fontWeight: 600,
  },
  sectionBadge: {
    background: "hsl(var(--pir-surface-2))",
    color: "var(--pir-text-muted)",
    padding: "1px 6px",
    borderRadius: 2,
    fontSize: 9,
    fontFamily: "var(--pir-font-mono)",
  },
  rowWrap: {
    position: "relative",
    padding: "5px 0",
  },
  row: {
    fontSize: 11,
    color: "var(--pir-text-secondary)",
    wordBreak: "break-word",
    lineHeight: 1.4,
    display: "flex",
    flexDirection: "column",
    gap: 2,
    paddingRight: 72,
  },
  rowPrimary: { color: "var(--pir-text-primary)" },
  rowSecondary: {
    color: "var(--pir-text-tertiary)",
    fontSize: 10,
    fontFamily: "var(--pir-font-mono)",
  },
  tag: {
    display: "inline-block",
    padding: "0 5px",
    fontSize: 9,
    letterSpacing: "0.06em",
    textTransform: "uppercase",
    borderRadius: 2,
    marginRight: 6,
    fontFamily: "var(--pir-font-mono)",
    fontWeight: 600,
  },
  hoverActions: {
    position: "absolute",
    top: 6,
    right: 0,
    display: "flex",
    gap: 3,
    opacity: 0,
    pointerEvents: "none",
    transition: "opacity 120ms ease",
  },
  hoverActionsActive: {
    position: "absolute",
    top: 6,
    right: 0,
    display: "flex",
    gap: 3,
    opacity: 1,
    pointerEvents: "auto",
    transition: "opacity 120ms ease",
  },
  iconBtn: {
    background: "hsl(var(--pir-surface-1))",
    border: "1px solid var(--pir-border)",
    color: "var(--pir-text-secondary)",
    cursor: "pointer",
    fontSize: 10,
    width: 22,
    height: 20,
    padding: 0,
    display: "inline-flex",
    alignItems: "center",
    justifyContent: "center",
    borderRadius: 2,
    textDecoration: "none",
    lineHeight: 1,
    fontFamily: "var(--pir-font-mono)",
  },
  iconBtnActive: {
    background: "hsl(var(--pir-accent) / 0.18)",
    border: "1px solid hsl(var(--pir-accent) / 0.5)",
    color: "hsl(var(--pir-accent))",
    cursor: "pointer",
    fontSize: 10,
    width: 22,
    height: 20,
    padding: 0,
    display: "inline-flex",
    alignItems: "center",
    justifyContent: "center",
    borderRadius: 2,
    textDecoration: "none",
    lineHeight: 1,
    fontFamily: "var(--pir-font-mono)",
  },
  moreBtn: {
    marginTop: 6,
    background: "transparent",
    border: "1px solid var(--pir-border)",
    color: "var(--pir-text-tertiary)",
    fontFamily: "var(--pir-font-mono)",
    fontSize: 9,
    padding: "3px 8px",
    borderRadius: 2,
    cursor: "pointer",
    textTransform: "uppercase",
    letterSpacing: "0.08em",
  },
  metaRow: {
    display: "flex",
    gap: 10,
    fontSize: 10,
    color: "var(--pir-text-secondary)",
    padding: "3px 0",
    wordBreak: "break-word",
    fontFamily: "var(--pir-font-mono)",
  },
  metaKey: {
    minWidth: 64,
    color: "var(--pir-text-muted)",
    textTransform: "uppercase",
    letterSpacing: "0.12em",
    fontSize: 9,
    paddingTop: 1,
  },
  contextBase: {
    fontSize: 11,
    color: "var(--pir-text-secondary)",
    lineHeight: 1.55,
    whiteSpace: "pre-wrap",
    wordBreak: "break-word",
  },
  empty: {
    fontSize: 10,
    color: "var(--pir-text-muted)",
    fontStyle: "italic",
    fontFamily: "var(--pir-font-mono)",
  },
  loading: {
    padding: 24,
    textAlign: "center",
    fontSize: 11,
    color: "var(--pir-text-tertiary)",
    textTransform: "uppercase",
    letterSpacing: "0.12em",
    fontFamily: "var(--pir-font-mono)",
  },
  actionsRow: {
    display: "flex",
    gap: 6,
    padding: "12px 16px 18px",
    position: "sticky",
    bottom: 0,
    background: "hsl(var(--pir-surface-0) / 0.98)",
    borderTop: "1px solid var(--pir-border)",
  },
  actionBtn: {
    flex: 1,
    background: "transparent",
    border: "1px solid var(--pir-border)",
    color: "var(--pir-text-secondary)",
    cursor: "pointer",
    fontSize: 10,
    padding: "6px 8px",
    borderRadius: 2,
    fontFamily: "var(--pir-font-mono)",
    textTransform: "uppercase",
    letterSpacing: "0.1em",
  },
  actionBtnActive: {
    flex: 1,
    background: "hsl(var(--pir-accent) / 0.12)",
    border: "1px solid hsl(var(--pir-accent) / 0.5)",
    color: "hsl(var(--pir-accent))",
    cursor: "pointer",
    fontSize: 10,
    padding: "6px 8px",
    borderRadius: 2,
    fontFamily: "var(--pir-font-mono)",
    textTransform: "uppercase",
    letterSpacing: "0.1em",
  },
} as const satisfies Record<string, CSSProperties>;

function sliceItems<T>(items: readonly T[], expanded: boolean): readonly T[] {
  return expanded ? items : items.slice(0, INITIAL_LIMIT);
}

// -----------------------------------------------------------------------------
// HoverRow — azioni inline
// -----------------------------------------------------------------------------

interface HoverRowProps {
  rowKey: string;
  hoveredKey: string | null;
  onHover: (key: string | null) => void;
  copyFlash: boolean;
  onCopy: () => void;
  finderHref?: string | null;
  downloadHref?: string | null;
  children: ReactNode;
}

function HoverRow({
  rowKey,
  hoveredKey,
  onHover,
  copyFlash,
  onCopy,
  finderHref,
  downloadHref,
  children,
}: HoverRowProps) {
  const active = hoveredKey === rowKey;
  return (
    <div
      style={styles.rowWrap}
      onMouseEnter={() => onHover(rowKey)}
      onMouseLeave={() => onHover(null)}
    >
      <div style={styles.row}>{children}</div>
      <div style={active ? styles.hoverActionsActive : styles.hoverActions}>
        <button
          type="button"
          style={copyFlash ? styles.iconBtnActive : styles.iconBtn}
          onClick={onCopy}
          title="Copy path / id"
        >
          {copyFlash ? "✓" : "⧉"}
        </button>
        {finderHref && (
          <a style={styles.iconBtn} href={finderHref} title="Open in finder">
            ↗
          </a>
        )}
        {downloadHref && (
          <a style={styles.iconBtn} href={downloadHref} title="Download">
            ⬇
          </a>
        )}
      </div>
    </div>
  );
}

// -----------------------------------------------------------------------------
// ListSection — wrapper con expand/collapse
// -----------------------------------------------------------------------------

interface ListSectionProps {
  sectionKey: string;
  label: string;
  total: number;
  expanded: boolean;
  onToggle: (key: string) => void;
  children: ReactNode;
}

function ListSection({
  sectionKey,
  label,
  total,
  expanded,
  onToggle,
  children,
}: ListSectionProps) {
  return (
    <section style={styles.section}>
      <div style={styles.sectionTitle}>
        {label}
        <span style={styles.sectionBadge}>{total}</span>
      </div>
      {total === 0 ? <div style={styles.empty}>none</div> : children}
      {total > INITIAL_LIMIT && (
        <button
          type="button"
          style={styles.moreBtn}
          onClick={() => onToggle(sectionKey)}
        >
          {expanded ? "show less" : `show ${total - INITIAL_LIMIT} more`}
        </button>
      )}
    </section>
  );
}

// -----------------------------------------------------------------------------
// InfoRow — riga label/value
// -----------------------------------------------------------------------------

interface InfoRowProps {
  label: string;
  value: ReactNode;
}

function InfoRow({ label, value }: InfoRowProps) {
  return (
    <div style={styles.metaRow}>
      <span style={styles.metaKey}>{label}</span>
      <span style={{ color: "var(--pir-text-primary)" }}>{value}</span>
    </div>
  );
}

// -----------------------------------------------------------------------------
// useProjectDetail — hook fetch progetto + artefatti
// -----------------------------------------------------------------------------

interface ProjectData {
  detail: ProjectDetail | null;
  handoffs: HandoffEntry[];
  tasks: TaskResponse[];
  commits: GitCommit[];
  learnings: LearningEntry[];
  docs: DocEntry[];
}

interface ProjectDataState {
  data: ProjectData | null;
  loading: boolean;
  error: string | null;
}

function useProjectDetail(slug: string): ProjectDataState {
  const [state, setState] = useState<ProjectDataState>({
    data: null,
    loading: true,
    error: null,
  });

  useEffect(() => {
    const ctrl = new AbortController();
    setState({ data: null, loading: true, error: null });

    Promise.allSettled([
      getProjectDetail(slug, { signal: ctrl.signal }),
      getProjectHandoffs(slug, { signal: ctrl.signal }),
      listTasks({ project: slug, status: "completed", limit: 100 }, { signal: ctrl.signal }),
      getProjectGitLog(slug, 50, { signal: ctrl.signal }),
      getProjectLearnings(slug, { signal: ctrl.signal }),
      getProjectDocs(slug, { signal: ctrl.signal }),
    ]).then((results) => {
      if (ctrl.signal.aborted) return;
      const [detailRes, handoffsRes, tasksRes, commitsRes, learningsRes, docsRes] =
        results;
      const docs = docsRes.status === "fulfilled" ? byDateDesc(docsRes.value) : [];
      const handoffs =
        handoffsRes.status === "fulfilled"
          ? [...handoffsRes.value].sort((a, b) =>
              (b.date ?? "").localeCompare(a.date ?? ""),
            )
          : [];
      const error =
        detailRes.status === "rejected" && !String(detailRes.reason).includes("Abort")
          ? String((detailRes.reason as Error)?.message ?? "fetch failed")
          : null;
      setState({
        data: {
          detail: detailRes.status === "fulfilled" ? detailRes.value : null,
          handoffs,
          tasks: tasksRes.status === "fulfilled" ? tasksRes.value : [],
          commits: commitsRes.status === "fulfilled" ? commitsRes.value : [],
          learnings: learningsRes.status === "fulfilled" ? learningsRes.value : [],
          docs,
        },
        loading: false,
        error,
      });
    });

    return () => ctrl.abort();
  }, [slug]);

  return state;
}

// -----------------------------------------------------------------------------
// ProjectInspector
// -----------------------------------------------------------------------------

interface ProjectInspectorProps {
  slug: string;
  onClose: () => void;
}

function ProjectInspector({ slug, onClose }: ProjectInspectorProps) {
  const { data, loading, error } = useProjectDetail(slug);
  const [hoveredKey, setHoveredKey] = useState<string | null>(null);
  const [copyRow, setCopyRow] = useState<string | null>(null);
  const [copyFlash, setCopyFlash] = useState<"id" | "url" | null>(null);
  const [expanded, setExpanded] = useState<Set<string>>(() => new Set());
  const [ctxExp, setCtxExp] = useState(false);

  const toggleExpand = useCallback((key: string) => {
    setExpanded((prev) => {
      const next = new Set(prev);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });
  }, []);

  const copyPathRow = useCallback((key: string, path: string) => {
    copy(path);
    setCopyRow(key);
    window.setTimeout(() => setCopyRow(null), 800);
  }, []);

  const copyFlashTimed = useCallback((kind: "id" | "url", text: string) => {
    copy(text);
    setCopyFlash(kind);
    window.setTimeout(() => setCopyFlash(null), 800);
  }, []);

  const ctx = data?.detail?.context_md ?? "";
  const ctxShow = useMemo(() => {
    if (!ctx) return "";
    if (ctxExp) return ctx;
    return ctx.length > 320 ? ctx.slice(0, 320) + "…" : ctx;
  }, [ctx, ctxExp]);

  if (loading) {
    return (
      <aside style={styles.panel}>
        <header style={styles.header}>
          <div>
            <div style={styles.title}>project.{slug}</div>
            <div style={styles.subtitle}>loading…</div>
          </div>
          <button type="button" style={styles.closeBtn} onClick={onClose} title="Close">
            ×
          </button>
        </header>
        <div style={styles.loading}>fetching artifacts…</div>
      </aside>
    );
  }

  return (
    <aside style={styles.panel}>
      <header style={styles.header}>
        <div>
          <div style={styles.title}>project.{slug}</div>
          <div style={styles.subtitle}>
            project · {data?.detail?.lifecycle ?? "—"}
          </div>
        </div>
        <button type="button" style={styles.closeBtn} onClick={onClose} title="Close (Esc)">
          ×
        </button>
      </header>

      {error && (
        <section style={styles.section}>
          <div style={{ ...styles.empty, color: "hsl(var(--pir-error))" }}>
            {error}
          </div>
        </section>
      )}

      <section style={styles.section}>
        <div style={styles.sectionTitle}>info</div>
        <InfoRow label="slug" value={slug} />
        {data?.detail?.metadata_path && (
          <InfoRow label="path" value={data.detail.metadata_path} />
        )}
        {data?.detail?.repo_path && <InfoRow label="repo" value={data.detail.repo_path} />}
        {data?.detail?.language && <InfoRow label="lang" value={data.detail.language} />}
        {data?.detail?.program && <InfoRow label="program" value={data.detail.program} />}
        {data?.detail?.phase && <InfoRow label="phase" value={data.detail.phase} />}
      </section>

      {ctx && (
        <section style={styles.section}>
          <div style={styles.sectionTitle}>
            context.md
            <span style={styles.sectionBadge}>
              {Math.round(ctx.length / 100) / 10}k
            </span>
          </div>
          <div style={styles.contextBase}>{ctxShow}</div>
          <div style={{ display: "flex", gap: 6, marginTop: 6 }}>
            <button
              type="button"
              style={styles.moreBtn}
              onClick={() => setCtxExp((v) => !v)}
            >
              {ctxExp ? "collapse" : "expand"}
            </button>
          </div>
        </section>
      )}

      <ListSection
        sectionKey="docs"
        label="docs"
        total={data?.docs.length ?? 0}
        expanded={expanded.has("docs")}
        onToggle={toggleExpand}
      >
        {sliceItems(data?.docs ?? [], expanded.has("docs")).map((d) => {
          const key = `doc:${d.filename}`;
          // `d.category` dal BE puo' contenere valori frontmatter non-enum
          // (es. `feat|fix|chore` dal convention MarvisX). Stesso fix di
          // `docActivityKind` per evitare chip colorato sbagliato.
          const cat = docActivityKind(d);
          const tag = docTagColor(cat);
          return (
            <HoverRow
              key={key}
              rowKey={key}
              hoveredKey={hoveredKey}
              onHover={setHoveredKey}
              copyFlash={copyRow === key}
              onCopy={() => copyPathRow(key, d.filename)}
              finderHref={finderUrl(slug, d.filename)}
              downloadHref={downloadUrl(slug, d.filename)}
            >
              <span style={styles.rowPrimary}>
                <span style={{ ...styles.tag, background: tag.bg, color: tag.fg }}>
                  {cat}
                </span>
                {d.title ?? d.filename.split("/").pop()}
              </span>
              {d.date && <span style={styles.rowSecondary}>{d.date}</span>}
            </HoverRow>
          );
        })}
      </ListSection>

      <ListSection
        sectionKey="handoffs"
        label="handoffs"
        total={data?.handoffs.length ?? 0}
        expanded={expanded.has("handoffs")}
        onToggle={toggleExpand}
      >
        {sliceItems(data?.handoffs ?? [], expanded.has("handoffs")).map((h) => {
          const key = `h:${h.filename}`;
          const name = h.filename
            .replace(/^memory\/handoffs?\//, "")
            .replace(/\.md$/, "");
          return (
            <HoverRow
              key={key}
              rowKey={key}
              hoveredKey={hoveredKey}
              onHover={setHoveredKey}
              copyFlash={copyRow === key}
              onCopy={() => copyPathRow(key, h.filename)}
              finderHref={finderUrl(slug, h.filename)}
              downloadHref={downloadUrl(slug, h.filename)}
            >
              <span style={styles.rowPrimary}>{name}</span>
              <span style={styles.rowSecondary}>
                {h.date ?? "—"} {h.session ? `· ${h.session}` : ""}
              </span>
            </HoverRow>
          );
        })}
      </ListSection>

      <ListSection
        sectionKey="tasks"
        label="tasks"
        total={data?.tasks.length ?? 0}
        expanded={expanded.has("tasks")}
        onToggle={toggleExpand}
      >
        {sliceItems(data?.tasks ?? [], expanded.has("tasks")).map((t) => {
          const key = `t:${t.id}`;
          return (
            <HoverRow
              key={key}
              rowKey={key}
              hoveredKey={hoveredKey}
              onHover={setHoveredKey}
              copyFlash={copyRow === key}
              onCopy={() => copyPathRow(key, t.id)}
            >
              <span style={styles.rowPrimary}>{t.title}</span>
              <span style={styles.rowSecondary}>
                {t.status} · {t.priority}
              </span>
            </HoverRow>
          );
        })}
      </ListSection>

      <ListSection
        sectionKey="commits"
        label="commits"
        total={data?.commits.length ?? 0}
        expanded={expanded.has("commits")}
        onToggle={toggleExpand}
      >
        {sliceItems(data?.commits ?? [], expanded.has("commits")).map((c) => {
          const key = `c:${c.hash}`;
          return (
            <HoverRow
              key={key}
              rowKey={key}
              hoveredKey={hoveredKey}
              onHover={setHoveredKey}
              copyFlash={copyRow === key}
              onCopy={() => copyPathRow(key, c.hash)}
            >
              <span style={styles.rowPrimary}>{c.message}</span>
              <span style={styles.rowSecondary}>
                {c.hash_short} · {c.author} · {c.date}
              </span>
            </HoverRow>
          );
        })}
      </ListSection>

      {data && data.learnings.length > 0 && (
        <ListSection
          sectionKey="learnings"
          label="learnings"
          total={data.learnings.length}
          expanded={expanded.has("learnings")}
          onToggle={toggleExpand}
        >
          {sliceItems(data.learnings, expanded.has("learnings")).map((l) => {
            const key = `l:${l.id}`;
            return (
              <HoverRow
                key={key}
                rowKey={key}
                hoveredKey={hoveredKey}
                onHover={setHoveredKey}
                copyFlash={copyRow === key}
                onCopy={() => copyPathRow(key, l.id)}
              >
                <span style={styles.rowPrimary}>{l.title}</span>
                <span style={styles.rowSecondary}>
                  {l.category ?? "—"} · {l.severity ?? "—"} · {l.created_at ?? ""}
                </span>
              </HoverRow>
            );
          })}
        </ListSection>
      )}

      <div style={styles.actionsRow}>
        <button
          type="button"
          style={copyFlash === "url" ? styles.actionBtnActive : styles.actionBtn}
          onClick={() => {
            const origin = typeof window === "undefined" ? "" : window.location.origin;
            copyFlashTimed("url", `${origin}/graph/?id=project:artifact:${encodeURIComponent(slug)}`);
          }}
          title="Copy share URL"
        >
          {copyFlash === "url" ? "✓ copied" : "share"}
        </button>
        <button
          type="button"
          style={copyFlash === "id" ? styles.actionBtnActive : styles.actionBtn}
          onClick={() => copyFlashTimed("id", `project:artifact:${slug}`)}
          title="Copy node id"
        >
          {copyFlash === "id" ? "✓ copied" : "copy id"}
        </button>
      </div>
    </aside>
  );
}

// -----------------------------------------------------------------------------
// DirInspector — fetch artefatti per kind + filtra
// -----------------------------------------------------------------------------

interface DirInspectorProps {
  slug: string;
  kind: Kind;
  dirName: string;
  onClose: () => void;
}

/** Derive una ActivityKind da un DocEntry (via `category` esplicito o `filename`).
 *
 * Il BE `/plans` endpoint popola `category` dal frontmatter `category || type`.
 * I doc MarvisX usano convention `type: feat|fix|refactor|chore|docs` → valori
 * che NON sono chiavi di `DOC_TAG_COLORS` (plans/brainstorms/...). Validiamo
 * prima di accettare: altrimenti `docMatchesKind` fallisce per doc indicizzati
 * sotto `docs/brainstorms/...` ma con `category: "feat"` → "no match" visivo.
 */
function docActivityKind(d: DocEntry): ActivityKind {
  const cat = d.category;
  if (cat && cat in DOC_TAG_COLORS) return cat as ActivityKind;
  return kindFromFilename(d.filename);
}

/** Match fra kind satellite (Cosmo) e kind di un doc. Usato solo per kind
 * che vivono effettivamente in docs/* (plan/brainstorm/solution/audit/research).
 * Handoff/task/learning hanno endpoint dedicati. */
function docMatchesKind(d: DocEntry, kind: Kind): boolean {
  const expected = COSMO_KIND_TO_DOC_KIND[kind];
  return docActivityKind(d) === expected;
}

/** Shape uniforme per il render list del DirInspector.
 *
 * Deriviamo da 4 fonti diverse (docs, handoffs, tasks, learnings) — ciascuna
 * con payload suo — verso una riga leggibile. `id` univoco, `title` primary,
 * `subtitle` secondary, `path` opzionale per finder/download/copy, `tagLabel`
 * per il chip colorato. */
interface DirItem {
  id: string;
  title: string;
  subtitle: string | null;
  /** Relative path sotto project root; se null il finder/download non appaiono. */
  path: string | null;
  /** Label del chip colorato (cat). Se null, nessun chip. */
  tagLabel: ActivityKind | null;
  /** Chiave per ordinamento discendente (ISO date o timestamp fallback). */
  sortKey: string;
  /** Valore copiato dal button "copy" (filename o id). */
  copyValue: string;
}

function docToItem(d: DocEntry): DirItem {
  const cat = docActivityKind(d);
  return {
    id: `doc:${d.filename}`,
    title: d.title ?? d.filename.split("/").pop() ?? d.filename,
    subtitle: d.date ?? null,
    path: d.filename,
    tagLabel: cat,
    sortKey: d.date ?? "",
    copyValue: d.filename,
  };
}

function handoffToItem(h: HandoffEntry): DirItem {
  const name = h.filename
    .replace(/^memory\/handoffs?\//, "")
    .replace(/\.md$/, "");
  const sessionTag = h.session ? ` · ${h.session}` : "";
  return {
    id: `h:${h.filename}`,
    title: name,
    subtitle: `${h.date ?? "—"}${sessionTag}`,
    path: h.filename,
    tagLabel: "docs",
    sortKey: h.date ?? "",
    copyValue: h.filename,
  };
}

function taskToItem(t: TaskResponse): DirItem {
  return {
    id: `t:${t.id}`,
    title: t.title,
    subtitle: `${t.status} · ${t.priority}`,
    path: null,
    tagLabel: "task",
    sortKey: t.updated_at ?? t.created_at ?? "",
    copyValue: t.id,
  };
}

function learningToItem(l: LearningEntry): DirItem {
  const parts = [l.category ?? null, l.severity ?? null, l.created_at ?? null]
    .filter((x): x is string => Boolean(x))
    .join(" · ");
  return {
    id: `l:${l.id}`,
    title: l.title,
    subtitle: parts || null,
    path: null,
    tagLabel: "docs",
    sortKey: l.created_at ?? "",
    copyValue: l.id,
  };
}

/** Fetch artefatti del kind selezionato + normalizza a DirItem[].
 *
 * Per `plan/brainstorm/solution/audit/research`: endpoint `/plans` (tutti in
 * docs/*), filtrato client-side via `docMatchesKind`.
 * Per `handoff`: endpoint `/handoffs`.
 * Per `task`: endpoint `/tasks?project=...`.
 * Per `learning`: endpoint `/learnings?project=...`.
 */
async function fetchDirItems(
  slug: string,
  kind: Kind,
  signal: AbortSignal,
): Promise<DirItem[]> {
  if (kind === "handoff") {
    const handoffs = await getProjectHandoffs(slug, { signal });
    return handoffs.map(handoffToItem);
  }
  if (kind === "task") {
    // Include tutti gli stati — utente in Cosmo vuole vedere i task del project,
    // non solo pending. Senza filtro status ritorna full list, capped 200 lato API.
    const tasks = await listTasks({ project: slug, limit: 200 }, { signal });
    return tasks.map(taskToItem);
  }
  if (kind === "learning") {
    const learnings = await getProjectLearnings(slug, { signal });
    return learnings.map(learningToItem);
  }
  // Kind che vivono in docs/*
  const docs = await getProjectDocs(slug, { signal });
  return docs.filter((d) => docMatchesKind(d, kind)).map(docToItem);
}

function DirInspector({ slug, kind, dirName, onClose }: DirInspectorProps) {
  const [items, setItems] = useState<DirItem[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [query, setQuery] = useState("");
  const [limit, setLimit] = useState(FILES_PAGE);
  const [hoveredKey, setHoveredKey] = useState<string | null>(null);
  const [copyRow, setCopyRow] = useState<string | null>(null);

  useEffect(() => {
    const ctrl = new AbortController();
    setItems(null);
    setError(null);
    setLimit(FILES_PAGE);
    fetchDirItems(slug, kind, ctrl.signal)
      .then((list) =>
        setItems([...list].sort((a, b) => b.sortKey.localeCompare(a.sortKey))),
      )
      .catch((e: unknown) => {
        if (ctrl.signal.aborted) return;
        setError(String((e as Error)?.message ?? e));
      });
    return () => ctrl.abort();
  }, [slug, kind]);

  const filtered = useMemo(() => {
    if (!items) return [];
    const q = query.trim().toLowerCase();
    if (!q) return items;
    return items.filter(
      (it) =>
        it.title.toLowerCase().includes(q) ||
        (it.path ?? "").toLowerCase().includes(q) ||
        (it.subtitle ?? "").toLowerCase().includes(q),
    );
  }, [items, query]);

  const visible = filtered.slice(0, limit);
  const hasMore = filtered.length > visible.length;

  const copyPathRow = useCallback((key: string, path: string) => {
    copy(path);
    setCopyRow(key);
    window.setTimeout(() => setCopyRow(null), 800);
  }, []);

  // Placeholder search tarato sul kind: per task/learning "filename" non ha senso.
  const SEARCH_PLACEHOLDERS: Partial<Record<Kind, string>> = {
    task: "search task title/status…",
    learning: "search learning…",
  };
  const searchPlaceholder = SEARCH_PLACEHOLDERS[kind] ?? "search filename…";
  const sectionLabel = kind === "task" || kind === "learning" ? kind : "files";

  return (
    <aside style={styles.panel}>
      <header style={styles.header}>
        <div>
          <div style={{ ...styles.title, fontSize: 13 }}>{dirName}</div>
          <div style={styles.subtitle}>
            <span style={{ color: "var(--pir-text-muted)" }}>{slug}</span> ·{" "}
            {kind}
          </div>
        </div>
        <button type="button" style={styles.closeBtn} onClick={onClose} title="Close (Esc)">
          ×
        </button>
      </header>

      <section style={styles.section}>
        <div style={styles.sectionTitle}>filter</div>
        <input
          type="text"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          placeholder={searchPlaceholder}
          style={{
            width: "100%",
            background: "hsl(var(--pir-surface-1))",
            border: "1px solid var(--pir-border)",
            color: "var(--pir-text-primary)",
            fontSize: 11,
            fontFamily: "var(--pir-font-mono)",
            padding: "6px 8px",
            borderRadius: 2,
            outline: "none",
            boxSizing: "border-box",
          }}
        />
      </section>

      <section style={styles.section}>
        <div style={styles.sectionTitle}>
          {sectionLabel}
          <span style={styles.sectionBadge}>
            {visible.length} / {filtered.length}
          </span>
        </div>
        {error && (
          <div style={{ ...styles.empty, color: "hsl(var(--pir-error))" }}>
            {error}
          </div>
        )}
        {!error && items === null && <div style={styles.empty}>loading…</div>}
        {items !== null && filtered.length === 0 && (
          <div style={styles.empty}>no match</div>
        )}
        {visible.map((it) => {
          const tag = it.tagLabel ? docTagColor(it.tagLabel) : null;
          return (
            <HoverRow
              key={it.id}
              rowKey={it.id}
              hoveredKey={hoveredKey}
              onHover={setHoveredKey}
              copyFlash={copyRow === it.id}
              onCopy={() => copyPathRow(it.id, it.copyValue)}
              finderHref={it.path ? finderUrl(slug, it.path) : null}
              downloadHref={it.path ? downloadUrl(slug, it.path) : null}
            >
              <span style={styles.rowPrimary}>
                {tag && it.tagLabel && (
                  <span style={{ ...styles.tag, background: tag.bg, color: tag.fg }}>
                    {it.tagLabel}
                  </span>
                )}
                {it.title}
              </span>
              {it.subtitle && <span style={styles.rowSecondary}>{it.subtitle}</span>}
            </HoverRow>
          );
        })}
        {hasMore && (
          <button
            type="button"
            style={styles.moreBtn}
            onClick={() => setLimit((v) => v + FILES_PAGE)}
          >
            Carica altri {Math.min(FILES_PAGE, filtered.length - visible.length)}
          </button>
        )}
      </section>
    </aside>
  );
}

// -----------------------------------------------------------------------------
// Public entry
// -----------------------------------------------------------------------------

/** @lintignore — interfaccia props consumata solo internamente da GraphPage. */
export interface GraphInspectorProps {
  selected: string | null;
  selectedDir: SelectedDir | null;
  onClose: () => void;
}

function GraphInspectorImpl({ selected, selectedDir, onClose }: GraphInspectorProps) {
  if (selectedDir) {
    return (
      <DirInspector
        slug={selectedDir.projectSlug}
        kind={selectedDir.kind}
        dirName={selectedDir.name}
        onClose={onClose}
      />
    );
  }
  if (selected) {
    return <ProjectInspector slug={selected} onClose={onClose} />;
  }
  return null;
}

export const GraphInspector = memo(GraphInspectorImpl);
