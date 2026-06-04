// v1.7.0 - 2026-04-24 - Memoize session cards + merge-stable refresh (jank fix with many sessions)
"use client";

import { memo, useCallback, useEffect, useRef, useState } from "react";
import {
  DndContext,
  closestCenter,
  PointerSensor,
  useSensor,
  useSensors,
  type DragEndEvent,
} from "@dnd-kit/core";
import {
  SortableContext,
  useSortable,
  verticalListSortingStrategy,
} from "@dnd-kit/sortable";
import { CSS } from "@dnd-kit/utilities";
import {
  listSessions,
  deleteSession,
  completeSession,
  updateSession,
  reorderSessions,
  resurrectSession,
  hibernateSession,
  resumeSession,
  restartSession,
  getCostsSummary,
} from "@/lib/api";
import type { Session, ProjectCostSummary } from "@/lib/types";
import CreateSessionModal from "./CreateSessionModal";
import ProjectSelectorModal from "./ProjectSelectorModal";
import { PermissionGate } from "@/components/PermissionGate";
import { useDesignV2 } from "@/lib/useDesignV2";
import { L5Loader } from "@/components/ui/L5Loader";

export const SESSION_REFRESH_BASE_INTERVAL_MS = 15_000;
export const SESSION_REFRESH_JITTER_MS = 3_000;

export function nextSessionRefreshDelayMs(random = Math.random): number {
  return SESSION_REFRESH_BASE_INTERVAL_MS + Math.floor(random() * SESSION_REFRESH_JITTER_MS);
}

// --- Icons as inline SVGs ---

function PinIcon({ filled }: { filled: boolean }) {
  return (
    <svg width="14" height="14" viewBox="0 0 16 16" fill="none">
      <path
        d="M10 1L6 5L2 6L5.5 9.5L3 14L7 10.5L10 14L11 10L15 6L10 1Z"
        fill={filled ? "currentColor" : "none"}
        stroke="currentColor"
        strokeWidth="1.2"
        strokeLinejoin="round"
      />
    </svg>
  );
}

function GripIcon() {
  return (
    <svg width="12" height="12" viewBox="0 0 12 12" fill="currentColor" opacity="0.4">
      <circle cx="4" cy="3" r="1" />
      <circle cx="8" cy="3" r="1" />
      <circle cx="4" cy="6" r="1" />
      <circle cx="8" cy="6" r="1" />
      <circle cx="4" cy="9" r="1" />
      <circle cx="8" cy="9" r="1" />
    </svg>
  );
}

function ChevronIcon({ collapsed }: { collapsed: boolean }) {
  return (
    <svg
      width="12"
      height="12"
      viewBox="0 0 12 12"
      fill="none"
      className={`transition-transform ${collapsed ? "-rotate-90" : ""}`}
    >
      <path d="M3 4.5L6 7.5L9 4.5" stroke="currentColor" strokeWidth="1.2" strokeLinecap="round" />
    </svg>
  );
}

function ResurrectIcon() {
  return (
    <svg width="14" height="14" viewBox="0 0 16 16" fill="none">
      <path d="M2 8a6 6 0 0111.5-2.3M14 8a6 6 0 01-11.5 2.3" stroke="currentColor" strokeWidth="1.3" strokeLinecap="round" />
      <path d="M14 2v4h-4" stroke="currentColor" strokeWidth="1.3" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  );
}

// --- Status helpers ---

function statusColor(session: Session, isOpen: boolean): string {
  if (session.hibernated) return "bg-pir-warning";
  // Working: green pulsating (Claude is processing/running commands)
  if (session.activity_state === "working") return "bg-pir-success animate-working";
  // Needs input: orange pulsating (waiting for user)
  if (session.activity_state === "needs_input") return "bg-amber-500 animate-input";
  // Idle: grey dead (finished, sitting there)
  if (session.activity_state === "idle") return "bg-pir-text-muted";
  // Fallbacks for sessions without activity tracking
  if (session.attached) return "bg-pir-success";
  if (isOpen) return "bg-pir-accent";
  const CLI_PROCESSES = ["claude", "node", "gemini", "codex", "opencode"];
  if (session.status && CLI_PROCESSES.includes(session.status)) return "bg-pir-success";
  return "bg-pir-text-muted";
}

const PROVIDER_BADGES: Record<Exclude<Session["provider"], "claude">, string> = {
  gemini: "G",
  codex: "X",
  opencode: "O",
};

function statusLabel(session: Session): string | null {
  if (session.activity_state === "needs_input") return "input";
  if (session.activity_state === "working") return null;
  if (!session.status || session.status === "bash" || session.status === "zsh") return null;
  return session.status;
}

// --- Metric helpers ---

function formatTimer(seconds: number): string {
  if (seconds < 60) return "<1m";
  const hours = Math.floor(seconds / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  if (hours > 0) return `${hours}h ${minutes}m`;
  return `${minutes}m`;
}

// PR2: compact token formatter — 1.2k / 45.6k / 104k / 1.5M.
function formatTokens(n: number | null | undefined): string {
  if (n == null) return "—";
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`;
  if (n >= 100_000) return `${Math.round(n / 1000)}k`;
  if (n >= 1000) return `${(n / 1000).toFixed(1)}k`;
  return `${n}`;
}

// PR2: dual cost formatter. Collapses to single value when equal (within 1¢).
function formatCostDual(
  conv: number | null | undefined,
  session: number | null | undefined,
): string {
  if (conv == null && session == null) return "—";
  if (conv == null) return session == null ? "—" : `$${session.toFixed(1)}`;
  if (session == null) return `$${conv.toFixed(2)}`;
  if (Math.abs(conv - session) < 0.01) return `$${conv.toFixed(2)}`;
  return `$${conv.toFixed(1)}/${session.toFixed(1)}`;
}

// PR4: shadow cost badge. Returns null when equivalent is unknown or equals
// real within 1¢ (Claude sessions or already-paid OpenCode). When equivalent
// exceeds real (OAuth/free sessions), return "est $X" for the sublabel.
function formatCostEquivalentBadge(
  real: number | null | undefined,
  equivalent: number | null | undefined,
): string | null {
  if (equivalent == null) return null;
  if (real != null && Math.abs(equivalent - real) <= 0.01) return null;
  if (real != null && equivalent < real) return null;
  const fmt = equivalent < 1 ? equivalent.toFixed(2) : equivalent.toFixed(1);
  return `est $${fmt}`;
}

// PR2: dual context-pct. Scaled=null (OpenCode) renders `real%/—`.
function formatCtxDual(
  real: number | null | undefined,
  scaled: number | null | undefined,
): string {
  if (real == null && scaled == null) return "—";
  const r = real != null ? `${Math.round(real)}%` : "—";
  const s = scaled != null ? `${Math.round(scaled)}%` : "—";
  if (r === s) return r;
  return `${r}/${s}`;
}

// PR2: staleness check — metrics older than 1h fade to 0.5 opacity.
function isMetricsStale(refreshedAt: string | null | undefined): boolean {
  if (!refreshedAt) return false;
  const d = Date.parse(refreshedAt);
  if (Number.isNaN(d)) return false;
  return Date.now() - d > 3_600_000;
}

function cpuColor(pct: number): string {
  if (pct > 15) return "text-pir-error";
  if (pct > 5) return "text-pir-warning";
  return "text-pir-text-tertiary";
}

function ramColor(mb: number): string {
  if (mb > 1024) return "text-pir-error";
  if (mb > 500) return "text-pir-warning";
  return "text-pir-text-tertiary";
}

const MODEL_SHORT: Record<string, string> = {
  opus: "opus",
  sonnet: "snnt",
  haiku: "haiku",
};

function shortModel(model: string | null): string {
  if (!model) return "";
  const normalized = model
    .split("/")
    .at(-1)!
    .replace(/\[1m\]/g, "")
    .replace(/^(claude|gemini|gpt)-/, "")
    .replace(/-\d[\d.-]*$/, "");
  // Exact match first, then prefix match
  if (MODEL_SHORT[normalized]) return MODEL_SHORT[normalized];
  for (const [prefix, short] of Object.entries(MODEL_SHORT)) {
    if (normalized.startsWith(prefix)) return short;
  }
  return normalized.slice(0, 6);
}

function hasOneMillionContext(model: string | null): boolean {
  if (!model) return false;
  if (model.includes("[1m]")) return true;
  const normalized = model.split("/").at(-1) || model;
  return [
    "claude-sonnet-4-6",
    "claude-opus-4-6",
    "gpt-5.5",
    "gpt-5.4",
    "gemini-2.5-pro",
    "gemini-2.5-flash",
    "gemini-3.1-pro-preview",
  ].some((prefix) => normalized.startsWith(prefix));
}

// --- Micro icons ---
function CpuIcon() {
  return (
    <svg width="9" height="9" viewBox="0 0 10 10" fill="none" className="shrink-0 opacity-60">
      <rect x="2.5" y="2.5" width="5" height="5" rx="0.8" stroke="currentColor" strokeWidth="1"/>
      <line x1="1" y1="3.5" x2="2.5" y2="3.5" stroke="currentColor" strokeWidth="1" strokeLinecap="round"/>
      <line x1="1" y1="6.5" x2="2.5" y2="6.5" stroke="currentColor" strokeWidth="1" strokeLinecap="round"/>
      <line x1="7.5" y1="3.5" x2="9" y2="3.5" stroke="currentColor" strokeWidth="1" strokeLinecap="round"/>
      <line x1="7.5" y1="6.5" x2="9" y2="6.5" stroke="currentColor" strokeWidth="1" strokeLinecap="round"/>
      <line x1="3.5" y1="1" x2="3.5" y2="2.5" stroke="currentColor" strokeWidth="1" strokeLinecap="round"/>
      <line x1="6.5" y1="1" x2="6.5" y2="2.5" stroke="currentColor" strokeWidth="1" strokeLinecap="round"/>
      <line x1="3.5" y1="7.5" x2="3.5" y2="9" stroke="currentColor" strokeWidth="1" strokeLinecap="round"/>
      <line x1="6.5" y1="7.5" x2="6.5" y2="9" stroke="currentColor" strokeWidth="1" strokeLinecap="round"/>
    </svg>
  );
}

function RamIcon() {
  return (
    <svg width="9" height="9" viewBox="0 0 10 10" fill="none" className="shrink-0 opacity-60">
      <rect x="0.8" y="2.5" width="8.4" height="4.5" rx="0.8" stroke="currentColor" strokeWidth="1"/>
      <line x1="3" y1="4" x2="3" y2="5.5" stroke="currentColor" strokeWidth="1" strokeLinecap="round"/>
      <line x1="5" y1="4" x2="5" y2="5.5" stroke="currentColor" strokeWidth="1" strokeLinecap="round"/>
      <line x1="7" y1="4" x2="7" y2="5.5" stroke="currentColor" strokeWidth="1" strokeLinecap="round"/>
      <line x1="3" y1="7" x2="3" y2="8" stroke="currentColor" strokeWidth="1" strokeLinecap="round"/>
      <line x1="7" y1="7" x2="7" y2="8" stroke="currentColor" strokeWidth="1" strokeLinecap="round"/>
    </svg>
  );
}

function ClockIcon() {
  return (
    <svg width="9" height="9" viewBox="0 0 10 10" fill="none" className="shrink-0 opacity-60">
      <circle cx="5" cy="5" r="3.8" stroke="currentColor" strokeWidth="1"/>
      <path d="M5 3v2l1.2 1.2" stroke="currentColor" strokeWidth="1" strokeLinecap="round" strokeLinejoin="round"/>
    </svg>
  );
}

function WorkIcon() {
  return (
    <svg width="9" height="9" viewBox="0 0 10 10" fill="none" className="shrink-0 opacity-60">
      <path d="M5 1.5v3l1.8 1.8" stroke="currentColor" strokeWidth="1" strokeLinecap="round" strokeLinejoin="round"/>
      <path d="M2 8.5C2.8 7.2 3.8 6.5 5 6.5s2.2.7 3 2" stroke="currentColor" strokeWidth="1" strokeLinecap="round"/>
      <circle cx="5" cy="5" r="3.8" stroke="currentColor" strokeWidth="1"/>
    </svg>
  );
}

// --- Merge-stable refresh -----------------------------------------------
// The /sessions endpoint is server-cached (15s TTL) and the sidebar polls it
// regularly. When the payload is unchanged, preserve prior session object
// references so React.memo on card components short-circuits re-renders.
// Without this, every poll produces a brand-new array of brand-new objects
// and the sidebar re-renders all N rows (dnd-kit handles, tooltips, etc.)
// even when nothing changed — visible jank with 15+ sessions.
function sessionShallowEqual(a: Session, b: Session): boolean {
  if (a === b) return true;
  const aKeys = Object.keys(a) as (keyof Session)[];
  const bKeys = Object.keys(b) as (keyof Session)[];
  if (aKeys.length !== bKeys.length) return false;
  for (const k of aKeys) {
    if (a[k] !== b[k]) return false;
  }
  return true;
}

function mergeStableSessions(prev: Session[], next: Session[]): Session[] {
  if (prev.length !== next.length) {
    // Length changed — shape diff; still preserve refs where content matches.
    const prevByName = new Map(prev.map((s) => [s.name, s]));
    return next.map((n) => {
      const p = prevByName.get(n.name);
      return p && sessionShallowEqual(p, n) ? p : n;
    });
  }
  const prevByName = new Map(prev.map((s) => [s.name, s]));
  let allSame = true;
  const merged = next.map((n) => {
    const p = prevByName.get(n.name);
    if (p && sessionShallowEqual(p, n)) return p;
    allSame = false;
    return n;
  });
  return allSame ? prev : merged;
}

// --- Shared hook: actions context menu (flip + outside-click + ESC close) ---

function useActionsMenu() {
  const [showMenu, setShowMenu] = useState(false);
  const [menuFlip, setMenuFlip] = useState(false);
  const menuRef = useRef<HTMLDivElement>(null);
  const triggerRef = useRef<HTMLButtonElement>(null);

  // Flip menu above button if it overflows viewport
  useEffect(() => {
    if (!showMenu || !menuRef.current) return;
    const rect = menuRef.current.getBoundingClientRect();
    setMenuFlip(rect.bottom > window.innerHeight - 8);
  }, [showMenu]);

  // Close on outside click (pointerdown covers mouse + touch) or ESC
  useEffect(() => {
    if (!showMenu) return;
    function handlePointer(e: PointerEvent) {
      const target = e.target as Node;
      const inMenu = menuRef.current?.contains(target) ?? false;
      const inTrigger = triggerRef.current?.contains(target) ?? false;
      if (!inMenu && !inTrigger) {
        setShowMenu(false);
      }
    }
    function handleKey(e: KeyboardEvent) {
      if (e.key === "Escape") setShowMenu(false);
    }
    document.addEventListener("pointerdown", handlePointer, true);
    document.addEventListener("keydown", handleKey);
    return () => {
      document.removeEventListener("pointerdown", handlePointer, true);
      document.removeEventListener("keydown", handleKey);
    };
  }, [showMenu]);

  return { showMenu, setShowMenu, menuFlip, menuRef, triggerRef };
}

// --- Shared actions menu dropdown (used by V1 SortableSession + V2 SessionCardV2) ---

interface SessionActionsMenuProps {
  session: Session;
  menuRef: React.RefObject<HTMLDivElement | null>;
  menuFlip: boolean;
  close: () => void;
  onStartEdit: (name: string) => void;
  onEditDescription: (name: string) => void;
  onTogglePin: (name: string, pinned: boolean) => void;
  onSetGroup: (name: string) => void;
  onToggleAgentManaged: (name: string, managed: boolean) => void;
  onResume: (name: string) => void;
  onHibernate: (name: string) => void;
  onRestart: (name: string) => void;
  onComplete: (name: string) => void;
  onDelete: (name: string) => void;
}

function SessionActionsMenu({
  session,
  menuRef,
  menuFlip,
  close,
  onStartEdit,
  onEditDescription,
  onTogglePin,
  onSetGroup,
  onToggleAgentManaged,
  onResume,
  onHibernate,
  onRestart,
  onComplete,
  onDelete,
}: SessionActionsMenuProps) {
  const itemClass = "w-full text-left px-3 py-1.5 text-xs hover:bg-pir-surface-1 flex items-center gap-2";
  const fire = (fn: () => void) => (e: React.MouseEvent) => {
    e.stopPropagation();
    close();
    fn();
  };
  return (
    <div
      ref={menuRef}
      className={`absolute right-1.5 z-50 bg-pir-surface-0 border border-pir rounded shadow-lg py-1 min-w-[140px] ${menuFlip ? "bottom-full mb-1" : "top-full mt-1"}`}
    >
      <button type="button" className={itemClass} onClick={fire(() => onStartEdit(session.name))}>
        Rename
      </button>
      <button type="button" className={itemClass} onClick={fire(() => onEditDescription(session.name))}>
        Description
      </button>
      <button type="button" className={itemClass} onClick={fire(() => onTogglePin(session.name, !session.pinned))}>
        {session.pinned ? "Unpin" : "Pin"}
      </button>
      <button type="button" className={itemClass} onClick={fire(() => onSetGroup(session.name))}>
        Set Project...
      </button>
      <PermissionGate minRole="operator">
        <button
          type="button"
          className={`${itemClass} ${session.agent_managed ? "text-pir-accent" : ""}`}
          onClick={fire(() => onToggleAgentManaged(session.name, !session.agent_managed))}
        >
          {session.agent_managed ? "Disable Agent Mgmt" : "Enable Agent Mgmt"}
        </button>
      </PermissionGate>
      <div className="border-t border-pir my-1" />
      {session.hibernated ? (
        <button
          type="button"
          className={`${itemClass} text-pir-success`}
          onClick={fire(() => onResume(session.name))}
        >
          {session.provider === "claude" ? "Resume" : "Restart"}
        </button>
      ) : (
        <button type="button" className={itemClass} onClick={fire(() => onHibernate(session.name))}>
          Hibernate
        </button>
      )}
      <button type="button" className={itemClass} onClick={fire(() => onRestart(session.name))}>
        Restart
      </button>
      <div className="border-t border-pir my-1" />
      <button
        type="button"
        className={`${itemClass} text-green-400`}
        onClick={fire(() => onComplete(session.name))}
      >
        Complete Session
      </button>
      <PermissionGate minRole="operator">
        <button
          type="button"
          className={`${itemClass} text-pir-error`}
          onClick={fire(() => onDelete(session.name))}
        >
          Kill Session
        </button>
      </PermissionGate>
    </div>
  );
}

// --- Sortable session item ---

interface SortableSessionProps {
  session: Session;
  isActive: boolean;
  isOpen: boolean;
  editingName: string | null;
  editValue: string;
  onSelect: (name: string) => void;
  onStartEdit: (name: string) => void;
  onEditChange: (value: string) => void;
  onEditSubmit: () => void;
  onEditCancel: () => void;
  onTogglePin: (name: string, pinned: boolean) => void;
  onDelete: (name: string) => void;
  onComplete: (name: string) => void;
  onEditDescription: (name: string) => void;
  onSetGroup: (name: string) => void;
  onHibernate: (name: string) => void;
  onResume: (name: string) => void;
  onRestart: (name: string) => void;
  onToggleAgentManaged: (name: string, managed: boolean) => void;
}

// eslint-disable-next-line sonarjs/cognitive-complexity -- pre-existing baseline (suppressions count pre-PR = 2, post-a490a4b = 3); fix tracked separately
const SortableSession = memo(function SortableSessionImpl({
  session,
  isActive,
  isOpen,
  editingName,
  editValue,
  onSelect,
  onStartEdit,
  onEditChange,
  onEditSubmit,
  onEditCancel,
  onTogglePin,
  onDelete,
  onComplete,
  onEditDescription,
  onSetGroup,
  onHibernate,
  onResume,
  onRestart,
  onToggleAgentManaged,
}: SortableSessionProps) {
  const {
    attributes,
    listeners,
    setNodeRef,
    transform,
    transition,
    isDragging,
  } = useSortable({ id: session.name });

  const style = {
    transform: CSS.Transform.toString(transform),
    transition,
    opacity: isDragging ? 0.5 : 1,
  };

  const { showMenu, setShowMenu, menuFlip, menuRef, triggerRef } = useActionsMenu();
  const inputRef = useRef<HTMLInputElement>(null);
  const submittingRef = useRef(false);

  useEffect(() => {
    if (editingName === session.name && inputRef.current) {
      inputRef.current.focus();
      inputRef.current.select();
    }
  }, [editingName, session.name]);

  const label = statusLabel(session);

  return (
    <div
      ref={setNodeRef}
      style={style}
      className={`
        group flex flex-col px-2 py-2.5 md:py-1.5 cursor-pointer border-l-2 transition-colors relative min-h-[44px] md:min-h-0
        ${isActive ? "bg-pir-surface-1 border-pir-accent" : "border-transparent hover:bg-pir-surface-1/60"}
      `}
      onClick={() => onSelect(session.name)}
      onContextMenu={(e) => {
        e.preventDefault();
        setShowMenu(true);
      }}
    >
      {/* Main row: drag handle + status dot + content + pin + menu */}
      <div className="flex items-center w-full">

      {/* Drag handle */}
      <div
        className="mr-1 cursor-grab opacity-0 group-hover:opacity-100 transition-opacity shrink-0"
        {...attributes}
        {...listeners}
      >
        <GripIcon />
      </div>

      {/* Status dot */}
      <div className={`w-2 h-2 rounded-full mr-2 shrink-0 ${statusColor(session, isOpen)}`} />

      {/* Name + description */}
      <div className="flex flex-col min-w-0 flex-1">
        {editingName === session.name ? (
          <input
            ref={inputRef}
            value={editValue}
            onChange={(e) => onEditChange(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter") {
                submittingRef.current = true;
                onEditSubmit();
              }
              if (e.key === "Escape") onEditCancel();
            }}
            onBlur={() => {
              // Delay cancel so Enter/submit can fire first
              setTimeout(() => {
                if (!submittingRef.current) onEditCancel();
                submittingRef.current = false;
              }, 150);
            }}
            onClick={(e) => e.stopPropagation()}
            className="text-sm font-mono bg-pir-base border border-pir-accent rounded px-1 py-0 text-pir-text-primary focus:outline-none w-full"
            maxLength={30}
          />
        ) : (
          <span
            className="text-sm font-mono text-pir-text-primary truncate cursor-text"
            title="Double-click to rename"
            onDoubleClick={(e) => {
              e.stopPropagation();
              onStartEdit(session.name);
            }}
          >
            {session.name}
          </span>
        )}
        {session.display_name && (
          <span className="text-[11px] text-pir-text-muted truncate leading-tight">
            {session.display_name}
          </span>
        )}
        {/* Status label + model */}
        {(label || session.model) && (
          <div className="flex items-center gap-1.5 leading-tight">
            {label && (
              <span className={`text-[10px] font-mono ${
                session.activity_state === "needs_input"
                  ? "text-amber-600 dark:text-amber-400 font-semibold animate-input"
                  : "text-pir-text-tertiary"
              }`}>
                {label}
              </span>
            )}
            {session.provider && session.provider !== "claude" && (
              <span className="text-[10px] font-bold text-pir-accent uppercase tracking-wide">
                {PROVIDER_BADGES[session.provider]}
              </span>
            )}
            {session.model && (
              <span className="text-[9px] font-mono text-pir-text-tertiary opacity-60">
                · {shortModel(session.model)}
              </span>
            )}
            {hasOneMillionContext(session.launch_model || session.model) && (
              <span className="rounded border border-pir-accent/40 px-1 py-0.5 text-[8px] font-semibold uppercase tracking-[0.12em] text-pir-accent">
                1m
              </span>
            )}
          </div>
        )}
        {/* Context bar */}
        {session.last_context_pct != null && (
          <div className="flex items-center gap-1 mt-0.5">
            <div className="h-1.5 flex-1 bg-pir/40 rounded-full overflow-hidden">
              <div
                className={`h-full rounded-full transition-all ${
                  session.last_context_pct > 80
                    ? "bg-pir-error"
                    : session.last_context_pct > 50
                      ? "bg-pir-warning"
                      : "bg-pir-accent"
                }`}
                style={{ width: `${Math.min(session.last_context_pct, 100)}%` }}
              />
            </div>
            <span className={`text-[10px] font-mono font-medium shrink-0 w-7 text-right ${
              session.last_context_pct > 80
                ? "text-pir-error"
                : session.last_context_pct > 50
                  ? "text-pir-warning"
                  : "text-pir-accent"
            }`}>
              {Math.round(session.last_context_pct)}%
            </span>
          </div>
        )}
        {/* Process metrics row: CPU + RAM */}
        {session.cpu_pct != null && (
          <div className="flex items-center gap-2 mt-0.5">
            <span className={`flex items-center gap-0.5 text-[9px] font-mono ${cpuColor(session.cpu_pct)}`}>
              <CpuIcon />
              {session.cpu_pct.toFixed(1)}%
            </span>
            {session.ram_mb != null && (
              <span className={`flex items-center gap-0.5 text-[9px] font-mono ${ramColor(session.ram_mb)}`}>
                <RamIcon />
                {session.ram_mb >= 1024
                  ? `${(session.ram_mb / 1024).toFixed(1)}G`
                  : `${Math.round(session.ram_mb)}M`}
              </span>
            )}
          </div>
        )}
        {/* Time row: uptime + working time + cost */}
        {session.created_epoch != null && (
          <div className="flex items-center gap-2 mt-0.5">
            <span className="flex items-center gap-0.5 text-[9px] font-mono text-pir-text-tertiary">
              <ClockIcon />
              {formatTimer(Math.floor(Date.now() / 1000 - session.created_epoch))}
            </span>
            <span className={`flex items-center gap-0.5 text-[9px] font-mono ${
              (session.working_seconds_msg ?? 0) > 0 ? "text-pir-text-tertiary" : "text-pir-text-tertiary opacity-30"
            }`}>
              <WorkIcon />
              {(session.working_seconds_msg ?? 0) > 0 ? formatTimer(session.working_seconds_msg as number) : "—"}
            </span>
            {session.last_cost_usd != null && (
              <span className="text-[9px] text-pir-text-tertiary font-mono shrink-0 ml-auto">
                ${session.last_cost_usd < 1
                  ? session.last_cost_usd.toFixed(2)
                  : session.last_cost_usd.toFixed(1)}
              </span>
            )}
          </div>
        )}
      </div>

      {/* Hibernated indicator */}
      {session.hibernated && (
        <span className="text-[9px] text-pir-warning font-mono shrink-0 ml-1" title="Hibernated">
          ZZZ
        </span>
      )}

      {/* Pin indicator */}
      {session.pinned && (
        <span className="text-pir-accent shrink-0 ml-1">
          <PinIcon filled />
        </span>
      )}

      {/* Agent managed indicator */}
      {session.agent_managed && (
        <span className="shrink-0 ml-1 text-xs" title="Agent managed">
          🤖
        </span>
      )}

      {/* Owner badge: shown when session has a user attribution */}
      {session.owner_id && (
        <span
          className="shrink-0 ml-1 text-[9px] font-mono text-pir-text-tertiary border border-pir rounded px-0.5 leading-tight"
          title={`Owned by ${session.owner_id}`}
        >
          {session.owner_id.slice(0, 4)}
        </span>
      )}

      {/* Actions menu trigger — hover-only (+ visible when menu open) */}
      <button
        ref={triggerRef}
        onClick={(e) => {
          e.stopPropagation();
          setShowMenu(!showMenu);
        }}
        className={`${showMenu ? "opacity-100" : "opacity-0 group-hover:opacity-100"} text-pir-text-secondary hover:text-pir-text-primary hover:bg-pir-surface-1 text-base leading-none font-bold ml-1 shrink-0 rounded w-5 h-5 flex items-center justify-center border border-pir hover:border-pir-accent transition-opacity transition-colors`}
        title="Session actions — rename, pin, delete, resume, hibernate, ..."
        aria-label="Session actions"
      >
        ⋯
      </button>
      </div>{/* end: flex items-center main row */}

      {/* Context menu */}
      {showMenu && (
        <SessionActionsMenu
          session={session}
          menuRef={menuRef}
          menuFlip={menuFlip}
          close={() => setShowMenu(false)}
          onStartEdit={onStartEdit}
          onEditDescription={onEditDescription}
          onTogglePin={onTogglePin}
          onSetGroup={onSetGroup}
          onToggleAgentManaged={onToggleAgentManaged}
          onResume={onResume}
          onHibernate={onHibernate}
          onRestart={onRestart}
          onComplete={onComplete}
          onDelete={onDelete}
        />
      )}
    </div>
  );
},
// Same rationale as SessionCardV2: reference equality on `session` (preserved
// by mergeStableSessions) + the three boolean/name drivers that actually
// affect the rendered subtree. Callbacks are stable-enough for close-on-click.
(prev, next) =>
  Object.is(prev.session, next.session) &&
  prev.isActive === next.isActive &&
  prev.isOpen === next.isOpen &&
  prev.editingName === next.editingName &&
  prev.editValue === next.editValue,
);

// --- Theme v2 session card + group header (gated by .theme-v2) -----------
//
// Target markup: ui_kits/terminal-direction-v3.html `.side > .grp` and `.ss[.active|.wait]`.
// v1 render stays untouched (SortableSession above); these components are
// rendered ONLY when useDesignV2() is true in SessionSidebar's body.

interface GroupHeaderV2Props {
  slug: string;
  label: string;
  sessions: Session[];
  collapsed: boolean;
  onToggle: (slug: string) => void;
  onHide: (slug: string) => void;
}

const GroupHeaderV2 = memo(function GroupHeaderV2Impl({
  slug,
  label,
  sessions,
  collapsed,
  onToggle,
  onHide,
}: GroupHeaderV2Props) {
  const waiting = sessions.filter((s) => s.activity_state === "needs_input").length;
  const total = sessions.length;
  const displayLabel = label || "Ungrouped";
  const expanded = !collapsed;
  return (
    <div
      role="button"
      tabIndex={0}
      onClick={() => onToggle(slug)}
      onKeyDown={(e) => {
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          onToggle(slug);
        }
      }}
      aria-expanded={expanded}
      aria-label={`Toggle ${displayLabel} group`}
      className="w-full flex justify-between items-center text-left bg-transparent hover:bg-pir-surface-1/60 transition-colors cursor-pointer"
      style={{ padding: "10px 12px 6px 12px", border: "none" }}
    >
      <span
        className="text-pir-accent relative"
        style={{
          fontFamily: "var(--pir-font-mono)",
          fontWeight: 600,
          fontSize: 9,
          letterSpacing: "0.2em",
          textTransform: "uppercase",
          paddingLeft: 8,
          lineHeight: 1,
        }}
      >
        <span
          aria-hidden
          className="absolute bg-pir-accent"
          style={{
            left: 0,
            top: "50%",
            width: 3,
            height: 3,
            transform: "translateY(-50%)",
          }}
        />
        {displayLabel}
      </span>
      <span
        className="flex items-center gap-1.5 text-pir-text-muted"
        style={{
          fontFamily: "var(--pir-font-mono)",
          fontWeight: 500,
          fontSize: 9,
          lineHeight: 1,
        }}
      >
        {waiting > 0 ? (
          <span>
            <span className="text-pir-accent" style={{ fontWeight: 700 }}>
              {waiting}
            </span>
            /{total}
          </span>
        ) : (
          <span>{total}</span>
        )}
        <button
          type="button"
          onClick={(e) => {
            e.stopPropagation();
            onHide(slug);
          }}
          aria-label={`Hide group ${displayLabel}`}
          title="Hide group"
          className="pir-v2-eye text-pir-text-muted hover:text-pir-text-primary bg-transparent border-0 p-0 flex items-center justify-center cursor-pointer"
          style={{
            width: 13,
            height: 13,
            transition: "opacity 150ms ease, color 150ms ease",
          }}
        >
          <svg
            width="11"
            height="11"
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            strokeWidth="1.8"
            strokeLinecap="round"
            strokeLinejoin="round"
            aria-hidden
          >
            <path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8S1 12 1 12z" />
            <circle cx="12" cy="12" r="3" />
          </svg>
        </button>
        <span
          aria-hidden
          className="pir-v2-chevron text-pir-text-muted"
          data-expanded={expanded ? "true" : "false"}
          style={{
            fontFamily: "var(--pir-font-mono)",
            fontSize: 9,
            lineHeight: 1,
            display: "inline-block",
            transform: expanded ? "rotate(90deg)" : "rotate(0deg)",
            transition: "transform 150ms ease",
            width: 9,
            textAlign: "center",
          }}
        >
          ›
        </span>
      </span>
    </div>
  );
});

interface SessionCardV2Props {
  session: Session;
  isActive: boolean;
  editingName: string | null;
  editValue: string;
  onSelect: (name: string) => void;
  // Actions context menu (mirror SortableSession)
  onStartEdit: (name: string) => void;
  onEditChange: (value: string) => void;
  onEditSubmit: () => void;
  onEditCancel: () => void;
  onEditDescription: (name: string) => void;
  onTogglePin: (name: string, pinned: boolean) => void;
  onSetGroup: (name: string) => void;
  onToggleAgentManaged: (name: string, managed: boolean) => void;
  onResume: (name: string) => void;
  onHibernate: (name: string) => void;
  onRestart: (name: string) => void;
  onComplete: (name: string) => void;
  onDelete: (name: string) => void;
}

// eslint-disable-next-line sonarjs/cognitive-complexity -- pre-existing baseline (suppressions count pre-PR = 2, post-a490a4b = 3); fix tracked separately
const SessionCardV2 = memo(function SessionCardV2Impl({
  session,
  isActive,
  editingName,
  editValue,
  onSelect,
  onStartEdit,
  onEditChange,
  onEditSubmit,
  onEditCancel,
  onEditDescription,
  onTogglePin,
  onSetGroup,
  onToggleAgentManaged,
  onResume,
  onHibernate,
  onRestart,
  onComplete,
  onDelete,
}: SessionCardV2Props) {
  const isWait = session.activity_state === "needs_input";
  const isWorking = session.activity_state === "working";
  const isEditing = editingName === session.name;

  // Actions context menu (mirror SortableSession) — shared hook
  const { showMenu, setShowMenu, menuFlip, menuRef, triggerRef } = useActionsMenu();
  const inputRef = useRef<HTMLInputElement>(null);
  const submittingRef = useRef(false);

  useEffect(() => {
    if (isEditing && inputRef.current) {
      inputRef.current.focus();
      inputRef.current.select();
    }
  }, [isEditing]);

  // Variant background / border-left per spec (.ss / .ss.active / .ss.wait).
  let variantStyle: React.CSSProperties = {
    borderLeft: "2px solid transparent",
  };
  if (isActive) {
    variantStyle = {
      background: "hsl(var(--pir-success) / 0.10)",
      borderLeft: "2px solid hsl(var(--pir-success))",
    };
  } else if (isWait) {
    variantStyle = {
      background: "hsl(var(--pir-accent) / 0.10)",
      borderLeft: "2px solid hsl(var(--pir-accent))",
    };
  }

  // PR3: last_context_pct is now SQL-aliased from last_context_pct_real;
  // the `??` fallback still works for edge cases where only one is populated.
  const ctxReal = session.last_context_pct_real ?? session.last_context_pct ?? null;
  const ctxScaled = session.last_context_pct_scaled ?? null;
  const ctxPct = ctxReal;
  const ctxHot = ctxPct != null && ctxPct >= 80;

  // PR3: DUR sourced exclusively from working_seconds_msg (wall-clock
  // message-gap sum via parser). Legacy pane-scraped working_seconds is gone.
  const durText =
    session.working_seconds_msg != null && session.working_seconds_msg > 0
      ? formatTimer(session.working_seconds_msg)
      : "—";
  const modelShort = session.model ? shortModel(session.model) : "—";

  // PR2: dual cost (conversation / session-cumulative). Back-compat: fall back
  // to last_cost_usd when the dual columns aren't present yet.
  const costConv = session.last_cost_conversation_usd ?? session.last_cost_usd ?? null;
  const costSession = session.last_cost_session_usd ?? null;
  const costText = formatCostDual(costConv, costSession);
  // PR4: shadow cost badge (OAuth sessions showing hypothetical API bill).
  const costEquivalent =
    session.last_cost_session_equivalent_usd ??
    session.last_cost_conversation_equivalent_usd ??
    null;
  const costRealForBadge = costSession ?? costConv;
  const costEquivalentBadge = formatCostEquivalentBadge(
    costRealForBadge,
    costEquivalent,
  );

  const metricsStale = isMetricsStale(session.metrics_refreshed_at);
  const ctxDualText = formatCtxDual(ctxReal, ctxScaled);
  const inTokens = session.last_input_tokens;
  const outTokens = session.last_output_tokens;

  // Treat "pinned" as priority P1 badge (closest domain signal available today).
  const showP1 = session.pinned;

  return (
    <div
      role="button"
      tabIndex={0}
      onClick={() => onSelect(session.name)}
      onKeyDown={(e) => {
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          onSelect(session.name);
        }
      }}
      onContextMenu={(e) => {
        e.preventDefault();
        setShowMenu(true);
      }}
      className="group relative block w-full text-left cursor-pointer transition-colors"
      style={{
        margin: "1px 4px",
        borderRadius: "var(--radius-sm, 2px)",
        padding: isActive ? "8px 10px 10px 12px" : "7px 10px 7px 12px",
        width: "calc(100% - 8px)",
        ...variantStyle,
      }}
    >
      {/* Actions menu trigger — hover-only pill top-right (+ visible when menu open) */}
      <button
        ref={triggerRef}
        type="button"
        onClick={(e) => {
          e.stopPropagation();
          setShowMenu(!showMenu);
        }}
        className={`${showMenu ? "opacity-100" : "opacity-0 group-hover:opacity-100"} absolute top-1.5 right-1.5 z-10 bg-pir-surface-0 text-pir-text-secondary hover:text-pir-text-primary hover:bg-pir-surface-1 text-base leading-none font-bold shrink-0 rounded w-5 h-5 flex items-center justify-center border border-pir hover:border-pir-accent transition-opacity transition-colors`}
        title="Session actions — rename, pin, delete, resume, hibernate, ..."
        aria-label="Session actions"
      >
        ⋯
      </button>

      {/* Context menu dropdown — shared with SortableSession */}
      {showMenu && (
        <SessionActionsMenu
          session={session}
          menuRef={menuRef}
          menuFlip={menuFlip}
          close={() => setShowMenu(false)}
          onStartEdit={onStartEdit}
          onEditDescription={onEditDescription}
          onTogglePin={onTogglePin}
          onSetGroup={onSetGroup}
          onToggleAgentManaged={onToggleAgentManaged}
          onResume={onResume}
          onHibernate={onHibernate}
          onRestart={onRestart}
          onComplete={onComplete}
          onDelete={onDelete}
        />
      )}

      {/* Row 1 (or .top in active): name + right cluster */}
      <div
        style={{
          display: "grid",
          gridTemplateColumns: "1fr auto",
          columnGap: 8,
          alignItems: "center",
        }}
      >
        {isEditing ? (
          <input
            ref={inputRef}
            value={editValue}
            onChange={(e) => onEditChange(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter") {
                submittingRef.current = true;
                onEditSubmit();
              }
              if (e.key === "Escape") onEditCancel();
            }}
            onBlur={() => {
              setTimeout(() => {
                if (!submittingRef.current) onEditCancel();
                submittingRef.current = false;
              }, 150);
            }}
            onClick={(e) => e.stopPropagation()}
            className="bg-pir-base border border-pir-accent rounded px-1 py-0 text-pir-text-primary focus:outline-none w-full truncate"
            style={{
              fontFamily: "var(--pir-font-sans)",
              fontSize: 12.5,
              fontWeight: isActive || isWait ? 600 : 500,
              lineHeight: 1,
            }}
            maxLength={30}
          />
        ) : (
          <span
            className="text-pir-text-primary truncate"
            title="Double-click to rename"
            onDoubleClick={(e) => {
              e.stopPropagation();
              onStartEdit(session.name);
            }}
            style={{
              fontFamily: "var(--pir-font-sans)",
              fontSize: 12.5,
              fontWeight: isActive || isWait ? 600 : 500,
              lineHeight: 1,
            }}
          >
            {session.display_name || session.name}
          </span>
        )}
        <span
          className="flex items-center gap-1.5"
          style={{
            fontFamily: "var(--pir-font-mono)",
            fontWeight: 500,
            fontSize: 10,
            color: "var(--pir-text-tertiary)",
            lineHeight: 1,
          }}
        >
          {showP1 && (
            <span
              className="bg-pir-accent"
              style={{
                fontFamily: "var(--pir-font-mono)",
                fontWeight: 700,
                fontSize: 8,
                letterSpacing: "0.1em",
                padding: "2px 4px",
                borderRadius: 2,
                color: "hsl(var(--pir-bone, 34 28% 88%))",
                lineHeight: 1,
              }}
            >
              P1
            </span>
          )}
          {isWait && (
            <span
              className="text-pir-accent pir-v2-nudge"
              style={{
                fontFamily: "var(--pir-font-sans)",
                fontWeight: 700,
                fontSize: 16,
                lineHeight: 0.5,
              }}
              aria-label="needs input"
            >
              ›
            </span>
          )}
          {!isWait && isWorking && (
            <span
              className="inline-flex items-center"
              aria-label="working"
              title="Agent is working"
            >
              <L5Loader size={12} />
            </span>
          )}
          {!isWait && !isWorking && ctxPct != null && (
            <span
              style={{
                color: ctxHot ? "hsl(var(--pir-accent))" : "var(--pir-text-secondary)",
                fontWeight: 600,
              }}
            >
              {Math.round(ctxPct)}%
            </span>
          )}
          {!isWait && !isWorking && ctxPct == null && <span>—</span>}
        </span>
      </div>

      {/* Expanded metrics (active only): 2-row dual layout (PR2).
          Row 1: Model / Dur / Cost (dual conv/session, collapse if equal).
          Row 2: In / Out / Ctx (dual real/scaled, "—" when scaled is null
          e.g. for OpenCode). Staleness fades all metrics to 0.5 opacity when
          metrics_refreshed_at is older than 1 hour. */}
      {isActive && (
        <div
          style={{
            marginTop: 8,
            paddingTop: 8,
            borderTop: "1px dashed hsl(var(--pir-success) / 0.25)",
            display: "flex",
            flexDirection: "column",
            gap: 6,
            fontFamily: "var(--pir-font-mono)",
            fontWeight: 500,
            fontSize: 10,
            lineHeight: 1.2,
            opacity: metricsStale ? 0.5 : 1,
          }}
        >
          <div
            style={{
              display: "grid",
              gridTemplateColumns: "repeat(3, 1fr)",
              gap: 6,
            }}
          >
            <div className="flex flex-col gap-[3px]">
              <span
                className="text-pir-text-muted uppercase"
                style={{ fontSize: 8.5, letterSpacing: "0.15em" }}
              >
                Model
              </span>
              <span className="text-pir-text-primary" style={{ fontWeight: 600 }}>
                {modelShort}
              </span>
            </div>
            <div className="flex flex-col gap-[3px]">
              <span
                className="text-pir-text-muted uppercase"
                style={{ fontSize: 8.5, letterSpacing: "0.15em" }}
              >
                Dur
              </span>
              <span className="text-pir-text-primary" style={{ fontWeight: 600 }}>
                {durText}
              </span>
            </div>
            <div
              className="flex flex-col gap-[3px]"
              aria-label={
                costSession != null && costConv != null
                  ? `Cost conversation ${costConv.toFixed(2)} session ${costSession.toFixed(2)}`
                  : undefined
              }
            >
              <span
                className="text-pir-text-muted uppercase"
                style={{ fontSize: 8.5, letterSpacing: "0.15em" }}
              >
                Cost
              </span>
              <span
                style={{
                  fontWeight: 600,
                  color:
                    costConv != null
                      ? "hsl(var(--pir-success))"
                      : "var(--pir-text-primary)",
                }}
              >
                {costText}
              </span>
              {costEquivalentBadge && (
                <span
                  className="text-pir-text-tertiary"
                  style={{
                    fontFamily: "var(--pir-font-mono)",
                    fontSize: 9,
                    fontWeight: 500,
                    letterSpacing: "0.02em",
                  }}
                  aria-label={`Equivalent API cost ${costEquivalentBadge}`}
                >
                  {costEquivalentBadge}
                </span>
              )}
            </div>
          </div>
          <div
            style={{
              display: "grid",
              gridTemplateColumns: "repeat(3, 1fr)",
              gap: 6,
            }}
          >
            <div className="flex flex-col gap-[3px]">
              <span
                className="text-pir-text-muted uppercase"
                style={{ fontSize: 8.5, letterSpacing: "0.15em" }}
              >
                In
              </span>
              <span className="text-pir-text-primary" style={{ fontWeight: 600 }}>
                {formatTokens(inTokens)}
              </span>
            </div>
            <div className="flex flex-col gap-[3px]">
              <span
                className="text-pir-text-muted uppercase"
                style={{ fontSize: 8.5, letterSpacing: "0.15em" }}
              >
                Out
              </span>
              <span className="text-pir-text-primary" style={{ fontWeight: 600 }}>
                {formatTokens(outTokens)}
              </span>
            </div>
            <div
              className="flex flex-col gap-[3px]"
              aria-label={
                ctxReal != null || ctxScaled != null
                  ? `Context real ${ctxReal ?? "unknown"}% scaled ${ctxScaled ?? "unknown"}%`
                  : undefined
              }
            >
              <span
                className="text-pir-text-muted uppercase"
                style={{ fontSize: 8.5, letterSpacing: "0.15em" }}
              >
                Ctx
              </span>
              <span
                style={{
                  fontWeight: 600,
                  color: ctxHot
                    ? "hsl(var(--pir-accent))"
                    : "var(--pir-text-primary)",
                }}
              >
                {ctxDualText}
              </span>
            </div>
          </div>
        </div>
      )}
    </div>
  );
},
// Custom comparison: reuse rendered card when the session object reference,
// isActive, and inline editing state haven't changed. onSelect and other
// handlers are intentionally skipped — sites pass stable useCallback or inline
// arrows; their identity doesn't affect output because they only fire on user
// action. Reference equality on `session` is enough because mergeStableSessions
// preserves it when content is unchanged. editingName + editValue MUST be
// compared, otherwise starting/cancelling a rename never re-renders the card
// (input stays hidden or stale) — parity with SortableSession comparator (~781).
(prev, next) =>
  Object.is(prev.session, next.session) &&
  prev.isActive === next.isActive &&
  prev.editingName === next.editingName &&
  prev.editValue === next.editValue,
);

// --- Prompt modal (for description and group name) ---

interface PromptModalProps {
  title: string;
  placeholder: string;
  initialValue: string;
  onSubmit: (value: string) => void;
  onClose: () => void;
}

function PromptModal({ title, placeholder, initialValue, onSubmit, onClose }: PromptModalProps) {
  const [value, setValue] = useState(initialValue);

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60">
      <form
        onSubmit={(e) => {
          e.preventDefault();
          onSubmit(value);
        }}
        className="bg-pir-surface-0 border border-pir rounded p-5 w-full max-w-sm"
        onClick={(e) => e.stopPropagation()}
      >
        <h3 className="text-sm font-semibold mb-3">{title}</h3>
        <input
          value={value}
          onChange={(e) => setValue(e.target.value)}
          placeholder={placeholder}
          className="w-full bg-pir-base border border-pir rounded px-3 py-2 text-pir-text-primary focus:outline-none focus:border-pir-accent text-sm"
          autoFocus
          maxLength={100}
        />
        <div className="flex gap-3 justify-end mt-4">
          <button
            type="button"
            onClick={onClose}
            className="px-3 py-1.5 text-sm text-pir-text-secondary hover:text-pir-text-primary"
          >
            Cancel
          </button>
          <button
            type="submit"
            className="px-3 py-1.5 text-sm bg-pir-accent text-white rounded hover:bg-pir-accent/90"
          >
            Save
          </button>
        </div>
      </form>
    </div>
  );
}

// --- Collapsed groups persistence ---

const COLLAPSED_GROUPS_KEY = "marvis-collapsed-groups";

function loadCollapsedGroups(): Set<string> {
  try {
    const raw = localStorage.getItem(COLLAPSED_GROUPS_KEY);
    if (!raw) return new Set();
    const arr = JSON.parse(raw);
    return Array.isArray(arr) ? new Set(arr) : new Set();
  } catch {
    return new Set();
  }
}

// --- Hidden groups persistence ---

const HIDDEN_GROUPS_KEY = "marvis-hidden-groups";

function loadHiddenGroups(): Set<string> {
  try {
    const raw = localStorage.getItem(HIDDEN_GROUPS_KEY);
    if (!raw) return new Set();
    const arr = JSON.parse(raw);
    return Array.isArray(arr) ? new Set(arr) : new Set();
  } catch {
    return new Set();
  }
}

// --- Main sidebar component ---

interface SessionSidebarProps {
  panelVisible?: boolean;
  activeSession: string | null;
  openSessions: string[];
  onSelectSession: (name: string) => void;
  onSessionCreated: (name: string, initialCommand?: string) => void;
  onSessionDeleted: (name: string) => void;
  onSessionRenamed?: (oldName: string, newName: string) => void;
}

// eslint-disable-next-line sonarjs/cognitive-complexity -- pre-existing baseline (suppressions count pre-PR = 2, post-a490a4b = 3); fix tracked separately
export default function SessionSidebar({
  panelVisible = true,
  activeSession,
  openSessions,
  onSelectSession,
  onSessionCreated,
  onSessionDeleted,
  onSessionRenamed,
}: SessionSidebarProps) {
  const [sessions, setSessions] = useState<Session[]>([]);
  const [projectCosts, setProjectCosts] = useState<Map<string, number>>(new Map());
  const [showCreate, setShowCreate] = useState(false);
  const [confirmDelete, setConfirmDelete] = useState<string | null>(null);
  const [confirmComplete, setConfirmComplete] = useState<string | null>(null);
  const [collapsedGroups, setCollapsedGroups] = useState<Set<string>>(loadCollapsedGroups);
  const [hiddenGroups, setHiddenGroups] = useState<Set<string>>(loadHiddenGroups);
  const [hiddenDrawerOpen, setHiddenDrawerOpen] = useState(false);
  const v2 = useDesignV2();

  // Inline rename state
  const [editingName, setEditingName] = useState<string | null>(null);
  const [editValue, setEditValue] = useState("");
  const editingRef = useRef<string | null>(null);

  // Keep ref in sync so polling can check without stale closure
  useEffect(() => {
    editingRef.current = editingName;
  }, [editingName]);

  // Prompt modal state
  const [promptModal, setPromptModal] = useState<{
    type: "description" | "project";
    sessionName: string;
    initial: string;
  } | null>(null);

  const sensors = useSensors(
    useSensor(PointerSensor, { activationConstraint: { distance: 5 } })
  );

  const refreshSessions = useCallback(async () => {
    // Skip refresh while user is editing a session name
    if (editingRef.current) return;
    try {
      const data = await listSessions();
      setSessions((prev) => mergeStableSessions(prev, data));
    } catch {
      // Silently fail, will retry
    }
  }, []);

  // Plan 2026-05-21: listen for WS broadcast session_renamed event with
  // delta payload {old_name, new_name, session_info} and patch the sessions
  // state in-place. This closes the post-rename stale sidebar bug by avoiding
  // the refetch round-trip (which can hit a stale server cache window).
  useEffect(() => {
    function handleSessionsChanged(event: Event) {
      const detail = (event as CustomEvent).detail as
        | { event?: string; old_name?: string; new_name?: string; session_info?: Partial<Session> & { prev_name: string; updated_at: string } }
        | undefined;
      if (!detail || detail.event !== "renamed") return;
      const oldName = detail.old_name;
      const newName = detail.new_name;
      const info = detail.session_info;
      if (!oldName || !newName || !info) return;
      setSessions((prev) =>
        prev.map((s) => {
          if (s.name !== oldName) return s;
          return {
            ...s,
            name: newName,
            display_name: info.display_name ?? s.display_name,
            provider: info.provider ?? s.provider,
            model: info.model ?? s.model,
            project_slug: info.project_slug ?? s.project_slug,
          };
        })
      );
    }
    window.addEventListener("marvisx:sessions_changed", handleSessionsChanged);
    return () => {
      window.removeEventListener("marvisx:sessions_changed", handleSessionsChanged);
    };
  }, []);

  const refreshProjectCosts = useCallback(async () => {
    try {
      const summaries: ProjectCostSummary[] = await getCostsSummary();
      const map = new Map<string, number>();
      for (const s of summaries) {
        map.set(s.project_slug, s.total_cost_usd);
      }
      setProjectCosts(map);
    } catch {
      // Non-critical
    }
  }, []);

  useEffect(() => {
    let sessionTimer: ReturnType<typeof setTimeout> | null = null;
    let costsInterval: ReturnType<typeof setInterval> | null = null;

    const scheduleNextSessionRefresh = () => {
      sessionTimer = setTimeout(() => {
        refreshSessions();
        scheduleNextSessionRefresh();
      }, nextSessionRefreshDelayMs());
    };

    const startPolling = () => {
      if (!panelVisible) return;
      if (sessionTimer || costsInterval) return;
      refreshSessions();
      refreshProjectCosts();
      // Keep the average cadence near the server cache TTL, but jitter every
      // cycle so multiple visible sidebars do not stampede /sessions together.
      scheduleNextSessionRefresh();
      costsInterval = setInterval(refreshProjectCosts, 60_000);
    };

    const stopPolling = () => {
      if (sessionTimer) { clearTimeout(sessionTimer); sessionTimer = null; }
      if (costsInterval) { clearInterval(costsInterval); costsInterval = null; }
    };

    const handleVisibility = () => {
      if (panelVisible && document.visibilityState === "visible") {
        startPolling();
      } else {
        stopPolling();
      }
    };

    if (panelVisible) startPolling();
    document.addEventListener("visibilitychange", handleVisibility);

    return () => {
      stopPolling();
      document.removeEventListener("visibilitychange", handleVisibility);
    };
  }, [panelVisible, refreshSessions, refreshProjectCosts]);

  // --- Handlers ---

  async function handleDelete(name: string) {
    try {
      await deleteSession(name);
      onSessionDeleted(name);
      setConfirmDelete(null);
      refreshSessions();
    } catch {
      // Error handled by API client
    }
  }

  async function handleComplete(name: string) {
    try {
      await completeSession(name);
      onSessionDeleted(name);
      setConfirmComplete(null);
      refreshSessions();
    } catch {
      // Error handled by API client
    }
  }

  function handleCreated(name: string, initialCommand?: string) {
    setShowCreate(false);
    onSessionCreated(name, initialCommand);
    refreshSessions();
  }

  async function handleTogglePin(name: string, pinned: boolean) {
    try {
      await updateSession(name, { pinned });
      refreshSessions();
    } catch {
      // ignore
    }
  }

  async function handleToggleAgentManaged(name: string, managed: boolean) {
    try {
      await updateSession(name, { agent_managed: managed });
      refreshSessions();
    } catch {
      // ignore
    }
  }

  async function handleEditSubmit() {
    if (!editingName || !editValue.trim() || editValue === editingName) {
      setEditingName(null);
      return;
    }
    try {
      const updated = await updateSession(editingName, { new_name: editValue.trim() });
      onSessionRenamed?.(editingName, editValue.trim());
      // Clear editingRef synchronously — the useEffect mirror is async, so
      // refreshSessions() below would early-return (see ref guard in
      // refreshSessions) and the UI would stay stale until next 15s poll.
      editingRef.current = null;
      setEditingName(null);
      // Optimistic patch from PATCH response — sidebar reflects new name
      // instantly without waiting for the WS broadcast or the refetch.
      // The WS broadcast (Plan 2026-05-21) still arrives for cross-tab sync,
      // dedup is handled by `applySessionRenameDelta` idempotency map.
      if (updated) {
        setSessions((prev) =>
          prev.map((s) => (s.name === editingName ? { ...s, ...updated } : s))
        );
      }
    } catch {
      editingRef.current = null;
      setEditingName(null);
    }
  }

  async function handleDescriptionSubmit(value: string) {
    if (!promptModal) return;
    try {
      await updateSession(promptModal.sessionName, { display_name: value });
      setPromptModal(null);
      refreshSessions();
      setTimeout(refreshSessions, 300);
    } catch {
      setPromptModal(null);
    }
  }

  async function handleProjectSubmit(value: string) {
    if (!promptModal) return;
    try {
      await updateSession(promptModal.sessionName, { project_slug: value || null });
      setPromptModal(null);
      refreshSessions();
      setTimeout(refreshSessions, 300);
    } catch {
      setPromptModal(null);
    }
  }

  async function handleResurrect(name: string) {
    try {
      await resurrectSession(name);
      onSessionCreated(name);
      refreshSessions();
    } catch {
      // ignore
    }
  }

  async function handleHibernate(name: string) {
    try {
      await hibernateSession(name);
      refreshSessions();
    } catch {
      // ignore
    }
  }

  async function handleResume(name: string) {
    try {
      await resumeSession(name);
      refreshSessions();
    } catch {
      // ignore
    }
  }

  async function handleRestart(name: string) {
    try {
      await restartSession(name);
      refreshSessions();
    } catch {
      // ignore
    }
  }

  async function handleDragEnd(event: DragEndEvent) {
    const { active, over } = event;
    if (!over || active.id === over.id) return;

    // Reorder locally first for instant feedback
    const oldIndex = sessions.findIndex((s) => s.name === active.id);
    const newIndex = sessions.findIndex((s) => s.name === over.id);
    if (oldIndex === -1 || newIndex === -1) return;

    const reordered = [...sessions];
    const [moved] = reordered.splice(oldIndex, 1);
    reordered.splice(newIndex, 0, moved);
    setSessions(reordered);

    // Persist to server
    try {
      await reorderSessions(reordered.map((s) => s.name));
    } catch {
      refreshSessions(); // revert on failure
    }
  }

  function toggleGroup(groupName: string) {
    setCollapsedGroups((prev) => {
      const next = new Set(prev);
      if (next.has(groupName)) {
        next.delete(groupName);
      } else {
        next.add(groupName);
      }
      try {
        localStorage.setItem(COLLAPSED_GROUPS_KEY, JSON.stringify([...next]));
      } catch {}
      return next;
    });
  }

  function hideGroup(groupName: string) {
    setHiddenGroups((prev) => {
      const next = new Set(prev);
      next.add(groupName);
      try {
        localStorage.setItem(HIDDEN_GROUPS_KEY, JSON.stringify([...next]));
      } catch {}
      return next;
    });
  }

  function showGroup(groupName: string) {
    setHiddenGroups((prev) => {
      const next = new Set(prev);
      next.delete(groupName);
      try {
        localStorage.setItem(HIDDEN_GROUPS_KEY, JSON.stringify([...next]));
      } catch {}
      return next;
    });
  }

  // --- Group sessions by project_slug ---

  const groups = new Map<string, Session[]>();
  for (const session of sessions) {
    const g = session.project_slug || "";
    if (!groups.has(g)) groups.set(g, []);
    groups.get(g)!.push(session);
  }

  // Sort group names: empty string (ungrouped) last
  const sortedGroupNames = [...groups.keys()].sort((a, b) => {
    if (a === "" && b !== "") return 1;
    if (a !== "" && b === "") return -1;
    return a.localeCompare(b);
  });

  const hasGroups = sortedGroupNames.length > 1 || (sortedGroupNames.length === 1 && sortedGroupNames[0] !== "");

  // existingGroups removed — ProjectSelectorModal loads projects from API

  // --- Render helpers ---

  function renderSession(session: Session) {
    const isOpen = openSessions.includes(session.name);
    const isActive = activeSession === session.name;

    return (
      <SortableSession
        key={session.name}
        session={session}
        isActive={isActive}
        isOpen={isOpen}
        editingName={editingName}
        editValue={editValue}
        onSelect={onSelectSession}
        onStartEdit={(name) => {
          setEditingName(name);
          setEditValue(name);
        }}
        onEditChange={setEditValue}
        onEditSubmit={handleEditSubmit}
        onEditCancel={() => setEditingName(null)}
        onTogglePin={handleTogglePin}
        onDelete={setConfirmDelete}
        onComplete={setConfirmComplete}
        onEditDescription={(name) => {
          const s = sessions.find((x) => x.name === name);
          setPromptModal({
            type: "description",
            sessionName: name,
            initial: s?.display_name || "",
          });
        }}
        onSetGroup={(name) => {
          const s = sessions.find((x) => x.name === name);
          setPromptModal({
            type: "project",
            sessionName: name,
            initial: s?.project_slug || "",
          });
        }}
        onHibernate={handleHibernate}
        onResume={handleResume}
        onRestart={handleRestart}
        onToggleAgentManaged={handleToggleAgentManaged}
      />
    );
  }

  function renderSessionV2(session: Session) {
    const isActive = activeSession === session.name;
    return (
      <SessionCardV2
        key={session.name}
        session={session}
        isActive={isActive}
        editingName={editingName}
        editValue={editValue}
        onSelect={onSelectSession}
        onStartEdit={(name) => {
          setEditingName(name);
          setEditValue(name);
        }}
        onEditChange={setEditValue}
        onEditSubmit={handleEditSubmit}
        onEditCancel={() => setEditingName(null)}
        onEditDescription={(name) => {
          const s = sessions.find((x) => x.name === name);
          setPromptModal({
            type: "description",
            sessionName: name,
            initial: s?.display_name || "",
          });
        }}
        onTogglePin={handleTogglePin}
        onSetGroup={(name) => {
          const s = sessions.find((x) => x.name === name);
          setPromptModal({
            type: "project",
            sessionName: name,
            initial: s?.project_slug || "",
          });
        }}
        onToggleAgentManaged={handleToggleAgentManaged}
        onResume={handleResume}
        onHibernate={handleHibernate}
        onRestart={handleRestart}
        onComplete={setConfirmComplete}
        onDelete={setConfirmDelete}
      />
    );
  }

  // --- Theme v2 body render ---------------------------------------------
  // When the design-v2 flag is ON we switch to the compact terminal-direction-v3
  // layout: no drag handles, no CPU/RAM/metrics clutter — just accent eyebrow
  // group headers + compact cards with active/wait variants. The v1 body below
  // is untouched so the default render (and all existing tests) stay identical.
  if (v2) {
    return (
      <aside className="w-60 bg-pir-surface-0 border-r border-pir flex flex-col h-full shrink-0 overflow-y-auto">
        {/* Sticky header con count sessioni + bottone new. Sostituisce il
            "+" della SubbarV2 (rimossa 2026-05-16): da quel momento il
            new-session-button era sr-only e irraggiungibile via mouse. */}
        <PermissionGate minRole="operator">
          <div className="sticky top-0 z-10 flex items-center justify-between gap-2 bg-pir-surface-0 border-b border-pir px-3 py-2">
            <span
              className="text-pir-text-tertiary font-mono uppercase"
              style={{ fontSize: 10, letterSpacing: "0.18em" }}
            >
              Sessioni {sessions.length}
            </span>
            <button
              type="button"
              data-testid="new-session-button"
              onClick={() => setShowCreate(true)}
              className="inline-flex items-center justify-center rounded-sm border border-pir-strong bg-pir-surface-2 text-pir-text-primary hover:text-pir-success hover:border-pir-success transition-colors"
              style={{ width: 22, height: 22, fontFamily: "var(--pir-font-mono)", fontSize: 14, lineHeight: 1 }}
              aria-label="New session"
            >
              +
            </button>
          </div>
        </PermissionGate>
        {sessions.length === 0 ? (
          <div className="p-4 text-center text-sm text-pir-text-muted">No sessions yet</div>
        ) : (
          <>
            {hasGroups ? (
              sortedGroupNames
                .filter((g) => !hiddenGroups.has(g))
                .map((groupName) => {
                  const groupSessions = groups.get(groupName) || [];
                  const label = groupName || "Ungrouped";
                  const isCollapsed = collapsedGroups.has(groupName);
                  return (
                    <div key={groupName || "__ungrouped"}>
                      <GroupHeaderV2
                        slug={groupName}
                        label={label}
                        sessions={groupSessions}
                        collapsed={isCollapsed}
                        onToggle={toggleGroup}
                        onHide={hideGroup}
                      />
                      {!isCollapsed && groupSessions.map((session) => renderSessionV2(session))}
                    </div>
                  );
                })
            ) : (
              sessions.map((session) => renderSessionV2(session))
            )}
            {hiddenGroups.size > 0 && (
              <div className="mt-auto border-t border-pir">
                <button
                  type="button"
                  onClick={() => setHiddenDrawerOpen((prev) => !prev)}
                  aria-expanded={hiddenDrawerOpen}
                  title="Show hidden groups"
                  className="text-pir-text-muted flex items-center gap-1.5 bg-transparent hover:bg-pir-surface-1/60 transition-colors text-left w-full cursor-pointer"
                  style={{
                    fontFamily: "var(--pir-font-mono)",
                    fontWeight: 500,
                    fontSize: 10,
                    letterSpacing: "0.15em",
                    textTransform: "uppercase",
                    padding: "8px 14px",
                    lineHeight: 1,
                  }}
                >
                  <span aria-hidden>◎</span>
                  {hiddenGroups.size} hidden group{hiddenGroups.size === 1 ? "" : "s"}
                  <span className="ml-auto" aria-hidden>
                    {hiddenDrawerOpen ? "▾" : "▸"}
                  </span>
                </button>
                {hiddenDrawerOpen && (
                  <div className="flex flex-col gap-0.5 px-2 pb-2 pt-1 border-t border-pir">
                    {[...hiddenGroups].sort().map((slug) => (
                      <div
                        key={slug || "__ungrouped"}
                        className="flex items-center gap-2 px-2 py-1 text-[11px] text-pir-text-tertiary"
                      >
                        <span className="flex-1 truncate font-mono">
                          {slug || "Ungrouped"}
                        </span>
                        <button
                          type="button"
                          onClick={() => showGroup(slug)}
                          title="Unhide"
                          className="text-[9px] uppercase tracking-[0.12em] text-pir-accent hover:text-pir-text-primary bg-transparent border-0 cursor-pointer"
                        >
                          Unhide
                        </button>
                      </div>
                    ))}
                    <button
                      type="button"
                      onClick={() => {
                        setHiddenGroups(new Set());
                        try {
                          localStorage.setItem(HIDDEN_GROUPS_KEY, JSON.stringify([]));
                        } catch {
                          // Safari private mode, etc. — in-memory only
                        }
                      }}
                      className="mt-1 text-[10px] text-pir-text-muted hover:text-pir-accent self-start px-2 bg-transparent border-0 cursor-pointer"
                    >
                      Show all
                    </button>
                  </div>
                )}
              </div>
            )}
          </>
        )}
        {/* Modals (shared with v1) */}
        {confirmComplete && (
          <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60">
            <div className="bg-pir-surface-0 border border-pir rounded p-5 max-w-sm">
              <p className="text-sm font-semibold mb-2">
                Complete session <span className="font-mono">{confirmComplete}</span>?
              </p>
              <p className="text-xs text-pir-text-secondary mb-4">
                The session will be stopped and a final recap (cost, time, tokens) will be saved to the project.
              </p>
              <div className="flex gap-3 justify-end">
                <button
                  onClick={() => setConfirmComplete(null)}
                  className="px-3 py-1 text-sm text-pir-text-secondary hover:text-pir-text-primary"
                >
                  Cancel
                </button>
                <button
                  onClick={() => handleComplete(confirmComplete)}
                  className="px-3 py-1 text-sm bg-pir-success text-white rounded hover:opacity-90"
                >
                  Complete
                </button>
              </div>
            </div>
          </div>
        )}
        {confirmDelete && (
          <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60">
            <div className="bg-pir-surface-0 border border-pir rounded p-5 max-w-sm">
              <p className="text-sm font-semibold mb-2">
                Kill session <span className="font-mono">{confirmDelete}</span>?
              </p>
              <p className="text-xs text-pir-error/80 mb-4">
                This will permanently delete all session data. Cost history and recap will be lost. Use &quot;Complete Session&quot; to save a recap first.
              </p>
              <div className="flex gap-3 justify-end">
                <button
                  onClick={() => setConfirmDelete(null)}
                  className="px-3 py-1 text-sm text-pir-text-secondary hover:text-pir-text-primary"
                >
                  Cancel
                </button>
                <button
                  onClick={() => handleDelete(confirmDelete)}
                  className="px-3 py-1 text-sm bg-pir-error text-white rounded hover:bg-pir-error/90"
                >
                  Kill
                </button>
              </div>
            </div>
          </div>
        )}
        {promptModal && promptModal.type === "description" && (
          <PromptModal
            title="Session Description"
            placeholder="What is this session for?"
            initialValue={promptModal.initial}
            onSubmit={handleDescriptionSubmit}
            onClose={() => setPromptModal(null)}
          />
        )}
        {promptModal && promptModal.type === "project" && (
          <ProjectSelectorModal
            currentSlug={promptModal.initial}
            onSubmit={handleProjectSubmit}
            onClose={() => setPromptModal(null)}
          />
        )}
        {showCreate && (
          <CreateSessionModal
            onClose={() => setShowCreate(false)}
            onCreated={handleCreated}
          />
        )}
      </aside>
    );
  }

  return (
    <aside className="w-60 bg-pir-surface-0 border-r border-pir flex flex-col h-full shrink-0">
      {/* Header */}
      <div className="p-3 border-b border-pir flex items-center justify-between">
        <span className="text-sm font-semibold text-pir-text-primary">Sessions</span>
        <PermissionGate minRole="operator">
          <button
            data-testid="new-session-button"
            onClick={() => setShowCreate(true)}
            className="text-xs bg-pir-accent text-white px-2 py-1 rounded hover:bg-pir-accent/90 transition-colors"
          >
            + New
          </button>
        </PermissionGate>
      </div>

      {/* Session list */}
      <div className="flex-1 overflow-y-auto">
        {sessions.length === 0 ? (
          <div className="p-4 text-center text-sm text-pir-text-muted">No sessions yet</div>
        ) : (
          <>
          {/* Hidden groups restore */}
          {hiddenGroups.size > 0 && (
            <div className="px-3 py-1.5 border-b border-pir">
              <details className="text-[11px]">
                <summary className="text-pir-text-muted cursor-pointer hover:text-pir-text-secondary flex items-center gap-1">
                  <svg className="w-3 h-3" viewBox="0 0 20 20" fill="currentColor">
                    <path d="M10 12a2 2 0 100-4 2 2 0 000 4z" />
                    <path fillRule="evenodd" d="M.458 10C1.732 5.943 5.522 3 10 3s8.268 2.943 9.542 7c-1.274 4.057-5.064 7-9.542 7S1.732 14.057.458 10zM14 10a4 4 0 11-8 0 4 4 0 018 0z" clipRule="evenodd" />
                  </svg>
                  {hiddenGroups.size} hidden group{hiddenGroups.size > 1 ? "s" : ""}
                </summary>
                <div className="mt-1 space-y-0.5">
                  {[...hiddenGroups].sort().map((name) => (
                    <button
                      key={name}
                      onClick={() => showGroup(name)}
                      className="w-full text-left px-2 py-0.5 text-[11px] text-pir-text-secondary hover:bg-pir-surface-1 rounded truncate"
                    >
                      Show {name || "Ungrouped"}
                    </button>
                  ))}
                </div>
              </details>
            </div>
          )}
          <DndContext sensors={sensors} collisionDetection={closestCenter} onDragEnd={handleDragEnd}>
            <SortableContext items={sessions.map((s) => s.name)} strategy={verticalListSortingStrategy}>
              {hasGroups ? (
                sortedGroupNames.filter((g) => !hiddenGroups.has(g)).map((groupName) => {
                  const groupSessions = groups.get(groupName) || [];
                  const isCollapsed = collapsedGroups.has(groupName);
                  const label = groupName || "Ungrouped";

                  // Aggregate KPIs for this group
                  const projectTotalCost = groupName ? (projectCosts.get(groupName) ?? 0) : 0;
                  const totalCpu = groupSessions.reduce((s, x) => s + (x.cpu_pct ?? 0), 0);
                  const totalRam = groupSessions.reduce((s, x) => s + (x.ram_mb ?? 0), 0);
                  const hasMetrics = groupSessions.some((x) => x.cpu_pct != null);

                  return (
                    <div key={groupName} className="group/grp">
                      <div className="w-full flex flex-col px-3 py-1.5 hover:bg-pir-surface-1/40">
                        <div className="flex items-center gap-1.5">
                          <button onClick={() => toggleGroup(groupName)} className="flex items-center gap-1.5 flex-1 min-w-0 text-left">
                            <ChevronIcon collapsed={isCollapsed} />
                            <span className="text-[11px] text-pir-text-muted uppercase tracking-wider truncate flex-1">{label}</span>
                          </button>
                          <span className="text-[10px] text-pir-text-tertiary">{groupSessions.length}</span>
                          <button
                            onClick={(e) => { e.stopPropagation(); hideGroup(groupName); }}
                            className="p-0.5 text-pir-text-tertiary hover:text-pir-text-secondary opacity-0 group-hover/grp:opacity-100 transition-opacity"
                            title={`Hide ${label}`}
                          >
                            <svg className="w-3 h-3" viewBox="0 0 20 20" fill="currentColor">
                              <path fillRule="evenodd" d="M3.707 2.293a1 1 0 00-1.414 1.414l14 14a1 1 0 001.414-1.414l-1.473-1.473A10.014 10.014 0 0019.542 10C18.268 5.943 14.478 3 10 3a9.958 9.958 0 00-4.512 1.074l-1.78-1.781zm4.261 4.26l1.514 1.515a2.003 2.003 0 012.45 2.45l1.514 1.514a4 4 0 00-5.478-5.478z" clipRule="evenodd" />
                              <path d="M12.454 16.697L9.75 13.992a4 4 0 01-3.742-3.741L2.335 6.578A9.98 9.98 0 00.458 10c1.274 4.057 5.065 7 9.542 7 .847 0 1.669-.105 2.454-.303z" />
                            </svg>
                          </button>
                        </div>
                        {hasMetrics && (
                          <button onClick={() => toggleGroup(groupName)} className="flex items-center gap-2 mt-0.5 pl-4 text-left w-full">
                            <span className={`flex items-center gap-0.5 text-[9px] font-mono ${cpuColor(totalCpu)}`}>
                              <CpuIcon />{totalCpu.toFixed(1)}%
                            </span>
                            <span className={`flex items-center gap-0.5 text-[9px] font-mono ${ramColor(totalRam)}`}>
                              <RamIcon />
                              {totalRam >= 1024 ? `${(totalRam / 1024).toFixed(1)}G` : `${Math.round(totalRam)}M`}
                            </span>
                            {projectTotalCost > 0 && (
                              <span className="text-[9px] font-mono text-pir-text-tertiary ml-auto">
                                ${projectTotalCost < 10 ? projectTotalCost.toFixed(2) : projectTotalCost.toFixed(0)}
                              </span>
                            )}
                          </button>
                        )}
                      </div>
                      {!isCollapsed && groupSessions.map(renderSession)}
                    </div>
                  );
                })
              ) : (
                sessions.map(renderSession)
              )}
            </SortableContext>
          </DndContext>
          </>
        )}
      </div>

      {/* Complete confirmation */}
      {confirmComplete && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60">
          <div className="bg-pir-surface-0 border border-pir rounded p-5 max-w-sm">
            <p className="text-sm font-semibold mb-2">
              Complete session <span className="font-mono">{confirmComplete}</span>?
            </p>
            <p className="text-xs text-pir-text-secondary mb-4">
              The session will be stopped and a final recap (cost, time, tokens) will be saved to the project.
            </p>
            <div className="flex gap-3 justify-end">
              <button
                onClick={() => setConfirmComplete(null)}
                className="px-3 py-1 text-sm text-pir-text-secondary hover:text-pir-text-primary"
              >
                Cancel
              </button>
              <button
                onClick={() => handleComplete(confirmComplete)}
                className="px-3 py-1 text-sm bg-green-600 text-white rounded hover:bg-green-700"
              >
                Complete
              </button>
            </div>
          </div>
        </div>
      )}

      {/* Kill confirmation (permanent delete) */}
      {confirmDelete && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60">
          <div className="bg-pir-surface-0 border border-pir rounded p-5 max-w-sm">
            <p className="text-sm font-semibold mb-2">
              Kill session <span className="font-mono">{confirmDelete}</span>?
            </p>
            <p className="text-xs text-pir-error/80 mb-4">
              This will permanently delete all session data. Cost history and recap will be lost. Use &quot;Complete Session&quot; to save a recap first.
            </p>
            <div className="flex gap-3 justify-end">
              <button
                onClick={() => setConfirmDelete(null)}
                className="px-3 py-1 text-sm text-pir-text-secondary hover:text-pir-text-primary"
              >
                Cancel
              </button>
              <button
                onClick={() => handleDelete(confirmDelete)}
                className="px-3 py-1 text-sm bg-pir-error text-white rounded hover:bg-pir-error/90"
              >
                Kill
              </button>
            </div>
          </div>
        </div>
      )}

      {/* Prompt modal for description */}
      {promptModal && promptModal.type === "description" && (
        <PromptModal
          title="Session Description"
          placeholder="What is this session for?"
          initialValue={promptModal.initial}
          onSubmit={handleDescriptionSubmit}
          onClose={() => setPromptModal(null)}
        />
      )}

      {/* Project selector modal */}
      {promptModal && promptModal.type === "project" && (
        <ProjectSelectorModal
          currentSlug={promptModal.initial}
          onSubmit={handleProjectSubmit}
          onClose={() => setPromptModal(null)}
        />
      )}

      {/* Create modal */}
      {showCreate && (
        <CreateSessionModal
          onClose={() => setShowCreate(false)}
          onCreated={handleCreated}
        />
      )}
    </aside>
  );
}
