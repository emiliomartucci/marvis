"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import {
  APIError,
  listBrainJournal,
  listBrainRuns,
  type BrainRunResponse,
} from "@/lib/api";
import {
  addDecisionToTodos,
  buildTimelineDays,
  delegateDecisionToAgent,
  latestAvailableDayIndex,
  normalizeJournalEntry,
  requestBrainRunNow,
  selectDiaryLimitState,
  timelineStateClasses,
  type DiaryItem,
  type DiaryProgressGroup,
  type NormalizedDiaryDay,
  type TimelineDay,
} from "@/lib/diario";
import { useT } from "@/lib/i18n";

type ActionStatus = "idle" | "adding" | "delegating" | "done" | "delegated" | "error";
type BrainRunStatus = "idle" | "polling" | "already_running" | "error";

function formatDayLabel(cycleKey: string, locale: string): string {
  return new Intl.DateTimeFormat(locale, {
    weekday: "long",
    day: "numeric",
    month: "long",
  }).format(new Date(`${cycleKey}T12:00:00`));
}

function formatMonthLabel(cycleKey: string, locale: string): string {
  return new Intl.DateTimeFormat(locale, {
    month: "long",
    year: "numeric",
  }).format(new Date(`${cycleKey}T12:00:00`));
}

function formatShortBoundary(cycleKey: string, locale: string): string {
  return new Intl.DateTimeFormat(locale, {
    month: "short",
    year: "2-digit",
  }).format(new Date(`${cycleKey}T12:00:00`));
}

function projectLabel(project: string | null, fallback: string): string {
  return project ?? fallback;
}

function isTerminalRun(run: BrainRunResponse | null): boolean {
  return Boolean(run && run.status !== "running");
}

function SectionHeading({
  title,
  count,
  tone = "neutral",
}: {
  title: string;
  count?: number;
  tone?: "neutral" | "accent" | "success";
}) {
  const toneClass = tone === "accent"
    ? "text-pir-accent"
    : tone === "success"
      ? "text-pir-success"
      : "text-pir-text-tertiary";
  return (
    <div className="flex items-center gap-2">
      <span className={`h-1.5 w-1.5 rounded-full ${tone === "accent" ? "bg-pir-accent" : tone === "success" ? "bg-pir-success" : "bg-pir-border-strong"}`} />
      <h2 className="font-mono text-caption uppercase text-pir-text-tertiary">
        {title}
      </h2>
      {count !== undefined && (
        <span className={`font-mono text-caption tabular-nums ${toneClass}`}>{count}</span>
      )}
    </div>
  );
}

function Snapshot({ day }: { day: NormalizedDiaryDay }) {
  const { t } = useT();
  return (
    <section className="border-y border-pir py-3">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="flex min-w-0 flex-wrap items-center gap-2">
          <span className="font-mono text-caption uppercase text-pir-text-muted">
            {t.diario.snapshot.projectsTouched}
          </span>
          {day.projectsTouched.length ? (
            day.projectsTouched.map((project) => (
              <span
                key={project}
                className="rounded border border-pir bg-pir-surface-1 px-2 py-1 font-mono text-caption text-pir-text-secondary"
              >
                {project}
              </span>
            ))
          ) : (
            <span className="text-caption text-pir-text-muted">{t.diario.snapshot.noProjects}</span>
          )}
        </div>
        <dl className="flex flex-wrap gap-3">
          <div className="flex items-baseline gap-1">
            <dt className="text-caption text-pir-text-muted">{t.diario.snapshot.decisions}</dt>
            <dd className="font-mono text-caption tabular-nums text-pir-accent">{day.counts.decisions}</dd>
          </div>
          <div className="flex items-baseline gap-1">
            <dt className="text-caption text-pir-text-muted">{t.diario.snapshot.progress}</dt>
            <dd className="font-mono text-caption tabular-nums text-pir-success">{day.counts.progress}</dd>
          </div>
          <div className="flex items-baseline gap-1">
            <dt className="text-caption text-pir-text-muted">{t.diario.snapshot.context}</dt>
            <dd className="font-mono text-caption tabular-nums text-pir-text-secondary">{day.counts.context}</dd>
          </div>
        </dl>
      </div>
    </section>
  );
}

function DecisionActions({
  item,
  status,
  onTodo,
  onDelegate,
}: {
  item: DiaryItem;
  status: ActionStatus;
  onTodo: (item: DiaryItem) => void;
  onDelegate: (item: DiaryItem) => void;
}) {
  const { t } = useT();
  const busy = status === "adding" || status === "delegating";
  if (status === "done") {
    return <span className="text-caption font-medium text-pir-success">{t.diario.actions.added}</span>;
  }
  if (status === "delegated") {
    return <span className="text-caption font-medium text-pir-success">{t.diario.actions.delegated}</span>;
  }
  return (
    <div className="flex flex-wrap gap-2">
      <button
        type="button"
        onClick={() => onTodo(item)}
        disabled={busy}
        className="rounded border border-pir-accent/40 bg-pir-accent/10 px-2.5 py-1 text-caption font-medium text-pir-accent transition-colors hover:bg-pir-accent/20 disabled:opacity-50"
      >
        {status === "adding" ? t.diario.actions.adding : t.diario.actions.addTodo}
      </button>
      <button
        type="button"
        onClick={() => onDelegate(item)}
        disabled={busy}
        className="rounded border border-pir bg-pir-surface-1 px-2.5 py-1 text-caption font-medium text-pir-text-secondary transition-colors hover:border-pir-success/40 hover:text-pir-success disabled:opacity-50"
      >
        {status === "delegating" ? t.diario.actions.delegating : t.diario.actions.delegate}
      </button>
      {status === "error" && <span className="text-caption text-pir-error">{t.diario.actions.error}</span>}
    </div>
  );
}

function DecisionsSection({
  items,
  actions,
  onTodo,
  onDelegate,
}: {
  items: DiaryItem[];
  actions: Record<string, ActionStatus>;
  onTodo: (item: DiaryItem) => void;
  onDelegate: (item: DiaryItem) => void;
}) {
  const { t } = useT();
  if (!items.length) return null;
  return (
    <section className="flex flex-col gap-3">
      <SectionHeading title={t.diario.sections.decisions} count={items.length} tone="accent" />
      <div className="flex flex-col divide-y divide-pir border-t border-pir">
        {items.map((item) => (
          <article key={item.id} className="grid gap-3 py-3 md:grid-cols-[1fr_auto] md:items-start">
            <div className="min-w-0">
              <p className="text-body text-pir-text-primary">{item.text}</p>
              <p className="mt-1 font-mono text-caption text-pir-text-muted">
                {projectLabel(item.project, t.diario.projectFallback)}
              </p>
            </div>
            <DecisionActions
              item={item}
              status={actions[item.id] ?? "idle"}
              onTodo={onTodo}
              onDelegate={onDelegate}
            />
          </article>
        ))}
      </div>
    </section>
  );
}

function ProgressSection({ groups }: { groups: DiaryProgressGroup[] }) {
  const { t } = useT();
  const count = groups.reduce((total, group) => total + group.items.length, 0);
  if (!count) return null;
  return (
    <section className="flex flex-col gap-3">
      <SectionHeading title={t.diario.sections.progress} count={count} tone="success" />
      <div className="flex flex-col gap-5">
        {groups.map((group) => (
          <article key={group.project ?? "unscoped"} className="flex flex-col gap-2">
            <h3 className="flex items-center gap-2 text-label font-semibold text-pir-success">
              <span className="h-2 w-2 rounded-full bg-pir-success" />
              {projectLabel(group.project, t.diario.projectFallback)}
            </h3>
            <ul className="flex flex-col divide-y divide-pir border-l border-pir pl-4">
              {group.items.map((item) => (
                <li key={item.id} className="py-2 text-body text-pir-text-secondary">
                  <span className={item.kind === "decision" ? "text-pir-success" : "text-pir-text-muted"}>
                    {item.kind === "decision" ? t.diario.progress.decisionPrefix : t.diario.progress.changePrefix}
                  </span>{" "}
                  {item.text}
                </li>
              ))}
            </ul>
          </article>
        ))}
      </div>
    </section>
  );
}

function ContextSection({ items }: { items: DiaryItem[] }) {
  const { t } = useT();
  if (!items.length) return null;
  return (
    <section className="flex flex-col gap-3">
      <SectionHeading title={t.diario.sections.context} count={items.length} />
      <ul className="flex flex-col divide-y divide-pir border-t border-pir">
        {items.map((item) => (
          <li key={item.id} className="py-3">
            <p className="text-body text-pir-text-secondary">{item.text}</p>
            <p className="mt-1 font-mono text-caption text-pir-text-muted">
              {projectLabel(item.project, t.diario.projectFallback)}
            </p>
          </li>
        ))}
      </ul>
    </section>
  );
}

function QuietState() {
  const { t } = useT();
  return (
    <section className="flex max-w-lg flex-col items-center gap-3 py-12 text-center">
      <div className="h-10 w-10 rounded-full border border-pir bg-pir-surface-1" />
      <h2 className="text-heading text-pir-text-primary">{t.diario.limit.quietTitle}</h2>
      <p className="text-body text-pir-text-tertiary">{t.diario.limit.quietBody}</p>
    </section>
  );
}

function NotRunState({
  onOpenLatest,
  onRunNow,
  runStatus,
  hasLatest,
}: {
  onOpenLatest: () => void;
  onRunNow: () => void;
  runStatus: BrainRunStatus;
  hasLatest: boolean;
}) {
  const { t } = useT();
  return (
    <section className="flex max-w-lg flex-col items-center gap-4 py-12 text-center">
      <div className="h-10 w-10 rounded-full border border-pir-warning/40 bg-pir-warning/10" />
      <div className="flex flex-col gap-2">
        <h2 className="text-heading text-pir-text-primary">{t.diario.limit.notRunTitle}</h2>
        <p className="text-body text-pir-text-tertiary">{t.diario.limit.notRunBody}</p>
      </div>
      <div className="flex flex-wrap justify-center gap-2">
        <button
          type="button"
          onClick={onOpenLatest}
          disabled={!hasLatest}
          className="rounded border border-pir bg-pir-surface-1 px-3 py-2 text-label text-pir-text-secondary transition-colors hover:border-pir-accent/40 hover:text-pir-text-primary disabled:opacity-40"
        >
          {t.diario.limit.openLatest}
        </button>
        <button
          type="button"
          onClick={onRunNow}
          disabled={runStatus === "polling"}
          className="rounded border border-pir-accent/40 bg-pir-accent px-3 py-2 text-label font-semibold text-pir-base transition-opacity hover:opacity-90 disabled:opacity-60"
        >
          {runStatus === "polling" ? t.diario.limit.runningNow : t.diario.limit.runNow}
        </button>
      </div>
      {runStatus === "polling" && (
        <p className="font-mono text-caption text-pir-accent">{t.diario.limit.polling}</p>
      )}
      {runStatus === "already_running" && (
        <p className="font-mono text-caption text-pir-warning">{t.diario.limit.alreadyRunning}</p>
      )}
      {runStatus === "error" && (
        <p className="font-mono text-caption text-pir-error">{t.diario.limit.runError}</p>
      )}
    </section>
  );
}

function TimelineSlider({
  days,
  selectedIndex,
  onSelect,
}: {
  days: TimelineDay[];
  selectedIndex: number;
  onSelect: (index: number) => void;
}) {
  const { t, locale } = useT();
  const trackRef = useRef<HTMLDivElement | null>(null);
  const selected = days[selectedIndex] ?? days[0];
  const month = selected ? formatMonthLabel(selected.cycleKey, locale) : "";

  const pick = useCallback((clientY: number) => {
    const rect = trackRef.current?.getBoundingClientRect();
    if (!rect || days.length <= 1) return;
    const ratio = Math.max(0, Math.min(1, (clientY - rect.top) / rect.height));
    onSelect(Math.round(ratio * (days.length - 1)));
  }, [days.length, onSelect]);

  return (
    <aside
      data-tour="cronologia"
      className="hidden w-[112px] shrink-0 flex-col border-l border-pir bg-pir-surface-0 md:flex"
    >
      <div className="border-b border-pir px-3 py-3 text-center">
        <div className="text-label font-semibold capitalize text-pir-text-primary">{month}</div>
      </div>
      <div
        ref={trackRef}
        role="group"
        aria-label={t.diario.timeline.label}
        onPointerDown={(event) => {
          event.currentTarget.setPointerCapture(event.pointerId);
          pick(event.clientY);
        }}
        onPointerMove={(event) => {
          if (event.buttons === 1) pick(event.clientY);
        }}
        onWheel={(event) => {
          event.preventDefault();
          const step = event.deltaY > 0 ? 1 : -1;
          onSelect(Math.max(0, Math.min(days.length - 1, selectedIndex + step)));
        }}
        className="min-h-0 flex-1 cursor-ns-resize touch-none select-none px-2 py-3"
      >
        <div className="flex h-full flex-col">
          {days.map((day, index) => {
            const active = index === selectedIndex;
            const previous = days[index - 1];
            const boundary = previous && previous.cycleKey.slice(0, 7) !== day.cycleKey.slice(0, 7);
            return (
              <button
                key={day.cycleKey}
                type="button"
                title={formatDayLabel(day.cycleKey, locale)}
                aria-label={formatDayLabel(day.cycleKey, locale)}
                aria-current={active ? "date" : undefined}
                onClick={() => onSelect(index)}
                className="relative flex min-h-[16px] flex-1 items-center justify-end gap-2 text-right"
              >
                {boundary && (
                  <span className="absolute left-0 top-0 flex w-full items-center gap-1">
                    <span className="h-px flex-1 bg-pir-border-strong" />
                    <span className="font-mono text-[8px] uppercase text-pir-text-muted">
                      {formatShortBoundary(day.cycleKey, locale)}
                    </span>
                  </span>
                )}
                {active && <span className="absolute left-0 right-0 h-px bg-pir-accent/45" />}
                <span className={`z-10 h-1.5 rounded-sm transition-all ${timelineStateClasses(day.state, active)}`} />
                <span className={`z-10 w-5 font-mono text-[10px] tabular-nums ${active ? "text-pir-accent" : "text-pir-text-muted"}`}>
                  {day.cycleKey.slice(8, 10)}
                </span>
              </button>
            );
          })}
        </div>
      </div>
      {selectedIndex !== 0 && (
        <button
          type="button"
          onClick={() => onSelect(0)}
          className="border-t border-pir px-2 py-2 text-caption font-semibold text-pir-accent hover:bg-pir-accent/10"
        >
          {t.diario.timeline.today}
        </button>
      )}
    </aside>
  );
}

function LoadingState() {
  const { t } = useT();
  return (
    <div className="flex flex-1 items-center justify-center bg-pir-base">
      <p className="font-mono text-caption uppercase text-pir-text-muted">{t.diario.loading}</p>
    </div>
  );
}

function ErrorState({ message }: { message: string }) {
  const { t } = useT();
  return (
    <div className="flex flex-1 items-center justify-center bg-pir-base p-6">
      <section className="max-w-md border border-pir bg-pir-surface-0 p-5">
        <h1 className="text-heading text-pir-text-primary">{t.diario.error.title}</h1>
        <p className="mt-2 text-body text-pir-text-secondary">{message}</p>
      </section>
    </div>
  );
}

export default function DiarioPage() {
  const { t, locale } = useT();
  const [days, setDays] = useState<TimelineDay[]>([]);
  const [selectedIndex, setSelectedIndex] = useState(0);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [actions, setActions] = useState<Record<string, ActionStatus>>({});
  const [runStatus, setRunStatus] = useState<BrainRunStatus>("idle");

  const load = useCallback(async (signal?: AbortSignal) => {
    const [runsResp, journalResp] = await Promise.all([
      listBrainRuns({ limit: 120, include_superseded: false }, { signal }),
      listBrainJournal({ scope_type: "company", scope_key: "__company__", limit: 120 }, { signal }),
    ]);
    const normalized = journalResp.items.map(normalizeJournalEntry);
    return buildTimelineDays(runsResp.items, normalized);
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    setLoading(true);
    load(controller.signal)
      .then((nextDays) => {
        setDays(nextDays);
        setSelectedIndex(0);
        setError(null);
      })
      .catch((err: unknown) => {
        if (err instanceof DOMException && err.name === "AbortError") return;
        const message = err instanceof APIError ? err.message : t.diario.error.body;
        setError(message);
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false);
      });
    return () => controller.abort();
  }, [load, t.diario.error.body]);

  useEffect(() => {
    if (runStatus !== "polling") return;
    const timer = window.setInterval(() => {
      load()
        .then((nextDays) => {
          setDays(nextDays);
          if (isTerminalRun(nextDays[0]?.run ?? null)) setRunStatus("idle");
        })
        .catch(() => setRunStatus("error"));
    }, 5000);
    return () => window.clearInterval(timer);
  }, [load, runStatus]);

  const selectedDay = days[selectedIndex] ?? null;
  const latestIndex = useMemo(
    () => (selectedDay ? latestAvailableDayIndex(days, selectedIndex) : null),
    [days, selectedDay, selectedIndex],
  );

  const handleAddTodo = useCallback((item: DiaryItem) => {
    setActions((current) => ({ ...current, [item.id]: "adding" }));
    addDecisionToTodos(item)
      .then(() => setActions((current) => ({ ...current, [item.id]: "done" })))
      .catch(() => setActions((current) => ({ ...current, [item.id]: "error" })));
  }, []);

  const handleDelegate = useCallback((item: DiaryItem) => {
    setActions((current) => ({ ...current, [item.id]: "delegating" }));
    delegateDecisionToAgent(item)
      .then(() => setActions((current) => ({ ...current, [item.id]: "delegated" })))
      .catch(() => setActions((current) => ({ ...current, [item.id]: "error" })));
  }, []);

  const handleRunNow = useCallback(() => {
    setRunStatus("polling");
    requestBrainRunNow()
      .then((state) => {
        if (state === "already_running") {
          setRunStatus("already_running");
          return;
        }
        setRunStatus("polling");
      })
      .catch(() => setRunStatus("error"));
  }, []);

  if (loading) return <LoadingState />;
  if (error) return <ErrorState message={error} />;

  const limitState = selectedDay ? selectDiaryLimitState(selectedDay) : "not_run";
  const journal = selectedDay?.journal ?? null;
  const dayLabel = selectedDay ? formatDayLabel(selectedDay.cycleKey, locale) : t.diario.todayFallback;

  return (
    <main className="flex min-h-0 flex-1 bg-pir-base text-pir-text-primary">
      <div className="min-w-0 flex-1 overflow-y-auto">
        <div className="mx-auto flex w-full max-w-[820px] flex-col gap-6 px-5 py-5 md:px-8 md:py-6">
          {selectedIndex > 0 && (
            <div className="flex items-center justify-between gap-3 border border-pir-accent/30 bg-pir-accent/10 px-3 py-2">
              <span className="text-caption text-pir-text-secondary">{t.diario.pastBanner}</span>
              <button
                type="button"
                onClick={() => setSelectedIndex(0)}
                className="text-caption font-semibold text-pir-accent"
              >
                {t.diario.timeline.today}
              </button>
            </div>
          )}

          <header className="flex flex-wrap items-baseline justify-between gap-3">
            <div>
              <p className="font-mono text-caption uppercase text-pir-text-muted">{t.appShell.nav.diario}</p>
              <h1 className="mt-1 text-display capitalize text-pir-text-primary">{dayLabel}</h1>
            </div>
            {selectedDay && (
              <span className="font-mono text-caption tabular-nums text-pir-text-muted">
                {selectedDay.cycleKey}
              </span>
            )}
          </header>

          {limitState === "not_run" && (
            <NotRunState
              runStatus={runStatus}
              hasLatest={latestIndex !== null}
              onOpenLatest={() => {
                if (latestIndex !== null) setSelectedIndex(latestIndex);
              }}
              onRunNow={handleRunNow}
            />
          )}

          {limitState === "quiet" && <QuietState />}

          {limitState === "active" && journal && (
            <>
              <section className="flex flex-col gap-2">
                <SectionHeading title={t.diario.sections.narrative} />
                <p className="max-w-2xl text-[15px] leading-7 text-pir-text-primary">
                  {journal.narrative ?? journal.baseSummary ?? t.diario.narrative.emptyFallback}
                </p>
                {journal.narrativeFallback && (
                  <p className="font-mono text-caption text-pir-text-muted">
                    {t.diario.narrative.fallbackCaption}
                  </p>
                )}
              </section>

              <Snapshot day={journal} />
              <DecisionsSection
                items={journal.decisions}
                actions={actions}
                onTodo={handleAddTodo}
                onDelegate={handleDelegate}
              />
              <ProgressSection groups={journal.progressGroups} />
              <ContextSection items={journal.context} />
            </>
          )}
        </div>
      </div>
      {days.length > 0 && (
        <TimelineSlider
          days={days}
          selectedIndex={selectedIndex}
          onSelect={setSelectedIndex}
        />
      )}
    </main>
  );
}
