// v1.1.0 - 2026-02-28 - Use permissions.canWrite to gate edit/delete actions
"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import CommentThread from "./CommentThread";
import PullRequestSection from "./PullRequestSection";
import TaskCostSection from "./TaskCostSection";
import CIChecksList from "@/components/triage/CIChecksList";
import { getMe, updateTask as apiUpdateTask, listUsers, getTask, getProjectRaci } from "@/lib/api";
import { API_BASE_URL } from "@/lib/config";
import type { TaskResponse, DelegationType, User, RaciEntry } from "@/lib/types";
import ScoreInput from "@/components/triage/ScoreInput";
import { useAuth } from "@/lib/auth";

type Task = TaskResponse;

// Template: "Devo {action} perché {problem}. Attenzione a {attention}."
//           "C'è bisogno di {action} perché {problem}. Attenzione a {attention}."
// Dir refs: lines starting with "-/" → path chips
const TEMPLATE_RE =
  /^(C'è bisogno(?:\s+di)?|Devo)\s+(.+?)\s+perch[eé]\s+(.+?)(?:\.\s*Attenzione a:?\s+(.+?))?\.?\s*$/i;
const DIR_REF_RE = /^-(\/.+)$/;

function parseDescription(text: string): {
  prefix: string;
  action: string;
  problem: string;
  attention: string | null;
  dirRefs: string[];
} | null {
  const lines = text.split("\n");
  const mainLines: string[] = [];
  const dirRefs: string[] = [];

  for (const line of lines) {
    const m = line.trim().match(DIR_REF_RE);
    if (m) dirRefs.push(m[1]);
    else if (line.trim()) mainLines.push(line.trim());
  }

  const main = mainLines.join(" ").trim();
  const m = main.match(TEMPLATE_RE);
  if (!m) return null;

  return {
    prefix: m[1],
    action: m[2],
    problem: m[3],
    attention: m[4] ?? null,
    dirRefs,
  };
}

function DirChip({ path }: { path: string }) {
  const [copied, setCopied] = useState(false);

  function handleCopy() {
    navigator.clipboard.writeText(path).then(() => {
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    });
  }

  return (
    <button
      type="button"
      onClick={handleCopy}
      title="Click to copy"
      className="inline-flex items-center gap-1.5 bg-pir-surface-0 border border-pir rounded px-2 py-0.5 font-mono text-[11px] text-pir-text-muted hover:text-pir-text-secondary hover:border-pir-accent/40 transition-colors"
    >
      <svg className="w-3 h-3 shrink-0" viewBox="0 0 12 12" fill="none" stroke="currentColor" strokeWidth="1.5">
        <path d="M2 2h4l2 2v6H2V2z" />
        <path d="M5 4h5l-1-1" />
      </svg>
      {copied ? "copied!" : path}
    </button>
  );
}

function DescriptionView({ description }: { description: string }) {
  const parsed = parseDescription(description);

  if (parsed) {
    const { prefix, action, problem, attention, dirRefs } = parsed;
    return (
      <div className="space-y-2.5">
        <p className="text-xs text-pir-text-secondary leading-relaxed">
          <span>{prefix} </span>
          <span className="text-pir-success font-semibold">{action}</span>
          <span> perché </span>
          <span className="text-pir-error font-semibold">{problem}</span>
          {attention && (
            <>
              <span className="text-pir-text-muted">. Attenzione a </span>
              <span className="text-pir-warning font-semibold">{attention}</span>
            </>
          )}
          <span>.</span>
        </p>
        {dirRefs.length > 0 && (
          <div className="flex flex-wrap gap-1.5">
            {dirRefs.map((p) => (
              <DirChip key={p} path={p} />
            ))}
          </div>
        )}
      </div>
    );
  }

  return (
    <p className="text-xs text-pir-text-secondary whitespace-pre-wrap leading-relaxed">
      {description}
    </p>
  );
}

const PRIORITY_COLORS: Record<string, string> = {
  high: "text-pir-error",
  medium: "text-pir-warning",
  low: "text-pir-text-muted",
};

const STATUS_COLORS: Record<string, string> = {
  pending: "bg-pir-text-muted/20 text-pir-text-muted",
  approved: "bg-pir-accent/20 text-pir-accent",
  in_progress: "bg-pir-warning/20 text-pir-warning",
  completed: "bg-pir-success/20 text-pir-success",
  failed: "bg-pir-error/20 text-pir-error",
};

const STATUS_OPTIONS = ["pending", "approved", "in_progress", "completed", "failed"];

// Mirror backend VALID_TRANSITIONS — only show reachable statuses in the dropdown
const VALID_TRANSITIONS: Record<string, string[]> = {
  pending: ["approved", "rejected"],
  approved: ["in_progress", "rejected"],
  in_progress: ["completed", "failed"],
  failed: ["approved"],
  completed: [],
  rejected: [],
};
const PRIORITY_OPTIONS = ["high", "medium", "low"];

interface Props {
  task: Task;
  slug: string;
  onClose: () => void;
  onStatusChange?: (taskId: string, newStatus: string) => void;
  onTaskUpdated?: (task: Task) => void;
  onTaskDeleted?: (taskId: string) => void;
}

export default function TaskDetailModal({
  task,
  slug,
  onClose,
  onStatusChange,
  onTaskUpdated,
  onTaskDeleted,
}: Props) {
  void slug;

  const { permissions } = useAuth();
  const [currentUser, setCurrentUser] = useState<string | null>(null);
  const [localTask, setLocalTask] = useState<Task>(task);
  const [responsible, setResponsible] = useState<RaciEntry | null>(null);

  // Edit mode state
  const [isEditing, setIsEditing] = useState(false);
  const [editTitle, setEditTitle] = useState(task.title);
  const [editDescription, setEditDescription] = useState(task.description ?? "");
  const [editPriority, setEditPriority] = useState<string>(task.priority);
  const [editTags, setEditTags] = useState(task.tags.join(", "));
  const [editOwnerId, setEditOwnerId] = useState<string | null>(task.owner_id ?? null);
  const [editUsers, setEditUsers] = useState<User[]>([]);
  const [isSaving, setIsSaving] = useState(false);

  // Delete confirmation state
  const [showDeleteConfirm, setShowDeleteConfirm] = useState(false);
  const [isDeleting, setIsDeleting] = useState(false);

  // Status change state
  const [isStatusSaving, setIsStatusSaving] = useState(false);

  // Scoring state (save-on-close)
  const [scoreImpact, setScoreImpact] = useState(task.impact);
  const [scoreConfidence, setScoreConfidence] = useState(task.confidence);
  const [scoreEase, setScoreEase] = useState(task.ease);
  const [scoreDelegation, setScoreDelegation] = useState(task.delegation);
  const scoreDirty = useRef(false);

  // Fetch full task detail (list endpoint omits description)
  useEffect(() => {
    const controller = new AbortController();
    getTask(task.id, { signal: controller.signal })
      .then((full) => {
        setLocalTask(full);
        setEditDescription(full.description ?? "");
      })
      .catch(() => {/* aborted or failed, keep list data */});
    return () => controller.abort();
  }, [task.id]);

  const computedIceScore =
    scoreImpact != null && scoreConfidence != null && scoreEase != null
      ? scoreImpact * scoreConfidence * scoreEase
      : null;

  const handleScoreChange = useCallback(
    (fields: { impact?: number | null; confidence?: number | null; ease?: number | null; delegation?: DelegationType | null }) => {
      if ("impact" in fields) setScoreImpact(fields.impact ?? null);
      if ("confidence" in fields) setScoreConfidence(fields.confidence ?? null);
      if ("ease" in fields) setScoreEase(fields.ease ?? null);
      if ("delegation" in fields) setScoreDelegation(fields.delegation ?? null);
      scoreDirty.current = true;
    },
    []
  );

  // Save scores on close if dirty
  const handleClose = useCallback(async () => {
    if (scoreDirty.current) {
      try {
        const updated = await apiUpdateTask(localTask.id, {
          impact: scoreImpact,
          confidence: scoreConfidence,
          ease: scoreEase,
          delegation: scoreDelegation,
        });
        onTaskUpdated?.(updated);
      } catch {
        // Silent fail — scores are best-effort
      }
    }
    onClose();
  }, [localTask.id, scoreImpact, scoreConfidence, scoreEase, scoreDelegation, onClose, onTaskUpdated]);

  useEffect(() => {
    getMe()
      .then((user) => setCurrentUser(user.username))
      .catch(() => {});
  }, []);

  // Fetch responsible from project RACI
  useEffect(() => {
    if (!localTask.project) return;
    getProjectRaci(localTask.project)
      .then((entries) => {
        const r = entries.find((e) => e.role === "responsible") ?? null;
        setResponsible(r);
      })
      .catch(() => {});
  }, [localTask.project]);

  function handleEditClick() {
    setEditTitle(localTask.title);
    setEditDescription(localTask.description ?? "");
    setEditPriority(localTask.priority);
    setEditTags(localTask.tags.join(", "));
    setEditOwnerId(localTask.owner_id ?? null);
    setIsEditing(true);
    // Load users for owner picker
    listUsers().then(setEditUsers).catch(() => {});
  }

  function handleCancelEdit() {
    setIsEditing(false);
  }

  async function handleSaveEdit() {
    setIsSaving(true);
    try {
      const parsedTags = editTags
        .split(",")
        .map((t) => t.trim())
        .filter(Boolean);

      const res = await fetch(`${API_BASE_URL}/api/v1/tasks/${localTask.id}`, {
        method: "PATCH",
        credentials: "include",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          title: editTitle,
          description: editDescription,
          priority: editPriority,
          tags: parsedTags,
          owner_id: editOwnerId,
        }),
      });

      if (res.ok) {
        const updated: Task = await res.json();
        setLocalTask(updated);
        setIsEditing(false);
        onTaskUpdated?.(updated);
      }
    } finally {
      setIsSaving(false);
    }
  }

  async function handleStatusChange(newStatus: string) {
    setIsStatusSaving(true);
    try {
      const res = await fetch(`${API_BASE_URL}/api/v1/tasks/${localTask.id}`, {
        method: "PATCH",
        credentials: "include",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ status: newStatus }),
      });

      if (res.ok) {
        setLocalTask((prev) => ({ ...prev, status: newStatus as Task["status"] }));
        onStatusChange?.(localTask.id, newStatus);
      }
    } finally {
      setIsStatusSaving(false);
    }
  }

  async function handleDeleteConfirm() {
    setIsDeleting(true);
    try {
      const res = await fetch(`${API_BASE_URL}/api/v1/tasks/${localTask.id}`, {
        method: "DELETE",
        credentials: "include",
      });

      if (res.ok) {
        onTaskDeleted?.(localTask.id);
        onClose();
      }
    } finally {
      setIsDeleting(false);
    }
  }

  const statusColorClass =
    STATUS_COLORS[localTask.status] ?? "bg-pir-surface-0 text-pir-text-muted";

  return (
    <div
      className="fixed inset-0 bg-black/60 flex items-center justify-center z-50 p-4"
      onClick={(e) => e.target === e.currentTarget && handleClose()}
    >
      <div className="bg-pir-surface-1 border border-pir rounded w-full max-w-lg max-h-[85vh] flex flex-col">
        {/* Header */}
        <div className="flex items-start justify-between px-5 py-4 border-b border-pir shrink-0">
          <div className="min-w-0 flex-1">
            {isEditing ? (
              <input
                className="w-full bg-pir-surface-0 border border-pir rounded px-2 py-1 text-sm font-semibold text-pir-text-primary focus:outline-none"
                value={editTitle}
                onChange={(e) => setEditTitle(e.target.value)}
                aria-label="Task title"
              />
            ) : (
              <h2 className="text-sm font-semibold text-pir-text-primary">
                {localTask.title}
              </h2>
            )}

            <div className="flex items-center gap-2 mt-1.5 flex-wrap">
              {/* Responsible badge from project RACI */}
              {responsible && (
                <span className="inline-flex items-center gap-1 text-[10px] px-2 py-0.5 rounded-full bg-blue-400/15 text-blue-400 border border-blue-400/30">
                  <span
                    className="w-3.5 h-3.5 rounded-full flex-shrink-0 flex items-center justify-center text-[7px] font-bold text-white"
                    style={{ backgroundColor: responsible.user.avatar_color }}
                  >
                    {responsible.user.display_name.charAt(0).toUpperCase()}
                  </span>
                  {responsible.user.display_name}
                </span>
              )}
              {/* Status: dropdown with current + valid transitions only */}
              {(() => {
                const reachable = VALID_TRANSITIONS[localTask.status] || [];
                const options = [localTask.status, ...reachable];
                return (
                  <select
                    className={`text-[10px] px-2 py-0.5 rounded-full border-0 cursor-pointer ${statusColorClass} bg-transparent`}
                    value={localTask.status}
                    disabled={isStatusSaving || reachable.length === 0 || !permissions.canWrite}
                    onChange={(e) => handleStatusChange(e.target.value)}
                    aria-label="Task status"
                  >
                    {options.map((s) => (
                      <option key={s} value={s}>
                        {s}
                      </option>
                    ))}
                  </select>
                );
              })()}

              {/* Quick Approve button for pending tasks — operator+ only */}
              {localTask.status === "pending" && !isEditing && permissions.canWrite && (
                <button
                  onClick={() => handleStatusChange("approved")}
                  disabled={isStatusSaving}
                  className="text-[10px] px-2 py-0.5 rounded-full bg-pir-accent/20 text-pir-accent hover:bg-pir-accent/30 disabled:opacity-50 transition-colors"
                  aria-label="Approve task"
                >
                  {isStatusSaving ? "..." : "Approve"}
                </button>
              )}

              {isEditing ? (
                <select
                  className="text-[10px] font-medium bg-pir-surface-0 border border-pir rounded px-1 py-0.5 text-pir-text-primary focus:outline-none"
                  value={editPriority}
                  onChange={(e) => setEditPriority(e.target.value)}
                  aria-label="Task priority"
                >
                  {PRIORITY_OPTIONS.map((p) => (
                    <option key={p} value={p}>
                      {p}
                    </option>
                  ))}
                </select>
              ) : (
                <span
                  className={`text-[10px] font-medium ${PRIORITY_COLORS[localTask.priority] ?? "text-pir-text-muted"}`}
                >
                  {localTask.priority}
                </span>
              )}
            </div>
          </div>

          <div className="flex items-center gap-2 ml-3 shrink-0">
            {!isEditing && permissions.canWrite && (
              <button
                onClick={handleEditClick}
                className="text-xs text-pir-text-muted hover:text-pir-text-secondary px-2 py-1 border border-pir rounded"
                aria-label="Edit task"
              >
                Edit
              </button>
            )}
            {isEditing && (
              <>
                <button
                  onClick={handleSaveEdit}
                  disabled={isSaving}
                  className="text-xs text-pir-text-primary px-2 py-1 border border-pir rounded hover:bg-pir-surface-2 disabled:opacity-50"
                  aria-label="Save task"
                >
                  {isSaving ? "Saving..." : "Save"}
                </button>
                <button
                  onClick={handleCancelEdit}
                  className="text-xs text-pir-text-muted px-2 py-1 border border-pir rounded hover:bg-pir-surface-2"
                  aria-label="Cancel edit"
                >
                  Cancel
                </button>
              </>
            )}
            {/* View in Graph — feature flagged */}
            {process.env.NEXT_PUBLIC_ENABLE_GRAPH_UX === "true" && (
              <a
                href={`/graph/?id=task:artifact:${localTask.id}&view=list&tab=context`}
                title="View in Knowledge Graph"
                className="text-caption text-pir-text-muted hover:text-pir-kg-node-task transition-colors px-1.5 py-0.5 border border-pir rounded"
              >
                KG
              </a>
            )}
            <button
              onClick={handleClose}
              className="text-pir-text-muted hover:text-pir-text-secondary text-lg leading-none"
              aria-label="Close"
            >
              &times;
            </button>
          </div>
        </div>

        {/* Body */}
        <div className="flex-1 overflow-y-auto px-5 py-4 space-y-4">
          {/* Description */}
          {isEditing ? (
            <div>
              <label className="text-label text-pir-text-tertiary block mb-1">
                Description
              </label>
              <textarea
                className="w-full bg-pir-surface-0 border border-pir rounded px-2 py-1 text-sm text-pir-text-primary focus:outline-none resize-none"
                rows={5}
                value={editDescription}
                onChange={(e) => setEditDescription(e.target.value)}
                aria-label="Task description"
              />
            </div>
          ) : (
            localTask.description && (
              <div>
                <label className="text-label text-pir-text-tertiary block mb-1.5">
                  Description
                </label>
                <DescriptionView description={localTask.description} />
              </div>
            )
          )}

          {/* Meta */}
          <div className="flex items-center gap-4 text-xs text-pir-text-muted">
            {localTask.owner && (
              <div className="flex items-center gap-1.5">
                <span className="text-pir-text-tertiary mr-1">Owner:</span>
                <div
                  className="w-4 h-4 rounded-full flex-shrink-0"
                  style={{ backgroundColor: localTask.owner.avatar_color }}
                />
                <span className="text-pir-text-secondary">{localTask.owner.display_name}</span>
              </div>
            )}
            <div>
              <span className="text-pir-text-tertiary mr-1">Created by:</span>
              <span className="text-pir-text-secondary">{localTask.created_by}</span>
            </div>
          </div>

          {/* Tags */}
          {isEditing ? (
            <div className="space-y-3">
              <div>
                <label className="text-label text-pir-text-tertiary block mb-1">
                  Owner
                </label>
                <select
                  className="w-full bg-pir-surface-0 border border-pir rounded px-2 py-1 text-sm text-pir-text-primary focus:outline-none"
                  value={editOwnerId ?? ""}
                  onChange={(e) => setEditOwnerId(e.target.value || null)}
                  aria-label="Task owner"
                >
                  <option value="">— nessuno —</option>
                  {editUsers.map((u) => (
                    <option key={u.id} value={u.id}>
                      {u.display_name} ({u.type})
                    </option>
                  ))}
                </select>
              </div>
              <div>
                <label className="text-label text-pir-text-tertiary block mb-1">
                  Tags (comma-separated)
                </label>
                <input
                  className="w-full bg-pir-surface-0 border border-pir rounded px-2 py-1 text-sm text-pir-text-primary focus:outline-none"
                  value={editTags}
                  onChange={(e) => setEditTags(e.target.value)}
                  aria-label="Task tags"
                  placeholder="tag1, tag2, tag3"
                />
              </div>
            </div>
          ) : (
            localTask.tags.length > 0 && (
              <div className="flex flex-wrap gap-1">
                {localTask.tags.map((tag) => (
                  <span
                    key={tag}
                    className="text-[10px] bg-pir-surface-0 border border-pir px-2 py-0.5 rounded text-pir-text-muted"
                  >
                    {tag}
                  </span>
                ))}
              </div>
            )
          )}

          {/* Scoring */}
          <div className="border border-pir rounded p-3 bg-pir-surface-0">
            <ScoreInput
              impact={scoreImpact}
              confidence={scoreConfidence}
              ease={scoreEase}
              delegation={scoreDelegation}
              iceScore={computedIceScore}
              onChange={handleScoreChange}
            />
          </div>

          {/* Pull Request */}
          <PullRequestSection
            taskId={localTask.id}
            onTaskCompleted={() => {
              setLocalTask((prev) => ({ ...prev, status: "completed" }));
              onStatusChange?.(localTask.id, "completed");
            }}
          />

          {/* CI Status */}
          <CIChecksList taskId={localTask.id} hasPr={!!localTask.pr_status} />

          {/* Cost */}
          <TaskCostSection taskId={localTask.id} />

          {/* Comments */}
          <div>
            <h3 className="text-xs font-medium text-pir-text-primary mb-2">Comments</h3>
            {currentUser ? (
              <CommentThread
                targetType="task"
                targetId={localTask.id}
                currentUser={currentUser}
              />
            ) : (
              <div className="text-xs text-pir-text-muted">Loading...</div>
            )}
          </div>
        </div>

        {/* Delete section — operator+ only */}
        {permissions.canWrite && (
          <div className="border-t border-pir px-5 py-3 shrink-0">
            {showDeleteConfirm ? (
              <div className="flex items-center gap-3">
                <span className="text-caption text-pir-text-secondary flex-1">
                  Delete this task?
                </span>
                <button
                  onClick={handleDeleteConfirm}
                  disabled={isDeleting}
                  className="text-xs px-3 py-1 rounded border border-pir-error text-pir-error hover:bg-pir-error/10 disabled:opacity-50"
                  aria-label="Confirm delete"
                >
                  {isDeleting ? "Deleting..." : "Confirm"}
                </button>
                <button
                  onClick={() => setShowDeleteConfirm(false)}
                  className="text-xs px-3 py-1 rounded border border-pir text-pir-text-muted hover:bg-pir-surface-2"
                  aria-label="Cancel delete"
                >
                  Cancel
                </button>
              </div>
            ) : (
              <button
                onClick={() => setShowDeleteConfirm(true)}
                className="text-xs text-pir-text-muted hover:text-pir-error"
                aria-label="Delete task"
              >
                Delete task
              </button>
            )}
          </div>
        )}
      </div>
    </div>
  );
}
