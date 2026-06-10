// v1.0.0 - 2026-02-28 - Task cost section: agent + human entries per task
"use client";

import { useEffect, useState } from "react";
import { getTaskCostEntries, createHumanCostEntry } from "@/lib/api";
import type { TaskCostSummary, TaskCostEntry } from "@/lib/types";
import { ErrorAlert } from "@/components/ui/ErrorAlert";

function fmtUsd(val: number): string {
  if (val === 0) return "$0.00";
  if (val < 0.01) return `$${val.toFixed(4)}`;
  return `$${val.toFixed(2)}`;
}

function fmtMinutes(mins: number): string {
  if (mins < 60) return `${mins}m`;
  const h = Math.floor(mins / 60);
  const m = Math.round(mins % 60);
  return m > 0 ? `${h}h ${m}m` : `${h}h`;
}

function fmtSeconds(secs: number): string {
  if (secs < 60) return `${secs}s`;
  return fmtMinutes(Math.round(secs / 60));
}

function timeAgo(dateStr: string): string {
  const now = Date.now();
  const then = new Date(dateStr).getTime();
  const diffMins = Math.floor((now - then) / 60000);
  if (diffMins < 60) return `${diffMins}m ago`;
  const diffHours = Math.floor(diffMins / 60);
  if (diffHours < 24) return `${diffHours}h ago`;
  return `${Math.floor(diffHours / 24)}d ago`;
}

function EntryRow({ entry }: { entry: TaskCostEntry }) {
  const isAgent = entry.entry_type === "agent";

  return (
    <div className="flex items-start gap-2 py-1.5 text-xs border-b border-pir last:border-0">
      <span
        className={`shrink-0 mt-0.5 rounded px-1.5 py-0.5 font-medium text-[10px] uppercase tracking-wide ${
          isAgent
            ? "bg-pir-accent/15 text-pir-accent"
            : "bg-pir-text-muted/15 text-pir-text-muted"
        }`}
      >
        {isAgent ? "Agent" : "Human"}
      </span>

      <div className="flex-1 min-w-0">
        {entry.description && (
          <p className="text-pir-text-secondary truncate mb-0.5">{entry.description}</p>
        )}
        <div className="flex flex-wrap gap-x-3 gap-y-0.5 text-pir-text-muted">
          {isAgent && entry.agent_seconds > 0 && (
            <span>agent time: {fmtSeconds(entry.agent_seconds)}</span>
          )}
          {!isAgent && entry.human_minutes > 0 && (
            <span>human time: {fmtMinutes(entry.human_minutes)}</span>
          )}
          <span>{timeAgo(entry.created_at)}</span>
          <span>by {entry.created_by}</span>
          {!entry.is_billable && (
            <span className="text-pir-warning">non-billable</span>
          )}
        </div>
      </div>

      <div className="shrink-0 text-right">
        <div className="text-pir-text-primary font-mono">{fmtUsd(entry.total_cost_usd)}</div>
        {entry.total_bill_usd !== entry.total_cost_usd && (
          <div className="text-pir-text-muted font-mono text-[10px]">
            bill {fmtUsd(entry.total_bill_usd)}
          </div>
        )}
      </div>
    </div>
  );
}

interface AddHumanFormProps {
  taskId: string;
  onSaved: (summary: TaskCostSummary) => void;
  onCancel: () => void;
}

function AddHumanForm({ taskId, onSaved, onCancel }: AddHumanFormProps) {
  const [minutes, setMinutes] = useState("");
  const [description, setDescription] = useState("");
  const [isBillable, setIsBillable] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    const mins = parseFloat(minutes);
    if (!mins || mins < 1) {
      setError("Minimum 1 minute");
      return;
    }
    setSaving(true);
    setError(null);
    try {
      const result = await createHumanCostEntry(taskId, {
        human_minutes: mins,
        description: description.trim() || undefined,
        is_billable: isBillable,
      });
      onSaved(result);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to save");
    } finally {
      setSaving(false);
    }
  }

  return (
    <form onSubmit={handleSubmit} className="border border-pir rounded p-3 bg-pir-surface-0 space-y-2.5 mt-2">
      <div className="flex gap-2">
        <div className="flex-1">
          <label className="text-[10px] text-pir-text-muted uppercase tracking-wide block mb-1">
            Minutes
          </label>
          <input
            type="number"
            min="1"
            max="1440"
            step="1"
            value={minutes}
            onChange={(e) => setMinutes(e.target.value)}
            placeholder="e.g. 30"
            className="w-full bg-pir-surface-1 border border-pir rounded px-2 py-1 text-xs text-pir-text-primary focus:outline-none focus:border-pir-accent/60"
            required
          />
        </div>
        <div className="flex-[2]">
          <label className="text-[10px] text-pir-text-muted uppercase tracking-wide block mb-1">
            Description (optional)
          </label>
          <input
            type="text"
            maxLength={200}
            value={description}
            onChange={(e) => setDescription(e.target.value)}
            placeholder="e.g. Code review"
            className="w-full bg-pir-surface-1 border border-pir rounded px-2 py-1 text-xs text-pir-text-primary focus:outline-none focus:border-pir-accent/60"
          />
        </div>
      </div>

      <label className="flex items-center gap-2 cursor-pointer select-none">
        <input
          type="checkbox"
          checked={isBillable}
          onChange={(e) => setIsBillable(e.target.checked)}
          className="accent-pir-accent"
        />
        <span className="text-xs text-pir-text-secondary">Billable</span>
      </label>

      {error && <ErrorAlert message={error} />}

      <div className="flex gap-2 justify-end">
        <button
          type="button"
          onClick={onCancel}
          className="px-3 py-1 text-xs text-pir-text-muted hover:text-pir-text-secondary rounded border border-pir hover:border-pir-text-muted/40 transition-colors"
        >
          Cancel
        </button>
        <button
          type="submit"
          disabled={saving}
          className="px-3 py-1 text-xs bg-pir-accent/90 hover:bg-pir-accent text-white rounded transition-colors disabled:opacity-50"
        >
          {saving ? "Saving..." : "Save"}
        </button>
      </div>
    </form>
  );
}

interface Props {
  taskId: string;
}

export default function TaskCostSection({ taskId }: Props) {
  const [summary, setSummary] = useState<TaskCostSummary | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [showEntries, setShowEntries] = useState(false);
  const [showAddForm, setShowAddForm] = useState(false);

  useEffect(() => {
    const controller = new AbortController();
    setLoading(true);
    setError(null);

    getTaskCostEntries(taskId)
      .then((data) => {
        if (!controller.signal.aborted) setSummary(data);
      })
      .catch((err) => {
        if (err.name !== "AbortError") {
          setError(err.message);
        }
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false);
      });

    return () => controller.abort();
  }, [taskId]);

  function handleSaved(updated: TaskCostSummary) {
    setSummary(updated);
    setShowAddForm(false);
    setShowEntries(true);
  }

  if (loading) {
    return (
      <div className="text-xs text-pir-text-muted animate-pulse">Loading costs...</div>
    );
  }

  if (error) {
    return (
      <div className="text-xs text-pir-error">Cost data unavailable</div>
    );
  }

  const hasCosts = summary && summary.entry_count > 0;

  return (
    <div>
      <div className="flex items-center justify-between mb-2">
        <h3 className="text-xs font-medium text-pir-text-primary">Cost</h3>
        <button
          type="button"
          onClick={() => setShowAddForm((v) => !v)}
          className="text-[10px] text-pir-text-muted hover:text-pir-accent transition-colors"
        >
          + Log time
        </button>
      </div>

      {hasCosts ? (
        <div className="border border-pir rounded p-3 bg-pir-surface-0 space-y-2">
          {/* Summary row */}
          <div className="flex items-center gap-4 text-xs">
            <div>
              <span className="text-pir-text-muted">Total cost </span>
              <span className="font-mono text-pir-text-primary">
                {fmtUsd(summary.total_cost_usd)}
              </span>
            </div>
            {summary.total_bill_usd !== summary.total_cost_usd && (
              <div>
                <span className="text-pir-text-muted">Billable </span>
                <span className="font-mono text-pir-text-primary">
                  {fmtUsd(summary.billable_usd)}
                </span>
              </div>
            )}
            <div className="ml-auto flex gap-3 text-pir-text-muted">
              {summary.agent_cost_usd > 0 && (
                <span>agent {fmtUsd(summary.agent_cost_usd)}</span>
              )}
              {summary.human_cost_usd > 0 && (
                <span>human {fmtUsd(summary.human_cost_usd)}</span>
              )}
            </div>
          </div>

          {/* Entries toggle */}
          {summary.entry_count > 0 && (
            <button
              type="button"
              onClick={() => setShowEntries((v) => !v)}
              className="text-[10px] text-pir-text-muted hover:text-pir-text-secondary transition-colors"
            >
              {showEntries ? "Hide" : "Show"} {summary.entry_count}{" "}
              {summary.entry_count === 1 ? "entry" : "entries"}
            </button>
          )}

          {showEntries && summary.entries.length > 0 && (
            <div className="mt-1">
              {summary.entries.map((e) => (
                <EntryRow key={e.id} entry={e} />
              ))}
            </div>
          )}
        </div>
      ) : (
        <div className="text-xs text-pir-text-muted italic">No cost entries yet.</div>
      )}

      {showAddForm && (
        <AddHumanForm
          taskId={taskId}
          onSaved={handleSaved}
          onCancel={() => setShowAddForm(false)}
        />
      )}
    </div>
  );
}
