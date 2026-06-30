"use client";

// Brain v1.2 — DirectionDiff side-by-side review component.
//
// Shows current direction (frontmatter / DB cache) vs proposed direction
// (latest pending finding). Operator can Approve, Edit (open inline textareas),
// or Reject. All mutations route through /api/v1/brain/findings/{id}/* and
// require super_admin role server-side (UI surfaces 403 as a banner).
//
// Theme tokens: industrial v2 (anthracite + Riddim orange + bone). All colors
// via var(--pir-*) — never hex hardcoded.

import { useState } from "react";

/** @public */
export type DirectionPair = {
  current: { summary: string; out_of_scope: string } | null;
  proposed: { summary: string; out_of_scope: string };
};

/** @public */
export interface DirectionDiffProps {
  findingId: string;
  projectSlug: string;
  pair: DirectionPair;
  urgencyScore?: number;
  confidence?: "low" | "medium" | "high";
  onApproved?: (findingId: string) => void;
  onRejected?: (findingId: string) => void;
  onEdited?: (findingId: string) => void;
}

type Mode = "view" | "edit";
type Status = "idle" | "saving" | "error";

const fetcher = async (path: string, init?: RequestInit) => {
  const resp = await fetch(path, {
    ...init,
    credentials: "include",
    headers: { "Content-Type": "application/json", ...(init?.headers || {}) },
  });
  if (!resp.ok) {
    const text = await resp.text();
    throw new Error(`${resp.status} ${text || resp.statusText}`);
  }
  return resp.json();
};

const urgencyBadge = (score?: number) => {
  if (score == null) return null;
  if (score >= 7) return { label: "alta", emoji: "🔥", className: "text-[var(--pir-danger)]" };
  if (score >= 4) return { label: "media", emoji: "⚠️", className: "text-[var(--pir-warning)]" };
  return { label: "bassa", emoji: "🔹", className: "text-[var(--pir-text-muted)]" };
};

/** @public */
export function DirectionDiff(props: DirectionDiffProps) {
  const { findingId, projectSlug, pair } = props;
  const [mode, setMode] = useState<Mode>("view");
  const [editSummary, setEditSummary] = useState(pair.proposed.summary);
  const [editOos, setEditOos] = useState(pair.proposed.out_of_scope);
  const [status, setStatus] = useState<Status>("idle");
  const [errorMsg, setErrorMsg] = useState<string | null>(null);

  const badge = urgencyBadge(props.urgencyScore);

  const handleApprove = async () => {
    setStatus("saving");
    setErrorMsg(null);
    try {
      await fetcher(`/api/v1/brain/findings/${findingId}/approve`, { method: "POST" });
      props.onApproved?.(findingId);
      setStatus("idle");
    } catch (err) {
      setStatus("error");
      setErrorMsg(err instanceof Error ? err.message : "approve failed");
    }
  };

  const handleReject = async () => {
    setStatus("saving");
    setErrorMsg(null);
    try {
      await fetcher(`/api/v1/brain/findings/${findingId}/reject`, { method: "POST" });
      props.onRejected?.(findingId);
      setStatus("idle");
    } catch (err) {
      setStatus("error");
      setErrorMsg(err instanceof Error ? err.message : "reject failed");
    }
  };

  const handleEditSave = async () => {
    if (!editSummary.trim() || !editOos.trim()) {
      setErrorMsg("summary and out_of_scope must not be empty");
      setStatus("error");
      return;
    }
    setStatus("saving");
    setErrorMsg(null);
    try {
      await fetcher(`/api/v1/brain/findings/${findingId}/edit`, {
        method: "POST",
        body: JSON.stringify({
          edited_summary: editSummary,
          edited_out_of_scope: editOos,
        }),
      });
      props.onEdited?.(findingId);
      setMode("view");
      setStatus("idle");
    } catch (err) {
      setStatus("error");
      setErrorMsg(err instanceof Error ? err.message : "edit failed");
    }
  };

  return (
    <article
      data-testid="direction-diff"
      className="rounded border border-[var(--pir-border)] bg-[var(--pir-surface)] p-4"
    >
      <header className="mb-3 flex items-center justify-between gap-3">
        <div className="flex items-center gap-2 font-mono text-sm">
          <span className="text-[var(--pir-text)]">{projectSlug}</span>
          {badge && (
            <span className={`text-xs ${badge.className}`} aria-label={`urgency ${badge.label}`}>
              {badge.emoji} {props.urgencyScore}
            </span>
          )}
          {props.confidence && (
            <span className="text-xs uppercase text-[var(--pir-text-muted)]">
              conf: {props.confidence}
            </span>
          )}
        </div>
      </header>

      <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
        <section aria-label="current direction">
          <h3 className="mb-1 text-xs uppercase tracking-wider text-[var(--pir-text-muted)]">
            Current
          </h3>
          <div className="rounded border border-[var(--pir-border)] bg-[var(--pir-surface-alt)] p-3 text-sm">
            {pair.current ? (
              <>
                <p className="mb-2 whitespace-pre-wrap">{pair.current.summary}</p>
                <p className="text-xs italic text-[var(--pir-text-muted)]">
                  {pair.current.out_of_scope}
                </p>
              </>
            ) : (
              <p className="italic text-[var(--pir-text-muted)]">
                Nessuna direction esistente (bootstrap).
              </p>
            )}
          </div>
        </section>

        <section aria-label="proposed direction">
          <h3 className="mb-1 text-xs uppercase tracking-wider text-[var(--pir-accent)]">
            Proposed
          </h3>
          <div className="rounded border border-[var(--pir-accent)] bg-[var(--pir-surface-alt)] p-3 text-sm">
            {mode === "view" ? (
              <>
                <p className="mb-2 whitespace-pre-wrap">{pair.proposed.summary}</p>
                <p className="text-xs italic text-[var(--pir-text-muted)]">
                  {pair.proposed.out_of_scope}
                </p>
              </>
            ) : (
              <div className="flex flex-col gap-2">
                <textarea
                  data-testid="edit-summary"
                  className="min-h-[120px] w-full rounded border border-[var(--pir-border)] bg-[var(--pir-surface)] p-2 text-sm font-mono"
                  value={editSummary}
                  onChange={(e) => setEditSummary(e.target.value)}
                  aria-label="edited summary"
                />
                <textarea
                  data-testid="edit-oos"
                  className="min-h-[60px] w-full rounded border border-[var(--pir-border)] bg-[var(--pir-surface)] p-2 text-sm font-mono"
                  value={editOos}
                  onChange={(e) => setEditOos(e.target.value)}
                  aria-label="edited out of scope"
                />
              </div>
            )}
          </div>
        </section>
      </div>

      {errorMsg && (
        <p
          role="alert"
          className="mt-3 text-xs text-[var(--pir-danger)]"
          data-testid="direction-diff-error"
        >
          {errorMsg}
        </p>
      )}

      <footer className="mt-4 flex justify-end gap-2">
        {mode === "view" ? (
          <>
            <button
              type="button"
              className="rounded border border-[var(--pir-border)] px-3 py-1 text-sm text-[var(--pir-text)] hover:bg-[var(--pir-surface-alt)]"
              onClick={() => setMode("edit")}
              disabled={status === "saving"}
            >
              Edit
            </button>
            <button
              type="button"
              className="rounded border border-[var(--pir-border)] px-3 py-1 text-sm text-[var(--pir-text-muted)] hover:bg-[var(--pir-surface-alt)]"
              onClick={handleReject}
              disabled={status === "saving"}
            >
              Reject
            </button>
            <button
              type="button"
              className="rounded bg-[var(--pir-accent)] px-3 py-1 text-sm font-semibold text-[var(--pir-on-accent)] hover:opacity-90"
              onClick={handleApprove}
              disabled={status === "saving"}
              data-testid="direction-diff-approve"
            >
              Approve
            </button>
          </>
        ) : (
          <>
            <button
              type="button"
              className="rounded border border-[var(--pir-border)] px-3 py-1 text-sm text-[var(--pir-text-muted)]"
              onClick={() => {
                setMode("view");
                setEditSummary(pair.proposed.summary);
                setEditOos(pair.proposed.out_of_scope);
                setErrorMsg(null);
              }}
              disabled={status === "saving"}
            >
              Cancel
            </button>
            <button
              type="button"
              className="rounded bg-[var(--pir-accent)] px-3 py-1 text-sm font-semibold text-[var(--pir-on-accent)] hover:opacity-90"
              onClick={handleEditSave}
              disabled={status === "saving"}
              data-testid="direction-diff-save"
            >
              Save edit
            </button>
          </>
        )}
      </footer>
    </article>
  );
}
