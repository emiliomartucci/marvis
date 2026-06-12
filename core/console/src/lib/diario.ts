import {
  APIError,
  createTodoLocal,
  delegateTodoLocal,
  triggerBrainRun,
  type BrainJournalEntryResponse,
  type BrainRunResponse,
  type TodoResponseLocal,
} from "@/lib/api";

export type TimelineState = "needs_decision" | "managed" | "quiet" | "not_run";
export type DiaryLimitState = "active" | "quiet" | "not_run";
export type BrainRunActionState = "started" | "already_running";

export interface DiaryItem {
  id: string;
  text: string;
  project: string | null;
  sourceRef: string | null;
}

export interface DiaryProgressGroup {
  project: string | null;
  items: Array<DiaryItem & { kind: "change" | "decision" }>;
}

export interface NormalizedDiaryDay {
  cycleKey: string;
  entryId: string;
  runId: string;
  narrative: string | null;
  baseSummary: string | null;
  narrativeFallback: boolean;
  isEmpty: boolean;
  decisions: DiaryItem[];
  context: DiaryItem[];
  progressGroups: DiaryProgressGroup[];
  projectsTouched: string[];
  counts: {
    decisions: number;
    progress: number;
    context: number;
    sources: number;
    tomorrowWatch: number;
  };
}

export interface TimelineDay {
  cycleKey: string;
  run: BrainRunResponse | null;
  journal: NormalizedDiaryDay | null;
  state: TimelineState;
}

type JournalObject = Record<string, unknown>;

function asObjects(value: unknown): JournalObject[] {
  return Array.isArray(value)
    ? value.filter((item): item is JournalObject => Boolean(item) && typeof item === "object")
    : [];
}

function asStrings(value: unknown): string[] {
  return Array.isArray(value)
    ? value.filter((item): item is string => typeof item === "string")
    : [];
}

function asString(value: unknown): string | null {
  return typeof value === "string" && value.trim() ? value.trim() : null;
}

function itemProject(item: JournalObject): string | null {
  return (
    asString(item.project) ??
    asString(item.proj) ??
    asString(item.source_project) ??
    asString(item.target_project) ??
    null
  );
}

function itemText(item: JournalObject, fallback: string): string {
  return (
    asString(item.text) ??
    asString(item.title) ??
    asString(item.summary) ??
    asString(item.question) ??
    asString(item.ref) ??
    fallback
  );
}

function itemSourceRef(item: JournalObject): string | null {
  return asString(item.source_ref) ?? asString(item.event_id) ?? asString(item.ref);
}

function normalizeObjectItem(item: JournalObject, id: string, fallback: string): DiaryItem {
  return {
    id,
    text: itemText(item, fallback),
    project: itemProject(item),
    sourceRef: itemSourceRef(item),
  };
}

function collectTouchedProjects(items: DiaryItem[]): string[] {
  const seen = new Set<string>();
  for (const item of items) {
    if (item.project) seen.add(item.project);
  }
  return Array.from(seen).sort((a, b) => a.localeCompare(b));
}

function groupProgress(
  changes: DiaryItem[],
  decisions: DiaryItem[],
): DiaryProgressGroup[] {
  const groups = new Map<string, DiaryProgressGroup>();
  const ensure = (project: string | null) => {
    const key = project ?? "__unscoped__";
    const existing = groups.get(key);
    if (existing) return existing;
    const group = { project, items: [] };
    groups.set(key, group);
    return group;
  };

  for (const item of changes) ensure(item.project).items.push({ ...item, kind: "change" });
  for (const item of decisions) ensure(item.project).items.push({ ...item, kind: "decision" });
  return Array.from(groups.values()).sort((left, right) => {
    if (left.project === null && right.project !== null) return 1;
    if (left.project !== null && right.project === null) return -1;
    return (left.project ?? "").localeCompare(right.project ?? "");
  });
}

export function normalizeJournalEntry(entry: BrainJournalEntryResponse): NormalizedDiaryDay {
  const body = entry.body ?? {
    what_changed: [],
    decisions_observed: [],
    open_loops: [],
    notable_context: [],
    sources: [],
    tomorrow_watch: [],
  };
  const changes = asObjects(body.what_changed).map((item, index) =>
    normalizeObjectItem(item, `${entry.entry_id}:change:${index}`, "Aggiornamento senza titolo")
  );
  const observedDecisions = asStrings(body.decisions_observed).map((text, index) => ({
    id: `${entry.entry_id}:decision:${index}`,
    text,
    project: null,
    sourceRef: text,
  }));
  const openLoops = asObjects(body.open_loops).map((item, index) =>
    normalizeObjectItem(item, `${entry.entry_id}:open:${index}`, "Decisione senza titolo")
  );
  const context = asObjects(body.notable_context).map((item, index) =>
    normalizeObjectItem(item, `${entry.entry_id}:context:${index}`, "Contesto senza titolo")
  );
  const fallbackSummary = changes.slice(0, 2).map((item) => item.text).join(" ");
  const narrative = asString(entry.narrative_polished);
  const progressGroups = groupProgress(changes, observedDecisions);
  const progressItems = progressGroups.flatMap((group) => group.items);

  return {
    cycleKey: entry.cycle_key,
    entryId: entry.entry_id,
    runId: entry.run_id,
    narrative,
    baseSummary: fallbackSummary || null,
    narrativeFallback: !narrative,
    isEmpty: entry.is_empty || (changes.length + observedDecisions.length + openLoops.length + context.length === 0),
    decisions: openLoops,
    context,
    progressGroups,
    projectsTouched: collectTouchedProjects([...openLoops, ...context, ...progressItems]),
    counts: {
      decisions: openLoops.length,
      progress: progressItems.length,
      context: context.length,
      sources: body.sources?.length ?? 0,
      tomorrowWatch: body.tomorrow_watch?.length ?? 0,
    },
  };
}

export function selectDiaryLimitState(day: TimelineDay): DiaryLimitState {
  if (!day.run) return "not_run";
  if (!day.journal || day.journal.isEmpty) return "quiet";
  return "active";
}

export function deriveTimelineState(
  run: BrainRunResponse | null,
  journal: NormalizedDiaryDay | null,
): TimelineState {
  // Mapping richiesto dal brief: arancio = open_loops da decidere; verde =
  // ciclo con contenuto gestito; muted = ciclo eseguito ma journal vuoto;
  // tratteggio = nessun run del brain per quel giorno.
  if (!run) return "not_run";
  if (journal?.decisions.length) return "needs_decision";
  if (journal && !journal.isEmpty) return "managed";
  return "quiet";
}

function ymd(date: Date): string {
  const year = date.getFullYear();
  const month = String(date.getMonth() + 1).padStart(2, "0");
  const day = String(date.getDate()).padStart(2, "0");
  return `${year}-${month}-${day}`;
}

function addDays(cycleKey: string, offset: number): string {
  const date = new Date(`${cycleKey}T00:00:00`);
  date.setDate(date.getDate() + offset);
  return ymd(date);
}

function compareCycleKeyDesc(left: string, right: string): number {
  return right.localeCompare(left);
}

export function buildTimelineDays(
  runs: BrainRunResponse[],
  journals: NormalizedDiaryDay[],
  today: Date = new Date(),
): TimelineDay[] {
  const runsByDay = new Map<string, BrainRunResponse>();
  for (const run of runs) {
    const existing = runsByDay.get(run.cycle_key);
    if (!existing || existing.started_at.localeCompare(run.started_at) < 0) {
      runsByDay.set(run.cycle_key, run);
    }
  }

  const journalsByDay = new Map(journals.map((journal) => [journal.cycleKey, journal]));
  const cycleKeys = new Set<string>([ymd(today), ...runs.map((run) => run.cycle_key), ...journals.map((journal) => journal.cycleKey)]);
  const sorted = Array.from(cycleKeys).sort(compareCycleKeyDesc);
  const newest = sorted[0] ?? ymd(today);
  const oldest = sorted[sorted.length - 1] ?? newest;
  const filled: string[] = [];
  for (let cursor = newest; cursor >= oldest; cursor = addDays(cursor, -1)) {
    filled.push(cursor);
    if (cursor === oldest) break;
  }

  return filled.map((cycleKey) => {
    const run = runsByDay.get(cycleKey) ?? null;
    const journal = journalsByDay.get(cycleKey) ?? null;
    return {
      cycleKey,
      run,
      journal,
      state: deriveTimelineState(run, journal),
    };
  });
}

export function timelineStateClasses(state: TimelineState, active: boolean): string {
  const width = active ? "w-12" : "w-8";
  if (state === "needs_decision") return `${width} bg-pir-accent`;
  if (state === "managed") return `${width} bg-pir-success`;
  if (state === "quiet") return `${active ? "w-7" : "w-5"} bg-pir-border-strong`;
  return "w-3 border border-dashed border-pir-strong bg-transparent";
}

export function latestAvailableDayIndex(days: TimelineDay[], currentIndex: number): number | null {
  const afterCurrent = days.findIndex((day, index) => index !== currentIndex && day.run);
  return afterCurrent >= 0 ? afterCurrent : null;
}

function defaultProject(project: string | null): string | null {
  return project && project.trim() ? project : null;
}

export async function addDecisionToTodos(item: DiaryItem): Promise<TodoResponseLocal> {
  return createTodoLocal({
    text: item.text,
    type: "decidi",
    project: defaultProject(item.project),
    source: "brain",
    source_ref: item.sourceRef ?? item.id,
  });
}

export async function delegateDecisionToAgent(item: DiaryItem): Promise<TodoResponseLocal> {
  const todo = await createTodoLocal({
    text: item.text,
    type: "rivedi",
    project: defaultProject(item.project),
    source: "brain",
    source_ref: item.sourceRef ?? item.id,
    doer: "agent",
  });
  return delegateTodoLocal(todo.id, {
    title: item.text.slice(0, 200),
    project: defaultProject(item.project),
  });
}

export async function requestBrainRunNow(): Promise<BrainRunActionState> {
  try {
    await triggerBrainRun();
    return "started";
  } catch (error) {
    if (error instanceof APIError && error.status === 409) return "already_running";
    throw error;
  }
}
