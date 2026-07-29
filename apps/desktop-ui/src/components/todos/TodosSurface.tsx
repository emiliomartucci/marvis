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
  completedTodos,
  doerGlyph,
  groupTodosByHorizon,
  isHeuristicClassified,
  matchesTodoFilter,
  nextHorizonDate,
  openTodos,
  todoActionDefinitions,
  todoHorizon,
  todoRowActionDefinitions,
  todoRowControl,
  type TodoActionDefinition,
  type TodoActionId,
  type TodoFilter,
} from "./todoModel";

type TodosDictionary = ReturnType<typeof useT>["t"]["todos"];

// Decidi gate: the decision captured at confirm time, persisted into the todo
// payload so the backend writes a non-empty ADR (gh #29).
type TodoDecisionInput = { scelta: string; rationale: string; opzioni: string[] };

const FILTERS: TodoFilter[] = ["all", "decisions", "promemoria", "idea", "brain", "completati"];

// Gate dot tones per type (design mock views.js TodoRow: approva=accent,
// decidi=warning, rivedi=success; idea resolves via Promote/Discard → purple).
const GATE_DOT_COLOR: Record<string, string> = {
  approva: "hsl(var(--pir-accent))",
  decidi: "hsl(var(--pir-warning))",
  rivedi: "hsl(var(--pir-success))",
  idea: "hsl(var(--pir-purple))",
};

// Type badge tones (design mock views.js type map).
const TYPE_BADGE_CLASS: Record<string, string> = {
  promemoria: "bg-pir-surface-2 text-pir-text-tertiary",
  azione: "bg-pir-surface-2 text-pir-text-tertiary",
  idea: "bg-pir-purple/20 text-pir-purple",
  decidi: "bg-pir-warning/20 text-pir-warning",
  approva: "bg-pir-accent/15 text-pir-accent",
  rivedi: "bg-pir-success/15 text-pir-success",
};

function actionToneClass(action: TodoActionDefinition): string {
  if (action.tone === "primary") {
    return "bg-pir-accent text-pir-base hover:bg-pir-accent/90";
  }
  if (action.tone === "danger") {
    return "border border-pir-error/50 text-pir-error hover:bg-pir-error/10";
  }
  return "border border-pir text-pir-text-secondary hover:border-pir-accent hover:text-pir-text-primary";
}

// Inline link tone for row actions (design mock views.js rowActions:
// Conferma=green, Delega/Promuovi/Rivedi=accent, the rest muted).
function rowActionToneClass(action: TodoActionDefinition): string {
  if (action.id === "confirm") return "text-pir-success hover:brightness-110";
  if (action.tone === "primary") return "text-pir-accent hover:brightness-110";
  return "text-pir-text-tertiary hover:text-pir-text-primary";
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

async function transitionTodo(
  todo: TodoResponseLocal,
  status: string,
  extraPayload?: Record<string, unknown>,
): Promise<TodoResponseLocal> {
  if (
    todo.status === "aperto" &&
    (todo.type === "decidi" || todo.type === "rivedi") &&
    ["deciso", "fatto", "delegato", "scartato"].includes(status)
  ) {
    await updateTodoLocal(todo.id, { status: "in_revisione" });
  }
  // The decision payload must ride the SAME update that sets `deciso`: the
  // backend writes the ADR from `updates["payload"]` on that transition.
  const patch: { status: string; payload?: Record<string, unknown> } = { status };
  if (extraPayload) {
    patch.payload = { ...(todo.payload ?? {}), ...extraPayload };
  }
  return updateTodoLocal(todo.id, patch);
}

export function TodoActionBar({
  todo,
  onAction,
  t,
  variant = "drawer",
}: {
  todo: TodoResponseLocal;
  onAction: (
    todo: TodoResponseLocal,
    action: TodoActionDefinition,
    feedback?: string,
    decision?: TodoDecisionInput,
  ) => void;
  t: TodosDictionary;
  variant?: "drawer" | "row";
}) {
  const [feedbackAction, setFeedbackAction] = useState<TodoActionDefinition | null>(null);
  const [feedback, setFeedback] = useState("");
  const [decisionAction, setDecisionAction] = useState<TodoActionDefinition | null>(null);
  const [scelta, setScelta] = useState("");
  const [rationale, setRationale] = useState("");
  const [opzioni, setOpzioni] = useState("");
  const actions = variant === "row" ? todoRowActionDefinitions(todo) : todoActionDefinitions(todo);

  function resetDecision() {
    setDecisionAction(null);
    setScelta("");
    setRationale("");
    setOpzioni("");
  }

  function submitAction(action: TodoActionDefinition) {
    if (action.needsDecision) {
      setDecisionAction(action);
      return;
    }
    if (action.needsFeedback) {
      setFeedbackAction(action);
      return;
    }
    onAction(todo, action);
  }

  return (
    <div className="flex flex-col gap-2" onClick={(event) => event.stopPropagation()}>
      {decisionAction && (
        <div className={`rounded border border-pir bg-pir-base p-2 ${variant === "row" ? "min-w-[280px]" : ""}`}>
          <p className="mb-2 font-mono text-caption uppercase text-pir-text-muted">{t.decision.title}</p>
          <textarea
            value={scelta}
            onChange={(event) => setScelta(event.target.value)}
            placeholder={t.decision.sceltaPlaceholder}
            className="min-h-12 w-full resize-none rounded border border-pir bg-pir-surface-0 px-2 py-2 text-body text-pir-text-primary outline-none focus:border-pir-accent"
          />
          <textarea
            value={rationale}
            onChange={(event) => setRationale(event.target.value)}
            placeholder={t.decision.rationalePlaceholder}
            className="mt-2 min-h-12 w-full resize-none rounded border border-pir bg-pir-surface-0 px-2 py-2 text-body text-pir-text-primary outline-none focus:border-pir-accent"
          />
          <textarea
            value={opzioni}
            onChange={(event) => setOpzioni(event.target.value)}
            placeholder={t.decision.opzioniPlaceholder}
            className="mt-2 min-h-12 w-full resize-none rounded border border-pir bg-pir-surface-0 px-2 py-2 text-body text-pir-text-primary outline-none focus:border-pir-accent"
          />
          <div className="mt-2 flex justify-end gap-2">
            <button
              type="button"
              className="h-8 rounded border border-pir px-3 text-caption text-pir-text-tertiary hover:text-pir-text-primary"
              onClick={resetDecision}
            >
              {t.cancel}
            </button>
            <button
              type="button"
              disabled={!scelta.trim()}
              className="h-8 rounded bg-pir-success px-3 text-caption font-semibold text-white disabled:cursor-not-allowed disabled:opacity-50"
              onClick={() => {
                onAction(todo, decisionAction, undefined, {
                  scelta: scelta.trim(),
                  rationale: rationale.trim(),
                  opzioni: opzioni
                    .split("\n")
                    .map((line) => line.trim())
                    .filter(Boolean),
                });
                resetDecision();
              }}
            >
              {t.decision.submit}
            </button>
          </div>
        </div>
      )}
      {feedbackAction && (
        <div className={`rounded border border-pir bg-pir-base p-2 ${variant === "row" ? "min-w-[260px]" : ""}`}>
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
      <div className={`flex flex-wrap items-center justify-end ${variant === "row" ? "gap-3" : "gap-2"}`}>
        {actions.map((action) => {
          // In the row, actions are inline text links; solid + colored stays
          // reserved for the approva gate (design mock views.js rowActions).
          if (variant === "row" && action.id !== "approve") {
            return (
              <button
                key={action.id}
                type="button"
                onClick={() => submitAction(action)}
                className={`text-caption font-semibold underline decoration-1 underline-offset-2 transition-colors ${rowActionToneClass(action)}`}
              >
                {t.actions[action.id]}
              </button>
            );
          }
          return (
            <button
              key={action.id}
              type="button"
              data-tour={action.id === "approve" ? "todo-approva" : undefined}
              onClick={() => submitAction(action)}
              className={
                variant === "row"
                  ? "h-7 rounded bg-pir-success px-2.5 text-caption font-bold text-white transition-colors hover:bg-pir-success/90"
                  : `h-8 rounded px-3 text-caption font-semibold transition-colors ${actionToneClass(action)}`
              }
            >
              {t.actions[action.id]}
            </button>
          );
        })}
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

// FU chip: calendar glyph + relative label, red when late (design mock
// views.js FuChip: "in ritardo" / "oggi" / "domani", weekday+day otherwise).
function FuChip({ fu, t, locale }: { fu: string; t: TodosDictionary; locale: string }) {
  const horizon = todoHorizon(fu);
  const late = horizon === "overdue";
  let label: string;
  if (horizon === "overdue" || horizon === "today" || horizon === "tomorrow") {
    label = t.horizons[horizon].toLocaleLowerCase(locale);
  } else if (horizon === "week") {
    const date = new Date(`${fu.slice(0, 10)}T00:00:00`);
    label = Number.isNaN(date.getTime())
      ? fu
      : new Intl.DateTimeFormat(locale, { weekday: "short", day: "numeric" }).format(date);
  } else {
    label = formatDate(fu, locale);
  }

  return (
    <span
      className={`inline-flex shrink-0 items-center gap-1 text-[10.5px] ${late ? "font-semibold text-pir-error" : "text-pir-text-tertiary"}`}
    >
      {/* Lucide-style inline calendar — the design system bans emoji in the
          UI; the mock renders Icon name="calendar" at 11px. */}
      <svg
        aria-hidden
        width="11"
        height="11"
        viewBox="0 0 24 24"
        fill="none"
        stroke="currentColor"
        strokeWidth="2"
        strokeLinecap="round"
        strokeLinejoin="round"
      >
        <rect x="3" y="4" width="18" height="18" rx="2" ry="2" />
        <line x1="16" y1="2" x2="16" y2="6" />
        <line x1="8" y1="2" x2="8" y2="6" />
        <line x1="3" y1="10" x2="21" y2="10" />
      </svg>
      {label}
    </span>
  );
}

// Discreet footnote (gh #22): when a todo was typed by the heuristic fallback
// (no LLM key), say so quietly. Informational, not a warning — muted + small,
// with the fuller explanation in the native tooltip.
function HeuristicLabel({ t }: { t: TodosDictionary }) {
  return (
    <span
      className="text-caption text-pir-text-muted"
      title={t.heuristic.tooltip}
    >
      {t.heuristic.label}
    </span>
  );
}

// List row (design mock views.js TodoRow): checkbox or gate dot on the left,
// text + metadata in the middle, per-type inline actions on the right.
function TodoRow({
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
  onAction: (
    todo: TodoResponseLocal,
    action: TodoActionDefinition,
    feedback?: string,
    decision?: TodoDecisionInput,
  ) => void;
  onReassign: (todo: TodoResponseLocal, project: string) => void;
  t: TodosDictionary;
  locale: string;
}) {
  const doerLabel = todo.doer === "human" ? t.doerHuman : t.doerAgent;
  const control = todoRowControl(todo);
  const gateTitle = `${t.gateHint} (${t.types[todo.type]})`;

  return (
    <div className="flex items-start gap-3 border-t border-pir px-1.5 py-3 transition-colors hover:bg-pir-surface-0">
      {control === "checkbox" ? (
        <button
          type="button"
          onClick={() => onAction(todo, { id: "complete", tone: "primary" })}
          aria-label={t.actions.complete}
          title={t.actions.complete}
          className="mt-0.5 h-[19px] w-[19px] shrink-0 rounded-[5px] border border-pir-strong bg-transparent transition-colors hover:border-pir-accent"
        />
      ) : (
        <button
          type="button"
          onClick={() => onOpen(todo)}
          aria-label={gateTitle}
          title={gateTitle}
          className="mt-0.5 flex h-[19px] w-[19px] shrink-0 items-center justify-center"
        >
          <span
            aria-hidden
            className="h-[9px] w-[9px] rounded-full border-2 bg-transparent"
            style={{ borderColor: GATE_DOT_COLOR[todo.type] ?? "hsl(var(--pir-accent))" }}
          />
        </button>
      )}
      <div className="flex min-w-0 flex-1 flex-col gap-1.5">
        <button
          type="button"
          onClick={() => onOpen(todo)}
          className="w-fit text-left text-body leading-snug text-pir-text-primary"
        >
          {todo.text}
        </button>
        <div className="flex flex-wrap items-center gap-x-2.5 gap-y-1 text-caption text-pir-text-muted">
          <span
            className={`rounded-[3px] px-1.5 py-0.5 font-mono text-[10px] font-bold uppercase tracking-wide ${TYPE_BADGE_CLASS[todo.type] ?? "bg-pir-surface-2 text-pir-text-tertiary"}`}
          >
            {t.types[todo.type]}
          </span>
          <FuChip fu={todo.fu} t={t} locale={locale} />
          {todo.doer && (
            <span title={doerLabel} aria-label={doerLabel} className="shrink-0 text-[11px]">
              <span aria-hidden>{doerGlyph(todo)}</span>
            </span>
          )}
          <TodoProjectControl todo={todo} onReassign={onReassign} t={t} />
          <span className="text-[10.5px]">{t.sources[todo.source as keyof typeof t.sources] ?? todo.source}</span>
          {todo.virtual && (
            <span className="rounded border border-pir-accent px-1.5 py-0.5 font-mono text-[10px] uppercase text-pir-accent">
              {t.originBadge}
            </span>
          )}
          {todo.origin && (
            <span className="rounded bg-pir-base px-1.5 py-0.5 font-mono text-[10px]">{todo.origin.kind}</span>
          )}
          {isHeuristicClassified(todo) && <HeuristicLabel t={t} />}
        </div>
        {caption && (
          <p className="font-mono text-[10px] text-pir-success" role="status">
            {caption}
          </p>
        )}
      </div>
      <div className="shrink-0 pt-0.5">
        <TodoActionBar todo={todo} onAction={onAction} t={t} variant="row" />
      </div>
    </div>
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
      {isHeuristicClassified(todo) && (
        <p className="mt-3 text-caption text-pir-text-muted" title={t.heuristic.tooltip}>
          {t.heuristic.drawerNote}
        </p>
      )}
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
  onAction: (
    todo: TodoResponseLocal,
    action: TodoActionDefinition,
    feedback?: string,
    decision?: TodoDecisionInput,
  ) => void;
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

// Read-only row for the "Completed" view (gh #34): a finished item has no
// actions — just a checked control, muted text, its type badge and the date.
function CompletedRow({
  todo,
  t,
  locale,
}: {
  todo: TodoResponseLocal;
  t: TodosDictionary;
  locale: string;
}) {
  return (
    <div className="flex items-start gap-3 border-t border-pir px-1.5 py-3">
      <span
        aria-hidden
        className="mt-0.5 flex h-[19px] w-[19px] shrink-0 items-center justify-center rounded-[5px] border border-pir-success/60 bg-pir-success/15 text-pir-success"
      >
        <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="3" strokeLinecap="round" strokeLinejoin="round">
          <polyline points="20 6 9 17 4 12" />
        </svg>
      </span>
      <div className="flex min-w-0 flex-1 flex-col gap-1.5">
        <span className="text-body leading-snug text-pir-text-tertiary line-through">{todo.text}</span>
        <div className="flex flex-wrap items-center gap-x-2.5 gap-y-1 text-caption text-pir-text-muted">
          <span
            className={`rounded-[3px] px-1.5 py-0.5 font-mono text-[10px] font-bold uppercase tracking-wide ${TYPE_BADGE_CLASS[todo.type] ?? "bg-pir-surface-2 text-pir-text-tertiary"}`}
          >
            {t.types[todo.type]}
          </span>
          {todo.project && (
            <span className="truncate font-mono text-[10px] text-pir-text-muted">{todo.project}</span>
          )}
          <span className="text-[10.5px]">{formatDate(todo.updated_at, locale)}</span>
        </div>
      </div>
    </div>
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

  // The "Completed" view (gh #34) fetches a different status set — finished
  // items otherwise never load (the open list only requests aperto/in_revisione).
  const completedView = filter === "completati";
  const fetchTodos = useCallback(
    (signal: AbortSignal) =>
      completedView
        ? listTodosLocal({ status: "fatto", limit: 50 }, { signal })
        : listTodosLocal({ status: "aperto,in_revisione", limit: 500 }, { signal }),
    [completedView]
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
  const completed = useMemo(() => completedTodos(data ?? []), [data]);

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

  async function handleAction(
    todo: TodoResponseLocal,
    action: TodoActionDefinition,
    feedback?: string,
    decision?: TodoDecisionInput,
  ) {
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
        // Persist the decision into the payload so the backend writes a full
        // ADR (gh #29). `domanda` falls back to the todo text when the
        // classification left it empty, so the ADR context is never generic.
        await transitionTodo(
          todo,
          "deciso",
          decision
            ? {
                domanda: textFromPayload(todo.payload, "domanda") ?? todo.text,
                scelta: decision.scelta,
                rationale: decision.rationale,
                opzioni: decision.opzioni,
              }
            : undefined,
        );
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
        {!loading && !error && (completedView ? completed.length === 0 : filteredTodos.length === 0) && (
          <div className="rounded border border-dashed border-pir p-8 text-center text-body text-pir-text-muted">
            {t.empty}
          </div>
        )}
        {completedView ? (
          <div className="max-w-3xl">
            {completed.map((todo) => (
              <CompletedRow key={todo.id} todo={todo} t={t} locale={locale} />
            ))}
          </div>
        ) : (
        <div className="max-w-3xl space-y-5">
          {groups.map((group) => {
            const late = group.horizon === "overdue";
            return (
              <section key={group.horizon} className={group.items.length === 0 ? "hidden" : ""}>
                <div className="mb-1 flex items-center gap-2">
                  <h2 className={`text-label font-bold ${late ? "text-pir-error" : "text-pir-text-primary"}`}>
                    {t.horizons[group.horizon]}
                  </h2>
                  <span
                    className={`rounded px-1.5 py-0.5 font-mono text-[10px] ${late ? "bg-pir-error/15 text-pir-error" : "bg-pir-surface-2 text-pir-text-muted"}`}
                  >
                    {group.items.length}
                  </span>
                </div>
                <div>
                  {group.items.map((todo) => (
                    <TodoRow
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
            );
          })}
        </div>
        )}
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
