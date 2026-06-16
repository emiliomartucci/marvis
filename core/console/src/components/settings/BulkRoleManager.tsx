// v1.0.0 - 2026-03-13 - Bulk role change with multi-select and confirmation
"use client";

import { useState } from "react";
import { updateUserRole } from "@/lib/api";
import type { User, SystemRole } from "@/lib/types";

interface BulkRoleManagerProps {
  users: User[];
  onComplete: () => void;
}

const ROLES: SystemRole[] = ["viewer", "operator", "admin"];

export default function BulkRoleManager({ users, onComplete }: BulkRoleManagerProps) {
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [targetRole, setTargetRole] = useState<SystemRole | null>(null);
  const [processing, setProcessing] = useState(false);
  const [progress, setProgress] = useState({ done: 0, total: 0 });
  const [error, setError] = useState<string | null>(null);

  function toggleUser(id: string) {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }

  function toggleAll() {
    if (selected.size === users.length) {
      setSelected(new Set());
    } else {
      setSelected(new Set(users.map((u) => u.id)));
    }
  }

  async function handleBulkChange() {
    if (!targetRole || selected.size === 0) return;

    setProcessing(true);
    setError(null);
    setProgress({ done: 0, total: selected.size });

    const ids = Array.from(selected);
    for (let i = 0; i < ids.length; i++) {
      try {
        await updateUserRole(ids[i], targetRole);
        setProgress({ done: i + 1, total: ids.length });
      } catch (err) {
        const msg = err instanceof Error ? err.message : "Failed to update role";
        setError(`Failed on user ${i + 1}/${ids.length}: ${msg}`);
        setProcessing(false);
        return;
      }
    }

    setProcessing(false);
    setSelected(new Set());
    setTargetRole(null);
    onComplete();
  }

  if (users.length === 0) return null;

  return (
    <div className="space-y-3">
      {/* Toolbar */}
      <div className="flex items-center gap-3 flex-wrap">
        <label className="flex items-center gap-1.5 text-xs text-pir-text-secondary cursor-pointer">
          <input
            type="checkbox"
            checked={selected.size === users.length}
            onChange={toggleAll}
            className="accent-[var(--pir-accent)]"
            disabled={processing}
          />
          Select all ({users.length})
        </label>

        {selected.size > 0 && (
          <>
            <span className="text-xs text-pir-text-muted">{selected.size} selected</span>
            <select
              value={targetRole ?? ""}
              onChange={(e) => setTargetRole(e.target.value as SystemRole)}
              className="bg-pir-surface-0 border border-pir rounded px-2 py-1 text-xs text-pir-text-secondary focus:outline-none focus:border-pir-accent"
              disabled={processing}
            >
              <option value="">Change role to...</option>
              {ROLES.map((r) => (
                <option key={r} value={r}>{r}</option>
              ))}
            </select>
            <button
              onClick={handleBulkChange}
              disabled={!targetRole || processing}
              className="px-3 py-1 bg-pir-accent text-white text-xs rounded hover:bg-pir-accent/90 disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
            >
              {processing ? `Updating ${progress.done}/${progress.total}...` : "Apply"}
            </button>
          </>
        )}
      </div>

      {error && (
        <div className="text-xs text-red-600 dark:text-red-400">{error}</div>
      )}

      {/* Processing overlay */}
      {processing && (
        <div className="bg-pir-surface-0/80 rounded p-2 text-center text-xs text-pir-text-muted">
          Updating roles... {progress.done}/{progress.total}
        </div>
      )}

      {/* User list with checkboxes */}
      <div className="space-y-1">
        {users.map((user) => (
          <label
            key={user.id}
            className={`flex items-center gap-3 px-3 py-2 rounded border transition-colors cursor-pointer ${
              selected.has(user.id)
                ? "border-pir-accent/50 bg-pir-accent/5"
                : "border-transparent hover:bg-pir-surface-1/50"
            } ${processing ? "pointer-events-none opacity-60" : ""}`}
          >
            <input
              type="checkbox"
              checked={selected.has(user.id)}
              onChange={() => toggleUser(user.id)}
              className="accent-[var(--pir-accent)]"
              disabled={processing}
            />
            <span className="flex-1 text-xs text-pir-text-primary">{user.display_name}</span>
            <span className="text-[10px] text-pir-text-muted">{user.email}</span>
            <span className="text-[10px] text-pir-text-muted">{user.system_role}</span>
          </label>
        ))}
      </div>
    </div>
  );
}
