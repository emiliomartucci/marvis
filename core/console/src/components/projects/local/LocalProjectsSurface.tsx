"use client";

import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import {
  Suspense,
  useEffect,
  useMemo,
  useState,
  type CSSProperties,
  type ReactNode,
} from "react";

import {
  createTodoLocal,
  deleteManualProjectEdge,
  getProjectDocs,
  getProjectFile,
  getProjectGitBranches,
  getProjectGitLog,
  getProjectDetail,
  getPrograms,
  listBrainJournal,
  listLearnings,
  listTasks,
  updateProjectColor,
  upsertManualProjectEdge,
  type BrainJournalEntryResponse,
} from "@/lib/api";
import {
  allProjects,
  computeProjectPulse,
  dispatchProjectColorChanged,
  docKind,
  findProject,
  hslStringToHex,
  isDecisionDoc,
  isMarkdownDoc,
  parseAdrDisplay,
  patchProgramProjectColor,
  projectDisplayName,
  relationsFromProjectDetail,
  sortDocs,
  type AdrDisplay,
  type ProjectRelation,
} from "@/lib/projectsLocal";
import { normalizeJournalEntry, type DiaryItem, type NormalizedDiaryDay } from "@/lib/diario";
import { useT } from "@/lib/i18n";
import type {
  DocEntry,
  GitBranch,
  GitCommit,
  LearningResponse,
  ManualProjectEdgeKind,
  ProgramInfo,
  ProjectDetail,
  ProjectInfo,
  TaskResponse,
} from "@/lib/types";
import { Drawer } from "@/components/ui/Drawer";
import SafeMarkdown from "@/components/projects/SafeMarkdown";
import { ErrorAlert } from "@/components/ui/ErrorAlert";

const COLOR_TOKENS = [
  "--pir-accent",
  "--pir-success",
  "--pir-warning",
  "--pir-error",
  "--pir-info",
] as const;

type ToastSetter = (message: string) => void;

function isLocalMode(): boolean {
  const value = process.env.NEXT_PUBLIC_LOCAL_MODE;
  return value === "1" || value === "true";
}

function replaceCount(template: string, count: number): string {
  return template.replace("{count}", String(count));
}

function statusLabel(project: ProjectDetail): string {
  const raw = project.lifecycle ?? project.phase ?? "active";
  return raw.replace(/[_-]+/g, " ").replace(/\b\w/g, (char) => char.toUpperCase());
}

function projectColorStyle(color: string | null | undefined): CSSProperties | undefined {
  return color ? { backgroundColor: color } : undefined;
}

function ProjectDot({ color, size = "md" }: { color: string | null | undefined; size?: "sm" | "md" | "lg" }) {
  const sizeClass = size === "lg" ? "h-3 w-3" : size === "sm" ? "h-1.5 w-1.5" : "h-2 w-2";
  return (
    <span
      aria-hidden
      className={`${sizeClass} shrink-0 rounded-full ${color ? "" : "bg-pir-border-strong"}`}
      style={projectColorStyle(color)}
    />
  );
}

function Section({
  title,
  caption,
  count,
  children,
}: {
  title: string;
  caption?: string;
  count?: number;
  children: ReactNode;
}) {
  return (
    <section className="min-w-0">
      <div className="mb-3 flex min-w-0 items-start justify-between gap-3">
        <div className="min-w-0">
          <h2 className="font-mono text-caption uppercase tracking-[0.08em] text-pir-text-tertiary">
            {title}
          </h2>
          {caption && (
            <p className="mt-1 truncate text-caption text-pir-text-muted">{caption}</p>
          )}
        </div>
        {count !== undefined && (
          <span className="rounded border border-pir bg-pir-surface-1 px-2 py-0.5 font-mono text-caption text-pir-text-muted">
            {count}
          </span>
        )}
      </div>
      {children}
    </section>
  );
}

function projectPulseFromCounts(project: ProjectInfo) {
  const counts = project.task_counts;
  const total = counts.pending + counts.approved + counts.in_progress + counts.review + counts.completed + counts.failed;
  const completed = counts.completed;
  const open = counts.pending + counts.approved + counts.in_progress + counts.review + counts.failed;
  const percent = total > 0 ? Math.round((completed / total) * 100) : 0;
  return { total, completed, open, percent };
}

function ProjectsOverview({ programs }: { programs: ProgramInfo[] }) {
  const { t } = useT();
  const strings = t.projects.overview;
  const visiblePrograms = programs
    .map((program) => ({ ...program, projects: program.projects.filter((project) => project.on_server) }))
    .filter((program) => program.projects.length > 0);
  const totalProjects = visiblePrograms.reduce((sum, program) => sum + program.projects.length, 0);

  return (
    <div className="min-h-0 flex-1 overflow-y-auto px-6 py-6 md:px-8">
      <div className="mx-auto flex max-w-6xl flex-col gap-6">
        <header className="flex items-end justify-between gap-4 border-b border-pir pb-4">
          <div>
            <h1 className="text-heading text-pir-text-primary">{strings.title}</h1>
            <p className="mt-1 text-body text-pir-text-tertiary">{strings.subtitle}</p>
          </div>
          <span className="font-mono text-caption text-pir-text-muted">
            {totalProjects}
          </span>
        </header>

        {visiblePrograms.length === 0 && (
          <div className="rounded border border-pir bg-pir-surface-0 px-4 py-5 text-body text-pir-text-muted">
            {strings.empty}
          </div>
        )}

        {visiblePrograms.map((program) => (
          <section key={program.name} className="flex flex-col gap-3">
            <div className="flex items-center gap-2">
              <h2 className="font-mono text-caption uppercase tracking-[0.1em] text-pir-text-tertiary">
                {program.name}
              </h2>
              <span className="rounded border border-pir bg-pir-surface-1 px-1.5 py-0.5 font-mono text-[10px] text-pir-text-muted">
                {program.projects.length}
              </span>
            </div>
            <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-3">
              {program.projects.map((project) => {
                const pulse = projectPulseFromCounts(project);
                return (
                  // next/link applies the basePath ("/ui") automatically; a
                  // plain <a> would not, so the card click 404'd on the static
                  // export (gh #23).
                  <Link
                    key={project.slug}
                    href={`/projects/?slug=${encodeURIComponent(project.slug)}`}
                    data-tour="project-row"
                    className="group rounded border border-pir bg-pir-surface-0 px-4 py-3 text-left transition-colors hover:border-pir-accent"
                    style={project.color ? { borderTopColor: project.color } : undefined}
                  >
                    <div className="flex items-start gap-3">
                      <ProjectDot color={project.color} />
                      <div className="min-w-0 flex-1">
                        <div className="truncate text-label font-semibold text-pir-text-primary">
                          {projectDisplayName(project)}
                        </div>
                        <div className="mt-1 flex flex-wrap gap-1.5">
                          {project.type && (
                            <span className="rounded border border-pir bg-pir-surface-1 px-1.5 py-0.5 text-caption text-pir-text-muted">
                              {project.type === "code" ? t.projects.dashboard.code : t.projects.dashboard.noCode}
                            </span>
                          )}
                          {project.scope && (
                            <span className="rounded border border-pir bg-pir-surface-1 px-1.5 py-0.5 text-caption text-pir-text-muted">
                              {project.scope === "personal" ? t.projects.dashboard.personal : t.projects.dashboard.work}
                            </span>
                          )}
                        </div>
                      </div>
                    </div>
                    <div className="mt-3 h-1.5 overflow-hidden rounded bg-pir-surface-2" title={strings.progressTooltip}>
                      <div
                        className="h-full rounded bg-pir-success"
                        style={{
                          width: `${pulse.percent}%`,
                          backgroundColor: project.color ?? undefined,
                        }}
                      />
                    </div>
                    <div className="mt-2 flex items-center justify-between font-mono text-caption text-pir-text-muted">
                      <span>{pulse.open} {strings.openTasks}</span>
                      <span>{pulse.completed}/{pulse.total}</span>
                    </div>
                  </Link>
                );
              })}
            </div>
          </section>
        ))}
      </div>
    </div>
  );
}

function resolveTokenHex(token: string): string | null {
  const value = getComputedStyle(document.documentElement).getPropertyValue(token).trim();
  return hslStringToHex(value);
}

function ColorSelector({
  slug,
  color,
  onColorChange,
  toast,
}: {
  slug: string;
  color: string | null | undefined;
  onColorChange: (color: string | null) => void;
  toast: ToastSetter;
}) {
  const { t } = useT();
  const strings = t.projects.dashboard;
  const [open, setOpen] = useState(false);
  const [saving, setSaving] = useState(false);

  async function saveColor(nextColor: string | null, message: string) {
    setSaving(true);
    try {
      const updated = await updateProjectColor(slug, nextColor);
      const persisted = updated.color ?? null;
      onColorChange(persisted);
      dispatchProjectColorChanged({ slug, color: persisted });
      toast(message);
      setOpen(false);
    } catch {
      toast(strings.colorError);
    } finally {
      setSaving(false);
    }
  }

  async function pickToken(token: string) {
    const nextColor = resolveTokenHex(token);
    if (!nextColor) {
      toast(strings.colorError);
      return;
    }
    await saveColor(nextColor, strings.colorSaved);
  }

  return (
    <div className="relative inline-flex">
      <button
        type="button"
        onClick={() => setOpen((value) => !value)}
        aria-label={strings.color}
        className="flex h-8 items-center gap-2 rounded border border-pir bg-pir-surface-1 px-2 text-caption text-pir-text-secondary transition-colors hover:border-pir-accent hover:text-pir-text-primary"
      >
        <ProjectDot color={color} size="lg" />
        <span>{strings.color}</span>
      </button>
      {open && (
        <>
          <button
            type="button"
            aria-label={t.projects.editor.close}
            className="fixed inset-0 z-40 cursor-default"
            onClick={() => setOpen(false)}
          />
          <div className="absolute left-0 top-10 z-50 flex min-w-48 flex-col gap-2 rounded border border-pir bg-pir-surface-0 p-3 shadow-xl">
            <div className="flex gap-2">
              {COLOR_TOKENS.map((token, index) => (
                <button
                  key={token}
                  type="button"
                  disabled={saving}
                  aria-label={`${strings.color} ${index + 1}`}
                  onClick={() => void pickToken(token)}
                  className="h-7 w-7 rounded border border-pir transition-transform hover:scale-105 disabled:opacity-50"
                  style={{ backgroundColor: `hsl(var(${token}))` }}
                />
              ))}
            </div>
            <button
              type="button"
              disabled={saving}
              onClick={() => void saveColor(null, strings.colorReset)}
              className="h-8 rounded border border-pir bg-pir-surface-1 px-2 text-caption text-pir-text-tertiary transition-colors hover:border-pir-accent hover:text-pir-text-primary disabled:opacity-50"
            >
              {strings.resetColor}
            </button>
          </div>
        </>
      )}
    </div>
  );
}

function PulseStrip({ slug, tasks }: { slug: string; tasks: TaskResponse[] }) {
  const { t } = useT();
  const strings = t.projects.dashboard;
  const router = useRouter();
  const pulse = computeProjectPulse(tasks);
  const openTasks = () => router.push(`/tasks?project=${encodeURIComponent(slug)}`);
  const tiles = [
    { label: strings.pulseOpen, value: String(pulse.open), onClick: openTasks },
    { label: strings.pulseReview, value: String(pulse.review), onClick: openTasks },
    {
      label: strings.pulseProgress,
      value: pulse.fraction,
      title: strings.pulseProgressTooltip,
      suffix: `${pulse.percent}%`,
    },
  ];

  return (
    <div className="grid gap-3 md:grid-cols-3">
      {tiles.map((tile) => {
        const Comp = tile.onClick ? "button" : "div";
        return (
          <Comp
            key={tile.label}
            type={tile.onClick ? "button" : undefined}
            onClick={tile.onClick}
            title={tile.title}
            className={`min-h-20 rounded border border-pir bg-pir-surface-0 px-4 py-3 text-left ${
              tile.onClick ? "transition-colors hover:border-pir-accent" : ""
            }`}
          >
            <div className="font-mono text-caption uppercase tracking-[0.08em] text-pir-text-muted">
              {tile.label}
            </div>
            <div className="mt-2 flex items-end justify-between gap-3">
              <span className="text-heading text-pir-text-primary">{tile.value}</span>
              {tile.suffix && (
                <span className="font-mono text-caption text-pir-text-muted">{tile.suffix}</span>
              )}
            </div>
          </Comp>
        );
      })}
    </div>
  );
}

function DiarySection({ journals }: { journals: NormalizedDiaryDay[] }) {
  const { t } = useT();
  const strings = t.projects.dashboard;
  const items = journals.flatMap((journal) =>
    journal.progressGroups.flatMap((group) =>
      group.items.map((item) => ({ ...item, day: journal.cycleKey, project: group.project }))
    )
  ).slice(0, 6);

  return (
    <Section title={strings.diary} caption={strings.diarySource} count={items.length}>
      {items.length === 0 ? (
        <p className="text-body text-pir-text-muted">{strings.diaryEmpty}</p>
      ) : (
        <div className="divide-y divide-pir">
          {items.map((item) => (
            <article key={item.id} className="grid grid-cols-[72px_minmax(0,1fr)] gap-3 py-3">
              <span className="font-mono text-caption text-pir-text-muted">{item.day}</span>
              <p className="text-body leading-relaxed text-pir-text-secondary">{item.text}</p>
            </article>
          ))}
        </div>
      )}
    </Section>
  );
}

function DecisionsSection({ decisions }: { decisions: AdrDisplay[] }) {
  const { t } = useT();
  const strings = t.projects.dashboard;

  return (
    <Section title={strings.decisions} caption={strings.decisionsPath} count={decisions.length}>
      {decisions.length === 0 ? (
        <p className="text-body text-pir-text-muted">{strings.decisionsEmpty}</p>
      ) : (
        <div className="flex flex-col gap-2">
          {decisions.map((adr) => (
            <article
              key={adr.filename}
              className={`rounded border border-pir bg-pir-surface-0 px-3 py-3 ${
                adr.status === "superseded" ? "opacity-70" : ""
              }`}
            >
              <div className="flex items-start justify-between gap-3">
                <div className="min-w-0">
                  <h3 className="truncate text-label font-semibold text-pir-text-primary">
                    {adr.title}
                  </h3>
                  {adr.excerpt && (
                    <p className="mt-1 text-body leading-relaxed text-pir-text-tertiary">
                      {adr.excerpt}
                    </p>
                  )}
                </div>
                <span className={`shrink-0 rounded border px-2 py-0.5 text-caption ${
                  adr.status === "superseded"
                    ? "border-pir bg-pir-surface-1 text-pir-text-muted"
                    : "border-pir-success/40 bg-pir-success/10 text-pir-success"
                }`}
                >
                  {adr.status === "superseded" ? strings.superseded : strings.current}
                </span>
              </div>
              <div className="mt-2 flex flex-wrap gap-2 font-mono text-caption text-pir-text-muted">
                {adr.date && <span>{adr.date}</span>}
                <span>{adr.filename}</span>
                {adr.supersededBy && <span>{adr.supersededBy}</span>}
              </div>
            </article>
          ))}
        </div>
      )}
    </Section>
  );
}

function QuestionsSection({
  slug,
  questions,
  toast,
}: {
  slug: string;
  questions: DiaryItem[];
  toast: ToastSetter;
}) {
  const { t } = useT();
  const strings = t.projects.dashboard;
  const [savingId, setSavingId] = useState<string | null>(null);

  async function addTodo(question: DiaryItem) {
    setSavingId(question.id);
    try {
      await createTodoLocal({
        text: question.text,
        type: "decidi",
        project: slug,
        source: "brain",
        source_ref: question.sourceRef,
      });
      toast(strings.addedTodo);
    } catch {
      toast(strings.todoError);
    } finally {
      setSavingId(null);
    }
  }

  return (
    <Section title={strings.questions} count={questions.length}>
      {questions.length === 0 ? (
        <p className="text-body text-pir-text-muted">{strings.questionsEmpty}</p>
      ) : (
        <div className="divide-y divide-pir">
          {questions.map((question) => (
            <div key={question.id} className="flex items-start gap-3 py-3">
              <span className="mt-2 h-1.5 w-1.5 shrink-0 rounded-full bg-pir-accent" aria-hidden />
              <p className="min-w-0 flex-1 text-body leading-relaxed text-pir-text-secondary">
                {question.text}
              </p>
              <button
                type="button"
                disabled={savingId === question.id}
                onClick={() => void addTodo(question)}
                className="shrink-0 rounded border border-pir bg-pir-surface-1 px-2 py-1 text-caption text-pir-text-tertiary transition-colors hover:border-pir-accent hover:text-pir-text-primary disabled:opacity-50"
              >
                {strings.addTodo}
              </button>
            </div>
          ))}
        </div>
      )}
    </Section>
  );
}

function BrainSection({ learnings }: { learnings: LearningResponse[] }) {
  const { t } = useT();
  const strings = t.projects.dashboard;

  return (
    <Section title={strings.brain} caption={strings.brainSource} count={learnings.length}>
      {learnings.length === 0 ? (
        <p className="text-body text-pir-text-muted">{strings.brainEmpty}</p>
      ) : (
        <div className="flex flex-col gap-2">
          {learnings.map((learning) => (
            <article key={learning.id} className="rounded border border-pir bg-pir-surface-0 px-3 py-3">
              <div className="flex items-start justify-between gap-3">
                <h3 className="text-label font-semibold text-pir-text-primary">{learning.title}</h3>
                <span className="rounded border border-pir bg-pir-surface-1 px-1.5 py-0.5 font-mono text-caption text-pir-text-muted">
                  {learning.severity}
                </span>
              </div>
              <p className="mt-1 text-body leading-relaxed text-pir-text-tertiary">
                {learning.prevention || learning.description}
              </p>
              <div className="mt-2 flex flex-wrap gap-1.5">
                {learning.tags.slice(0, 4).map((tag) => (
                  <span key={tag} className="rounded border border-pir bg-pir-surface-1 px-1.5 py-0.5 text-caption text-pir-text-muted">
                    {tag}
                  </span>
                ))}
              </div>
            </article>
          ))}
        </div>
      )}
    </Section>
  );
}

function DocsModal({
  docs,
  onClose,
  onOpen,
}: {
  docs: DocEntry[];
  onClose: () => void;
  onOpen: (doc: DocEntry) => void;
}) {
  const { t } = useT();
  const strings = t.projects.docsModal;
  const [query, setQuery] = useState("");
  const [kind, setKind] = useState("all");
  const kinds = useMemo(() => ["all", ...Array.from(new Set(docs.map(docKind)))], [docs]);
  const filtered = docs.filter((doc) => {
    const docType = docKind(doc);
    const haystack = `${doc.filename} ${doc.title ?? ""}`.toLowerCase();
    return (kind === "all" || kind === docType) && haystack.includes(query.trim().toLowerCase());
  });

  return (
    <div className="fixed inset-0 z-[85] flex items-center justify-center p-4">
      <button
        type="button"
        aria-label={t.projects.editor.close}
        className="absolute inset-0 cursor-default bg-pir-base/70"
        onClick={onClose}
      />
      <section className="relative flex max-h-[82vh] w-[min(92vw,680px)] flex-col overflow-hidden rounded border border-pir bg-pir-surface-0 text-pir-text-primary shadow-xl">
        <header className="flex items-center justify-between border-b border-pir px-4 py-3">
          <h2 className="text-label font-semibold">{strings.title}</h2>
          <button
            type="button"
            onClick={onClose}
            className="rounded border border-pir bg-pir-surface-1 px-2 py-1 text-caption text-pir-text-muted hover:text-pir-text-primary"
          >
            {t.projects.editor.close}
          </button>
        </header>
        <div className="border-b border-pir px-4 py-3">
          <input
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            placeholder={strings.search}
            aria-label={strings.search}
            className="h-8 w-full rounded border border-pir bg-pir-base px-3 text-body text-pir-text-primary outline-none placeholder:text-pir-text-muted focus:border-pir-accent"
          />
          <div className="mt-2 flex flex-wrap gap-1.5">
            {kinds.map((item) => (
              <button
                key={item}
                type="button"
                onClick={() => setKind(item)}
                className={`rounded border px-2 py-1 text-caption transition-colors ${
                  kind === item
                    ? "border-pir-accent bg-pir-accent/10 text-pir-text-primary"
                    : "border-pir bg-pir-surface-1 text-pir-text-muted hover:text-pir-text-primary"
                }`}
              >
                {item === "all" ? strings.all : item}
              </button>
            ))}
          </div>
        </div>
        <div className="min-h-0 flex-1 overflow-y-auto p-3">
          {filtered.length === 0 && (
            <p className="px-1 py-4 text-body text-pir-text-muted">{strings.empty}</p>
          )}
          <div className="flex flex-col gap-2">
            {filtered.map((doc) => (
              <button
                key={doc.filename}
                type="button"
                onClick={() => onOpen(doc)}
                className="rounded border border-pir bg-pir-surface-1 px-3 py-2 text-left transition-colors hover:border-pir-accent"
              >
                <div className="flex items-center justify-between gap-3">
                  <span className="min-w-0 truncate font-mono text-caption text-pir-text-secondary">
                    {doc.title || doc.filename}
                  </span>
                  <span className="shrink-0 text-caption text-pir-text-muted">{strings.open}</span>
                </div>
                <div className="mt-1 font-mono text-[10px] text-pir-text-muted">{doc.filename}</div>
              </button>
            ))}
          </div>
        </div>
      </section>
    </div>
  );
}

function DocumentDrawer({
  slug,
  doc,
  onClose,
}: {
  slug: string;
  doc: DocEntry | null;
  onClose: () => void;
  toast: ToastSetter;
}) {
  const { t } = useT();
  const strings = t.projects.editor;
  const [content, setContent] = useState("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const isMd = Boolean(doc && isMarkdownDoc(doc.filename));

  useEffect(() => {
    if (!doc || !isMd) return;
    const controller = new AbortController();
    setLoading(true);
    setError(null);
    getProjectFile(slug, doc.filename, { signal: controller.signal })
      .then((file) => setContent(file.content))
      .catch((err) => {
        if (err instanceof DOMException && err.name === "AbortError") return;
        setError(strings.loadError);
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false);
      });
    return () => controller.abort();
  }, [doc, isMd, slug, strings.loadError]);

  return (
    <Drawer
      open={Boolean(doc)}
      onClose={onClose}
      widthClassName="w-[min(94vw,680px)]"
      header={
        <div className="flex min-w-0 items-center justify-between gap-3">
          <div className="min-w-0">
            <h2 className="truncate font-mono text-label text-pir-text-primary">
              {doc?.filename}
            </h2>
          </div>
          <button
            type="button"
            onClick={onClose}
            className="rounded border border-pir bg-pir-surface-1 px-2 py-1 text-caption text-pir-text-muted hover:text-pir-text-primary"
          >
            {strings.close}
          </button>
        </div>
      }
    >
      {!isMd ? (
        <div className="flex h-full items-center justify-center text-center text-body text-pir-text-muted">
          {strings.unsupported}
        </div>
      ) : loading ? (
        <p className="text-body text-pir-text-muted">{t.projects.dashboard.loading}</p>
      ) : error ? (
        <ErrorAlert message={error} />
      ) : (
        <SafeMarkdown content={content} />
      )}
    </Drawer>
  );
}

function DocsSection({ slug, project, docs, toast }: { slug: string; project: ProjectDetail; docs: DocEntry[]; toast: ToastSetter }) {
  const { t } = useT();
  const strings = t.projects.dashboard;
  const sorted = useMemo(() => sortDocs(docs), [docs]);
  const topDocs = sorted.slice(0, 4);
  const [modalOpen, setModalOpen] = useState(false);
  const [editingDoc, setEditingDoc] = useState<DocEntry | null>(null);

  return (
    <>
      <Section title={strings.docs} count={docs.length}>
        {project.type === "code" && (
          <p className="mb-3 rounded border border-pir bg-pir-surface-0 px-3 py-2 text-caption text-pir-text-muted">
            {strings.docsCodeCaption}
          </p>
        )}
        {topDocs.length === 0 ? (
          <p className="text-body text-pir-text-muted">{strings.docsEmpty}</p>
        ) : (
          <div className="flex flex-col gap-2">
            {topDocs.map((doc) => (
              <button
                key={doc.filename}
                type="button"
                onClick={() => setEditingDoc(doc)}
                className="rounded border border-pir bg-pir-surface-0 px-3 py-2 text-left transition-colors hover:border-pir-accent"
              >
                <div className="truncate font-mono text-caption text-pir-text-secondary">
                  {doc.title || doc.filename}
                </div>
                <div className="mt-1 flex items-center justify-between gap-2 font-mono text-[10px] text-pir-text-muted">
                  <span className="truncate">{doc.filename}</span>
                  <span>{docKind(doc)}</span>
                </div>
              </button>
            ))}
          </div>
        )}
        {docs.length > topDocs.length && (
          <button
            type="button"
            onClick={() => setModalOpen(true)}
            className="mt-3 rounded border border-pir bg-pir-surface-1 px-2.5 py-1 text-caption text-pir-text-tertiary transition-colors hover:border-pir-accent hover:text-pir-text-primary"
          >
            {replaceCount(strings.viewAllDocs, docs.length)}
          </button>
        )}
      </Section>
      {modalOpen && (
        <DocsModal
          docs={sorted}
          onClose={() => setModalOpen(false)}
          onOpen={(doc) => {
            setModalOpen(false);
            setEditingDoc(doc);
          }}
        />
      )}
      <DocumentDrawer slug={slug} doc={editingDoc} onClose={() => setEditingDoc(null)} toast={toast} />
    </>
  );
}

function RelationsSection({
  currentSlug,
  programs,
  relations,
  onRelationsChange,
  toast,
}: {
  currentSlug: string;
  programs: ProgramInfo[];
  relations: ProjectRelation[];
  onRelationsChange: (relations: ProjectRelation[]) => void;
  toast: ToastSetter;
}) {
  const { t } = useT();
  const strings = t.projects.dashboard;
  const projects = allProjects(programs);
  const [expanded, setExpanded] = useState(false);
  const [targetSlug, setTargetSlug] = useState("");
  const [kind, setKind] = useState<ManualProjectEdgeKind>("related");
  const shown = expanded ? relations : relations.slice(0, 2);
  const candidates = projects.filter((project) =>
    project.slug !== currentSlug && !relations.some((relation) => relation.slug === project.slug)
  );

  async function addRelation() {
    if (!targetSlug) return;
    try {
      await upsertManualProjectEdge({ src_slug: currentSlug, dst_slug: targetSlug, kind });
      onRelationsChange([...relations, { slug: targetSlug, kind }]);
      setTargetSlug("");
      toast(strings.relationSaved);
    } catch {
      toast(strings.relationError);
    }
  }

  async function removeRelation(relation: ProjectRelation) {
    try {
      await deleteManualProjectEdge({ src_slug: currentSlug, dst_slug: relation.slug, kind: relation.kind });
      onRelationsChange(relations.filter((item) => !(item.slug === relation.slug && item.kind === relation.kind)));
      toast(strings.relationRemoved);
    } catch {
      toast(strings.relationError);
    }
  }

  function relationLabel(relationKind: ManualProjectEdgeKind): string {
    return relationKind === "depends_on" ? strings.relationDepends : strings.relationRelated;
  }

  return (
    <div className="mt-4 border-t border-pir pt-4">
      <div className="mb-2 font-mono text-caption uppercase tracking-[0.08em] text-pir-text-muted">
        {strings.relations}
      </div>
      <div className="flex flex-wrap items-center gap-2">
        {shown.map((relation) => {
          const project = projects.find((item) => item.slug === relation.slug);
          return (
            <span key={`${relation.slug}:${relation.kind}`} className="inline-flex max-w-full items-center gap-2 rounded border border-pir bg-pir-surface-1 px-2 py-1 text-caption text-pir-text-secondary">
              <ProjectDot color={project?.color} size="sm" />
              <span className="truncate">{project ? projectDisplayName(project) : relation.slug}</span>
              <span className="font-mono text-[10px] uppercase text-pir-text-muted">{relationLabel(relation.kind)}</span>
              <button
                type="button"
                onClick={() => void removeRelation(relation)}
                className="text-pir-text-muted hover:text-pir-text-primary"
                aria-label={`${strings.removeRelation}: ${relation.slug}`}
              >
                x
              </button>
            </span>
          );
        })}
        {!expanded && relations.length > shown.length && (
          <button
            type="button"
            onClick={() => setExpanded(true)}
            className="rounded border border-pir bg-pir-surface-1 px-2 py-1 text-caption text-pir-text-muted"
          >
            {replaceCount(strings.more, relations.length - shown.length)}
          </button>
        )}
        {expanded && relations.length > 2 && (
          <button
            type="button"
            onClick={() => setExpanded(false)}
            className="rounded border border-pir bg-pir-surface-1 px-2 py-1 text-caption text-pir-text-muted"
          >
            {strings.collapse}
          </button>
        )}
      </div>
      <div className="mt-3 flex flex-wrap gap-2">
        <select
          value={targetSlug}
          onChange={(event) => setTargetSlug(event.target.value)}
          aria-label={strings.addRelation}
          className="h-8 min-w-44 rounded border border-pir bg-pir-surface-1 px-2 text-caption text-pir-text-primary outline-none focus:border-pir-accent"
        >
          <option value="">{strings.addRelation}</option>
          {candidates.map((project) => (
            <option key={project.slug} value={project.slug}>
              {projectDisplayName(project)}
            </option>
          ))}
        </select>
        <select
          value={kind}
          onChange={(event) => setKind(event.target.value as ManualProjectEdgeKind)}
          aria-label={strings.relations}
          className="h-8 rounded border border-pir bg-pir-surface-1 px-2 text-caption text-pir-text-primary outline-none focus:border-pir-accent"
        >
          <option value="related">{strings.relationRelated}</option>
          <option value="depends_on">{strings.relationDepends}</option>
        </select>
        <button
          type="button"
          onClick={() => void addRelation()}
          disabled={!targetSlug}
          className="h-8 rounded border border-pir-accent bg-pir-accent/10 px-3 text-caption text-pir-text-primary transition-colors hover:bg-pir-accent/20 disabled:opacity-50"
        >
          {strings.addRelation}
        </button>
      </div>
    </div>
  );
}

function CodePanel({
  project,
  tasks,
  branches,
  commits,
}: {
  project: ProjectDetail;
  tasks: TaskResponse[];
  branches: GitBranch[];
  commits: GitCommit[];
}) {
  const { t } = useT();
  const strings = t.projects.dashboard;
  if (project.type !== "code") return null;
  const currentBranch = branches.find((branch) => branch.is_current)?.name ?? branches[0]?.name ?? null;
  const prTasks = tasks.filter((task) => task.pr_status);
  const repoName = project.repo_path?.split("/").filter(Boolean).pop() ?? project.slug;
  const latestCommit = commits[0]?.hash_short ?? null;

  return (
    <Section title={strings.codePanel}>
      <div className="rounded border border-pir bg-pir-surface-0 px-3 py-3">
        <p className="mb-3 rounded border border-pir bg-pir-surface-1 px-3 py-2 text-caption text-pir-text-muted">
          {strings.codeBanner}
        </p>
        <div className="grid gap-2 font-mono text-caption text-pir-text-secondary">
          <div className="flex justify-between gap-3">
            <span className="text-pir-text-muted">repo</span>
            <span className="truncate">{repoName}</span>
          </div>
          <div className="flex justify-between gap-3">
            <span className="text-pir-text-muted">{strings.branch}</span>
            <span className="truncate">{currentBranch ?? "-"}</span>
          </div>
          {latestCommit && (
            <div className="flex justify-between gap-3">
              <span className="text-pir-text-muted">commit</span>
              <span>{latestCommit}</span>
            </div>
          )}
        </div>
        <div className="mt-3 border-t border-pir pt-3">
          <div className="mb-2 font-mono text-caption uppercase text-pir-text-muted">
            {strings.pullRequests}
          </div>
          {prTasks.length === 0 ? (
            <p className="text-caption text-pir-text-muted">{strings.noPullRequests}</p>
          ) : (
            <div className="flex flex-col gap-1.5">
              {prTasks.map((task) => (
                <Link
                  key={task.id}
                  href={`/tasks?project=${encodeURIComponent(project.slug)}`}
                  className="rounded border border-pir bg-pir-surface-1 px-2 py-1 text-caption text-pir-text-secondary hover:border-pir-accent"
                >
                  {task.title}
                </Link>
              ))}
            </div>
          )}
        </div>
      </div>
    </Section>
  );
}

function Toast({ message }: { message: string | null }) {
  if (!message) return null;
  return (
    <div
      role="status"
      className="fixed bottom-10 right-6 z-[100] rounded border border-pir bg-pir-surface-1 px-3 py-2 text-label text-pir-text-primary shadow-xl"
    >
      {message}
    </div>
  );
}

function ProjectDashboard({
  slug,
  programs,
  selectedProject,
  onProjectColorChange,
}: {
  slug: string;
  programs: ProgramInfo[];
  selectedProject: ProjectInfo | null;
  onProjectColorChange: (slug: string, color: string | null) => void;
}) {
  const { t } = useT();
  const strings = t.projects.dashboard;
  const router = useRouter();
  const [detail, setDetail] = useState<ProjectDetail | null>(null);
  const [tasks, setTasks] = useState<TaskResponse[]>([]);
  const [journals, setJournals] = useState<NormalizedDiaryDay[]>([]);
  const [questions, setQuestions] = useState<DiaryItem[]>([]);
  const [docs, setDocs] = useState<DocEntry[]>([]);
  const [decisions, setDecisions] = useState<AdrDisplay[]>([]);
  const [learnings, setLearnings] = useState<LearningResponse[]>([]);
  const [relations, setRelations] = useState<ProjectRelation[]>([]);
  const [branches, setBranches] = useState<GitBranch[]>([]);
  const [commits, setCommits] = useState<GitCommit[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [toast, setToast] = useState<string | null>(null);

  useEffect(() => {
    if (!toast) return;
    const timeout = window.setTimeout(() => setToast(null), 2200);
    return () => window.clearTimeout(timeout);
  }, [toast]);

  useEffect(() => {
    const controller = new AbortController();
    setLoading(true);
    setError(null);
    Promise.all([
      getProjectDetail(slug, { signal: controller.signal, deep: true }),
      listTasks({ project: slug, detailed: true, limit: 500, sort: "updated_at:desc" }, { signal: controller.signal }),
      listBrainJournal({ scope_type: "project", scope_key: slug, limit: 20 }, { signal: controller.signal }),
      getProjectDocs(slug, { signal: controller.signal }),
      listLearnings({ project: slug, limit: 6 }, { signal: controller.signal }),
    ])
      .then(async ([nextDetail, nextTasks, journalResponse, nextDocs, nextLearnings]) => {
        if (controller.signal.aborted) return;
        setDetail(nextDetail);
        setTasks(nextTasks);
        const normalized = journalResponse.items.map((entry: BrainJournalEntryResponse) => normalizeJournalEntry(entry));
        setJournals(normalized);
        setQuestions(normalized.flatMap((entry) => entry.decisions).slice(0, 6));
        setDocs(nextDocs);
        setLearnings(nextLearnings);
        setRelations(relationsFromProjectDetail(nextDetail));
        const decisionDocs = nextDocs.filter(isDecisionDoc);
        const parsed = await Promise.all(decisionDocs.map(async (doc) => {
          try {
            const file = await getProjectFile(slug, doc.filename, { signal: controller.signal });
            return parseAdrDisplay(doc, file.content);
          } catch {
            return parseAdrDisplay(doc, "");
          }
        }));
        if (!controller.signal.aborted) {
          setDecisions(parsed);
        }
      })
      .catch((err) => {
        if (err instanceof DOMException && err.name === "AbortError") return;
        setError(err instanceof Error ? err.message : strings.error);
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false);
      });
    return () => controller.abort();
  }, [slug, strings.error]);

  useEffect(() => {
    if (!detail || detail.type !== "code") {
      setBranches([]);
      setCommits([]);
      return;
    }
    const controller = new AbortController();
    Promise.all([
      getProjectGitBranches(slug, { signal: controller.signal }),
      getProjectGitLog(slug, 5, { signal: controller.signal }),
    ])
      .then(([nextBranches, nextCommits]) => {
        if (!controller.signal.aborted) {
          setBranches(nextBranches);
          setCommits(nextCommits);
        }
      })
      .catch(() => {});
    return () => controller.abort();
  }, [detail, slug]);

  if (loading) {
    return (
      <div className="flex min-h-0 flex-1 items-center justify-center text-body text-pir-text-muted">
        {strings.loading}
      </div>
    );
  }

  if (error || !detail) {
    return (
      <div className="flex min-h-0 flex-1 flex-col items-center justify-center gap-3">
        <ErrorAlert message={error || strings.error} />
        <button
          type="button"
          onClick={() => router.push("/projects/")}
          className="rounded border border-pir bg-pir-surface-1 px-3 py-1 text-caption text-pir-text-secondary hover:border-pir-accent"
        >
          {strings.back}
        </button>
      </div>
    );
  }

  const color = detail.color ?? selectedProject?.color ?? null;
  const headerStyle = {
    "--project-color": color ?? "hsl(var(--pir-surface-2))",
    background: "linear-gradient(180deg, color-mix(in srgb, var(--project-color) 14%, transparent), transparent)",
  } as CSSProperties;

  function handleColorChange(nextColor: string | null) {
    setDetail((current) => current ? { ...current, color: nextColor } : current);
    onProjectColorChange(slug, nextColor);
  }

  return (
    <>
      <div data-tour="project-dash" className="min-h-0 flex-1 overflow-y-auto">
        <header className="border-b border-pir px-6 py-5 md:px-8" style={headerStyle}>
          <div className="mx-auto max-w-6xl">
            <div className="mb-3 flex items-center gap-2 text-caption text-pir-text-muted">
              <button
                type="button"
                onClick={() => router.push("/projects/")}
                className="hover:text-pir-text-primary"
              >
                {strings.breadcrumbRoot}
              </button>
              <span>/</span>
              {detail.program && <span>{detail.program}</span>}
            </div>
            <div className="flex flex-wrap items-start justify-between gap-4">
              <div className="min-w-0">
                <div className="flex items-center gap-3">
                  <ColorSelector slug={slug} color={color} onColorChange={handleColorChange} toast={setToast} />
                  <h1 className="truncate text-heading text-pir-text-primary">{detail.name || slug}</h1>
                </div>
                {detail.description && (
                  <p className="mt-2 max-w-3xl text-body leading-relaxed text-pir-text-secondary">
                    {detail.description}
                  </p>
                )}
                <div className="mt-3 flex flex-wrap gap-2">
                  {detail.program && <span className="rounded border border-pir bg-pir-surface-0 px-2 py-1 text-caption text-pir-text-muted">{detail.program}</span>}
                  {detail.type && <span className="rounded border border-pir bg-pir-surface-0 px-2 py-1 text-caption text-pir-text-muted">{detail.type === "code" ? strings.code : strings.noCode}</span>}
                  {detail.scope && <span className="rounded border border-pir bg-pir-surface-0 px-2 py-1 text-caption text-pir-text-muted">{detail.scope === "personal" ? strings.personal : strings.work}</span>}
                  {detail.language && <span className="rounded border border-pir bg-pir-surface-0 px-2 py-1 text-caption text-pir-text-muted">{detail.language}</span>}
                  <span className="rounded border border-pir bg-pir-surface-0 px-2 py-1 text-caption text-pir-text-muted">
                    {strings.status}: <span className="text-pir-text-secondary">{statusLabel(detail)}</span>
                    <span className="ml-2 font-mono text-[10px] text-pir-text-muted">{strings.statusCaption}</span>
                  </span>
                </div>
              </div>
              <Link
                href={`/tasks?project=${encodeURIComponent(slug)}`}
                className="rounded border border-pir bg-pir-surface-1 px-3 py-2 text-caption text-pir-text-secondary transition-colors hover:border-pir-accent hover:text-pir-text-primary"
              >
                {strings.headerTaskButton}
              </Link>
            </div>
            <RelationsSection
              currentSlug={slug}
              programs={programs}
              relations={relations}
              onRelationsChange={setRelations}
              toast={setToast}
            />
          </div>
        </header>

        <main className="mx-auto grid max-w-6xl gap-6 px-6 py-6 md:grid-cols-[minmax(0,1.25fr)_minmax(320px,0.85fr)] md:px-8">
          <div className="flex min-w-0 flex-col gap-7">
            <PulseStrip slug={slug} tasks={tasks} />
            <DiarySection journals={journals} />
            <DecisionsSection decisions={decisions} />
            <QuestionsSection slug={slug} questions={questions} toast={setToast} />
            <CodePanel project={detail} tasks={tasks} branches={branches} commits={commits} />
          </div>
          <div className="flex min-w-0 flex-col gap-7">
            <BrainSection learnings={learnings} />
            <DocsSection slug={slug} project={detail} docs={docs} toast={setToast} />
          </div>
        </main>
      </div>
      <Toast message={toast} />
    </>
  );
}

function LocalProjectsSurfaceContent() {
  const { t } = useT();
  const searchParams = useSearchParams();
  const slug = searchParams.get("slug");
  const [programs, setPrograms] = useState<ProgramInfo[]>([]);
  const [loading, setLoading] = useState(true);
  const selectedProject = useMemo(() => findProject(programs, slug), [programs, slug]);

  useEffect(() => {
    if (!isLocalMode()) return;
    const controller = new AbortController();
    getPrograms({ signal: controller.signal })
      .then(setPrograms)
      .catch(() => {})
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false);
      });
    return () => controller.abort();
  }, []);

  function handleProjectColorChange(projectSlug: string, color: string | null) {
    setPrograms((current) => patchProgramProjectColor(current, projectSlug, color));
  }

  if (loading) {
    return (
      <div className="flex min-h-0 flex-1 items-center justify-center text-body text-pir-text-muted">
        {t.projects.dashboard.loading}
      </div>
    );
  }

  if (!slug) return <ProjectsOverview programs={programs} />;

  return (
    <ProjectDashboard
      slug={slug}
      programs={programs}
      selectedProject={selectedProject}
      onProjectColorChange={handleProjectColorChange}
    />
  );
}

export default function LocalProjectsSurface() {
  return (
    <Suspense fallback={null}>
      <LocalProjectsSurfaceContent />
    </Suspense>
  );
}
