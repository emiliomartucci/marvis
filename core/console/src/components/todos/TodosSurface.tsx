"use client";

import { useCallback, useEffect, useMemo, useState, type FormEvent } from "react";
import {
  APIError,
  applyVirtualTodoActionLocal,
  createTodoLocal,
  delegateTodoLocal,
  listTodosLocal,
  updateTodoLocal,
  type TodoResponseLocal,
} from "@/lib/api";
import { usePollingData } from "@/hooks/usePollingData";
import { useT } from "@/lib/i18n";
import { TODOS_CHANGED_EVENT, notifyTodosChanged } from "@/lib/todosEvents";
import { Drawer } from "@/components/ui/Drawer";
import {
  doerGlyph,
  groupTodosByHorizon,
  matchesTodoFilter,
  nextHorizonDate,
  openTodos,
  todoActionDefinitions,
  type TodoActionDefinition,
  type TodoActionId,
  type TodoFilter,
} from "./todoModel";

type TodosDictionary = ReturnType<typeof useT>["t"]["todos"];

const FILTERS: TodoFilter[] = ["all", "decisions", "promemoria", "idea", "brain"];

function actionToneClass(action: TodoActionDefinition): string {
  if (action.tone === "primary") {
    return "bg-pir-accent text-pir-base hover:bg-pir-accent/90";
  }
  if (action.tone === "danger") {
    return "border border-pir-error/50 text-pir-error hover:bg-pir-error/10";
  }
  return "border border-pir text-pir-text-secondary hover:border-pir-accent hover:text-pir-text-primary";
}

function formatDate(value: string, locale: string): string {
  const date = new Date(`${value.slice(0, 10)}T00:00:00`);
  if (Number.isNaN(date.getTime())) return value;
  return new Intl.DateTimeFormat(locale, { day: "2-digit", month: "short" }).format(date);
}

function textFromPayload(payload: Record<string, unknown> | null | undefined, key: string): string | null {
  const value = payload?.[key];
  return typeof value === "string" && value.trim() ? value : null;
}

function numberFromPayload(payload: Record<string, unknown> | null | undefined, key: string): string | null {
  const value = payload?.[key];
  return typeof value === "number" ? String(value) : null;
}

function payloadPreview(payload: Record<string, unknown> | null | undefined): string {
  if (!payload || Object.keys(payload).length === 0) return "";
  return JSON.stringify(payload, null, 2);
}

function captionForAction(action: TodoActionId, t: TodosDictionary): string {
  if (action === "complete") return t.captions.completed;
  if (action === "discard") return t.captions.discarded;
  if (action === "postpone") return t.captions.postponed;
  if (action === "delegate") return t.captions.delegated;
  if (action === "promote") return t.captions.promoted;
  if (action === "confirm") return t.captions.decided;
  if (action === "revise") return t.captions.revise;
  if (action === "approve") return t.captions.approved;
  if (action === "reject") return t.captions.rejected;
  if (action === "review") return t.captions.reviewing;
  return t.captions.feedback;
}

async function transitionTodo(todo: TodoResponseLocal, status: string): Promise<TodoResponseLocal> {
  if (
    todo.status === "aperto" &&
    (todo.type === "decidi" || todo.type === "rivedi") &&
    ["deciso", "fatto", "delegato", "scartato"].includes(status)
  ) {
    await updateTodoLocal(todo.id, { status: "in_revisione" });
  }
  return updateTodoLocal(todo.id, { status });
}

export function TodoActionBar({
  todo,
  onAction,
  t,
  compact = false,
}: {
  todo: TodoResponseLocal;
  onAction: (todo: TodoResponseLocal, action: TodoActionDefinition, feedback?: string) => void;
  t: TodosDictionary;
  compact?: boolean;
}) {
  const [feedbackAction, setFeedbackAction] = useState<TodoActionDefinition | null>(null);
  const [feedback, setFeedback] = useState("");
  const actions = todoActionDefinitions(todo);

  function submitAction(action: TodoActionDefinition) {
    if (action.needsFeedback) {
      setFeedbackAction(action);
      return;
    }
    onAction(todo, action);
  }

  return (
    <div className="flex flex-col gap-2" onClick={(event) => event.stopPropagation()}>
      {feedbackAction && (
        <div className="rounded border border-pir bg-pir-base p-2">
          <textarea
            value={feedback}
            onChange={(event) => setFeedback(event.target.value)}
            placeholder={t.drawer.feedbackPlaceholder}
            className="min-h-16 w-full resize-none rounded border border-pir bg-pir-surface-0 px-2 py-2 text-body text-pir-text-primary outline-none focus:border-pir-accent"
          />
          <div className="mt-2 flex justify-end gap-2">
            <button
              type="button"
              className="h-8 rounded border border-pir px-3 text-caption text-pir-text-tertiary hover:text-pir-text-primary"
              onClick={() => {
                setFeedbackAction(null);
                setFeedback("");
              }}
            >
              {t.cancel}
            </button>
            <button
              type="button"
              disabled={!feedback.trim()}
              className="h-8 rounded bg-pir-accent px-3 text-caption font-semibold text-pir-base disabled:cursor-not-allowed disabled:opacity-50"
              onClick={() => {
                onAction(todo, feedbackAction, feedback.trim());
                setFeedbackAction(null);
                setFeedback("");
              }}
            >
              {t.actions.sendFeedback}
            </button>
          </div>
        </div>
      )}
      <div className={`flex flex-wrap gap-2 ${compact ? "justify-start" : "justify-end"}`}>
        {actions.map((action) => (
          <button
            key={action.id}
            type="button"
            data-tour={action.id === "approve" ? "todo-approva" : undefined}
            onClick={() => submitAction(action)}
            className={`h-8 rounded px-3 text-caption font-semibold transition-colors ${actionToneClass(action)}`}
          >
            {t.actions[action.id]}
          </button>
        ))}
      </div>
    </div>
  );
}

function TodoProjectControl({
  todo,
  onReassign,
  t,
}: {
  todo: TodoResponseLocal;
  onReassign: (todo: TodoResponseLocal, project: string) => void;
  t: TodosDictionary;
}) {
  const [editing, setEditing] = useState(false);
  const [project, setProject] = useState("");

  if (todo.project) {
    return <span className="truncate font-mono text-[10px] text-pir-text-muted">{todo.project}</span>;
  }

  if (!editing) {
    return (
      <button
        type="button"
        className="font-mono text-[10px] text-pir-accent hover:text-pir-text-primary"
        onClick={(event) => {
          event.stopPropagation();
          setEditing(true);
        }}
      >
        {t.personal} · {t.assign}
      </button>
    );
  }

  return (
    <form
      className="flex min-w-[180px] items-center gap-1"
      onClick={(event) => event.stopPropagation()}
      onSubmit={(event) => {
        event.preventDefault();
        if (!project.trim()) return;
        onReassign(todo, project.trim());
        setEditing(false);
        setProject("");
      }}
    >
      <input
        value={project}
        onChange={(event) => setProject(event.target.value)}
        aria-label={t.assignProject}
        placeholder={t.assignProject}
        className="h-7 min-w-0 flex-1 rounded border border-pir bg-pir-base px-2 font-mono text-[10px] text-pir-text-primary outline-none focus:border-pir-accent"
      />
      <button type="submit" className="h-7 rounded bg-pir-accent px-2 text-[10px] font-semibold text-pir-base">
        {t.save}
      </button>
    </form>
  );
}

function TodoCard({
  todo,
  caption,
  onOpen,
  onAction,
  onReassign,
  t,
  locale,
}: {
  todo: TodoResponseLocal;
  caption?: string;
  onOpen: (todo: TodoResponseLocal) => void;
  onAction: (todo: TodoResponseLocal, action: TodoActionDefinition, feedback?: string) => void;
  onReassign: (todo: TodoResponseLocal, project: string) => void;
  t: TodosDictionary;
  locale: string;
}) {
  const doerLabel = todo.doer === "human" ? t.doerHuman : t.doerAgent;

  return (
    <article className="rounded border border-pir bg-pir-surface-0 p-3 text-pir-text-primary transition-colors hover:border-pir-accent">
      <button type="button" className="block w-full text-left" onClick={() => onOpen(todo)}>
        <div className="flex items-start justify-between gap-3">
          <div className="min-w-0">
            <div className="mb-1 flex flex-wrap items-center gap-2">
              <span className="rounded bg-pir-surface-2 px-1.5 py-0.5 font-mono text-[10px] uppercase text-pir-text-muted">
                {t.types[todo.type]}
              </span>
              {todo.virtual && (
                <span className="rounded border border-pir-accent px-1.5 py-0.5 font-mono text-[10px] uppercase text-pir-accent">
                  {t.originBadge}
                </span>
              )}
              {todo.origin && (
                <span className="rounded bg-pir-base px-1.5 py-0.5 font-mono text-[10px] text-pir-text-muted">
                  {todo.origin.kind}
                </span>
              )}
            </div>
            <h3 className="text-label font-semibold text-pir-text-primary">{todo.text}</h3>
          </div>
          <span className="shrink-0 rounded border border-pir bg-pir-base px-2 py-1 text-caption" title={doerLabel}>
            <span aria-hidden>{doerGlyph(todo)}</span>
          </span>
        </div>
        <div className="mt-3 flex flex-wrap items-center gap-3 text-caption text-pir-text-muted">
          <span className="font-mono">{formatDate(todo.fu, locale)}</span>
          <TodoProjectControl todo={todo} onReassign={onReassign} t={t} />
          <span className="font-mono">{t.sources[todo.source as keyof typeof t.sources] ?? todo.source}</span>
        </div>
      </button>
      <div className="mt-3 border-t border-pir pt-3">
        <TodoActionBar todo={todo} onAction={onAction} t={t} compact />
        {caption && (
          <p className="mt-2 font-mono text-[10px] text-pir-success" role="status">
            {caption}
          </p>
        )}
      </div>
    </article>
  );
}

function TodoDrawerBody({ todo, t }: { todo: TodoResponseLocal; t: TodosDictionary }) {
  const payload = todo.payload ?? {};
  const preview = payloadPreview(todo.payload);

  if (todo.origin?.kind === "task_review") {
    return (
      <section className="rounded border border-pir bg-pir-base p-3">
        <h3 className="font-mono text-caption uppercase text-pir-text-muted">{t.drawer.taskReview}</h3>
        <dl className="mt-3 space-y-2 text-body text-pir-text-secondary">
          <div className="flex justify-between gap-3">
            <dt className="text-pir-text-muted">{t.drawer.branch}</dt>
            <dd className="truncate font-mono">{textFromPayload(payload, "branch") ?? "-"}</dd>
          </div>
          <div className="flex justify-between gap-3">
            <dt className="text-pir-text-muted">{t.drawer.status}</dt>
            <dd className="font-mono">{textFromPayload(payload, "pr_status") ?? "-"}</dd>
          </div>
        </dl>
        <p className="mt-3 whitespace-pre-wrap text-body text-pir-text-secondary">
          {textFromPayload(payload, "description") ?? todo.text}
        </p>
      </section>
    );
  }

  if (todo.origin?.kind === "finding") {
    return (
      <section className="rounded border border-pir bg-pir-base p-3">
        <h3 className="font-mono text-caption uppercase text-pir-text-muted">{t.drawer.finding}</h3>
        <p className="mt-3 text-body text-pir-text-secondary">
          {textFromPayload(payload, "summary") ?? todo.text}
        </p>
        <div className="mt-3 flex flex-wrap gap-2 font-mono text-[10px] text-pir-text-muted">
          {textFromPayload(payload, "severity") && <span>{textFromPayload(payload, "severity")}</span>}
          {textFromPayload(payload, "approval_state") && <span>{textFromPayload(payload, "approval_state")}</span>}
          {numberFromPayload(payload, "confidence") && <span>{numberFromPayload(payload, "confidence")}</span>}
        </div>
      </section>
    );
  }

  if (todo.origin?.kind === "memory_op") {
    return (
      <section className="rounded border border-pir bg-pir-base p-3">
        <h3 className="font-mono text-caption uppercase text-pir-text-muted">{t.drawer.memoryOperation}</h3>
        <p className="mt-3 text-body text-pir-text-secondary">
          {textFromPayload(payload, "summary") ?? todo.text}
        </p>
        {payload.proposed_write !== undefined ? (
          <pre className="mt-3 max-h-56 overflow-auto rounded border border-pir bg-pir-surface-0 p-3 font-mono text-[10px] text-pir-text-secondary">
            {JSON.stringify(payload.proposed_write, null, 2)}
          </pre>
        ) : null}
      </section>
    );
  }

  return (
    <section className="rounded border border-pir bg-pir-base p-3">
      <h3 className="font-mono text-caption uppercase text-pir-text-muted">{t.drawer.contextTitle}</h3>
      <p className="mt-3 whitespace-pre-wrap text-body text-pir-text-secondary">{todo.text}</p>
      <h3 className="mt-5 font-mono text-caption uppercase text-pir-text-muted">{t.drawer.payloadTitle}</h3>
      {preview ? (
        <pre className="mt-2 max-h-56 overflow-auto rounded border border-pir bg-pir-surface-0 p-3 font-mono text-[10px] text-pir-text-secondary">
          {preview}
        </pre>
      ) : (
        <p className="mt-2 text-body text-pir-text-muted">{t.drawer.noPayload}</p>
      )}
    </section>
  );
}

function TodoDetailDrawer({
  todo,
  open,
  caption,
  onClose,
  onAction,
  t,
  locale,
}: {
  todo: TodoResponseLocal | null;
  open: boolean;
  caption?: string;
  onClose: () => void;
  onAction: (todo: TodoResponseLocal, action: TodoActionDefinition, feedback?: string) => void;
  t: TodosDictionary;
  locale: string;
}) {
  if (!todo) {
    return (
      <Drawer open={open} onClose={onClose} header={<span />}>
        <span />
      </Drawer>
    );
  }

  const header = (
    <div className="flex items-start justify-between gap-3">
      <div className="min-w-0">
        <p className="font-mono text-caption text-pir-text-muted">
          {t.types[todo.type]} · {formatDate(todo.fu, locale)} · {todo.project ?? t.personal} · {t.sources[todo.source as keyof typeof t.sources] ?? todo.source} · {doerGlyph(todo)}
        </p>
        <h2 id="todo-drawer-title" className="mt-1 text-heading text-pir-text-primary">
          {todo.text}
        </h2>
      </div>
      <button
        type="button"
        onClick={onClose}
        className="h-8 w-8 shrink-0 rounded border border-pir text-pir-text-muted hover:text-pir-text-primary"
        aria-label={t.drawer.close}
      >
        x
      </button>
    </div>
  );

  return (
    <Drawer
      open={open}
      onClose={onClose}
      titleId="todo-drawer-title"
      header={header}
      actions={<TodoActionBar todo={todo} onAction={onAction} t={t} />}
    >
      <div className="space-y-4">
        <section className="grid grid-cols-2 gap-2">
          {[
            [t.drawer.status, todo.status],
            [t.drawer.branch, todo.origin?.kind ?? "-"],
            [t.assignProject, todo.project ?? t.personal],
            [t.originBadge, todo.virtual ? t.originBadge : "-"],
          ].map(([label, value]) => (
            <div key={label} className="rounded border border-pir bg-pir-base p-2">
              <p className="font-mono text-[10px] uppercase text-pir-text-muted">{label}</p>
              <p className="mt-1 truncate text-caption text-pir-text-secondary">{value}</p>
            </div>
          ))}
        </section>
        <TodoDrawerBody todo={todo} t={t} />
        {caption && (
          <p className="font-mono text-caption text-pir-success" role="status">
            {caption}
          </p>
        )}
      </div>
    </Drawer>
  );
}

export function TodosSurface() {
  const { t: dictionary, locale } = useT();
  const t = dictionary.todos;
  const [filter, setFilter] = useState<TodoFilter>("all");
  const [capture, setCapture] = useState("");
  const [savingCapture, setSavingCapture] = useState(false);
  const [selectedTodo, setSelectedTodo] = useState<TodoResponseLocal | null>(null);
  const [captions, setCaptions] = useState<Record<string, string>>({});

  const fetchTodos = useCallback(
    (signal: AbortSignal) => listTodosLocal({ status: "aperto,in_revisione", limit: 500 }, { signal }),
    []
  );
  const { data, loading, error, refresh } = usePollingData(fetchTodos, {
    interval: 15_000,
    backoff: true,
    unchangedThreshold: 4,
  });

  useEffect(() => {
    function handleTodosChanged() {
      refresh();
    }
    window.addEventListener(TODOS_CHANGED_EVENT, handleTodosChanged);
    return () => window.removeEventListener(TODOS_CHANGED_EVENT, handleTodosChanged);
  }, [refresh]);

  const todos = useMemo(() => openTodos(data ?? []), [data]);
  const filteredTodos = useMemo(
    () => todos.filter((todo) => matchesTodoFilter(todo, filter)),
    [filter, todos]
  );
  const groups = useMemo(() => groupTodosByHorizon(filteredTodos), [filteredTodos]);

  function setCaption(todo: TodoResponseLocal, message: string) {
    setCaptions((current) => ({ ...current, [todo.id]: message }));
  }

  async function handleCaptureSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const text = capture.trim();
    if (!text || savingCapture) return;
    setSavingCapture(true);
    try {
      await createTodoLocal({ text });
      setCapture("");
      setCaptions((current) => ({ ...current, "__capture": t.captions.captured }));
      notifyTodosChanged();
      refresh();
    } catch {
      setCaptions((current) => ({ ...current, "__capture": t.captions.actionFailed }));
    } finally {
      setSavingCapture(false);
    }
  }

  async function handleReassign(todo: TodoResponseLocal, project: string) {
    try {
      await updateTodoLocal(todo.id, { project });
      setCaption(todo, t.captions.reassigned);
      notifyTodosChanged();
      refresh();
    } catch {
      setCaption(todo, t.captions.actionFailed);
    }
  }

  async function handleAction(todo: TodoResponseLocal, action: TodoActionDefinition, feedback?: string) {
    try {
      if (todo.virtual || todo.type === "approva") {
        await applyVirtualTodoActionLocal(todo, action.id === "approve" ? "approve" : "reject", { feedback });
      } else if (action.id === "complete") {
        await transitionTodo(todo, "fatto");
      } else if (action.id === "discard") {
        await transitionTodo(todo, "scartato");
      } else if (action.id === "postpone") {
        await updateTodoLocal(todo.id, { fu: nextHorizonDate(todo.fu) });
      } else if (action.id === "delegate") {
        await delegateTodoLocal(todo.id, { project: todo.project ?? undefined });
      } else if (action.id === "promote") {
        await updateTodoLocal(todo.id, { status: "promosso", project: todo.project ?? undefined });
      } else if (action.id === "confirm") {
        await transitionTodo(todo, "deciso");
      } else if (action.id === "review") {
        await updateTodoLocal(todo.id, { status: "in_revisione" });
      } else {
        await updateTodoLocal(todo.id, {
          status: "in_revisione",
          payload: { ...(todo.payload ?? {}), review_feedback: feedback ?? "" },
        });
      }
      setCaption(todo, captionForAction(action.id, t));
      notifyTodosChanged();
      refresh();
    } catch (error) {
      const requiresOperator = error instanceof APIError && (error.status === 401 || error.status === 403);
      setCaption(todo, requiresOperator ? t.captions.operatorSession : t.captions.actionFailed);
    }
  }

  return (
    <main className="flex min-h-0 flex-1 flex-col bg-pir-base text-pir-text-primary">
      <header className="shrink-0 border-b border-pir px-5 py-4">
        <div className="flex flex-wrap items-end justify-between gap-4">
          <div>
            <p className="font-mono text-caption uppercase text-pir-accent">{t.title}</p>
            <h1 className="mt-1 text-heading text-pir-text-primary">{t.title}</h1>
            <p className="mt-1 max-w-3xl text-body text-pir-text-secondary">{t.subtitle}</p>
          </div>
          <form
            data-tour="todo-capture"
            className="flex min-w-[320px] max-w-xl flex-1 gap-2"
            onSubmit={handleCaptureSubmit}
          >
            <input
              value={capture}
              onChange={(event) => setCapture(event.target.value)}
              placeholder={t.capturePlaceholder}
              aria-label={t.capturePlaceholder}
              className="h-10 min-w-0 flex-1 rounded border border-pir bg-pir-surface-0 px-3 text-body text-pir-text-primary outline-none placeholder:text-pir-text-muted focus:border-pir-accent"
            />
            <button
              type="submit"
              disabled={!capture.trim() || savingCapture}
              className="h-10 rounded bg-pir-accent px-4 text-label font-semibold text-pir-base disabled:cursor-not-allowed disabled:opacity-50"
            >
              {savingCapture ? t.captureSaving : t.captureSubmit}
            </button>
          </form>
        </div>
        {captions.__capture && (
          <p className="mt-3 font-mono text-caption text-pir-success" role="status">
            {captions.__capture}
          </p>
        )}
        <div data-tour="todo-type" className="mt-4 flex flex-wrap gap-2">
          {FILTERS.map((nextFilter) => {
            const active = filter === nextFilter;
            return (
              <button
                key={nextFilter}
                type="button"
                onClick={() => setFilter(nextFilter)}
                className={`h-8 rounded border px-3 text-caption font-semibold transition-colors ${
                  active
                    ? "border-pir-accent bg-pir-accent/10 text-pir-text-primary"
                    : "border-pir bg-pir-surface-0 text-pir-text-tertiary hover:text-pir-text-primary"
                }`}
              >
                {t.filters[nextFilter]}
              </button>
            );
          })}
        </div>
      </header>

      <div className="min-h-0 flex-1 overflow-auto px-5 py-4">
        {loading && <p className="font-mono text-caption text-pir-text-muted">{t.loading}</p>}
        {error && (
          <div className="rounded border border-pir-error/40 bg-pir-error/10 p-3 text-body text-pir-text-secondary">
            <p>{t.error}</p>
            <button type="button" className="mt-2 h-8 rounded border border-pir px-3 text-caption" onClick={refresh}>
              {t.retry}
            </button>
          </div>
        )}
        {!loading && !error && filteredTodos.length === 0 && (
          <div className="rounded border border-dashed border-pir p-8 text-center text-body text-pir-text-muted">
            {t.empty}
          </div>
        )}
        <div className="space-y-5">
          {groups.map((group) => (
            <section key={group.horizon} className={group.items.length === 0 ? "hidden" : ""}>
              <div className="mb-2 flex items-center gap-2">
                <h2 className="text-label font-semibold text-pir-text-primary">{t.horizons[group.horizon]}</h2>
                <span className="rounded bg-pir-surface-2 px-1.5 py-0.5 font-mono text-[10px] text-pir-text-muted">
                  {group.items.length}
                </span>
              </div>
              <div className="grid gap-3 xl:grid-cols-2">
                {group.items.map((todo) => (
                  <TodoCard
                    key={todo.id}
                    todo={todo}
                    caption={captions[todo.id]}
                    onOpen={setSelectedTodo}
                    onAction={handleAction}
                    onReassign={handleReassign}
                    t={t}
                    locale={locale}
                  />
                ))}
              </div>
            </section>
          ))}
        </div>
      </div>

      <TodoDetailDrawer
        todo={selectedTodo}
        open={selectedTodo !== null}
        caption={selectedTodo ? captions[selectedTodo.id] : undefined}
        onClose={() => setSelectedTodo(null)}
        onAction={handleAction}
        t={t}
        locale={locale}
      />
    </main>
  );
}
