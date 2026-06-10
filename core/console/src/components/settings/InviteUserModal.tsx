// v1.0.0 - 2026-03-13 - Invite user modal with email + role selection
"use client";

import { useState } from "react";
import { inviteWorkspaceUser } from "@/lib/api";
import { useWorkspace } from "@/lib/workspace";
import type { SystemRole } from "@/lib/types";

interface InviteUserModalProps {
  onClose: () => void;
  onInvited: () => void;
}

const ROLES: { value: SystemRole; label: string; desc: string }[] = [
  { value: "viewer", label: "Viewer", desc: "Read-only access" },
  { value: "operator", label: "Operator", desc: "Can run tasks and agents" },
  { value: "admin", label: "Admin", desc: "Manage users and settings" },
];

export default function InviteUserModal({ onClose, onInvited }: InviteUserModalProps) {
  const { workspaceId } = useWorkspace();
  const [email, setEmail] = useState("");
  const [role, setRole] = useState<SystemRole>("operator");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const emailValid = /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email);

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (!emailValid) return;
    setLoading(true);
    setError(null);

    try {
      await inviteWorkspaceUser(workspaceId, { email: email.trim(), role });
      onInvited();
      onClose();
    } catch (err) {
      const msg = err instanceof Error ? err.message : "Failed to invite user";
      setError(msg);
    } finally {
      setLoading(false);
    }
  }

  return (
    <div className="fixed inset-0 bg-black/60 backdrop-blur-sm z-50 flex items-center justify-center p-4">
      <div className="bg-pir-surface-1 border border-pir rounded-xl p-6 w-full max-w-md space-y-4 shadow-2xl">
        <div className="flex items-center justify-between">
          <h2 className="text-heading text-pir-text-primary">Invite User</h2>
          <button onClick={onClose} className="text-pir-text-muted hover:text-pir-text-primary transition-colors">
            <svg width="16" height="16" viewBox="0 0 16 16" fill="none">
              <path d="M4 4l8 8M12 4l-8 8" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
            </svg>
          </button>
        </div>

        {error && (
          <div className="bg-red-50 dark:bg-red-900/20 border border-red-200 dark:border-red-800 rounded px-3 py-2 text-xs text-red-700 dark:text-red-400">
            {error}
          </div>
        )}

        <form onSubmit={handleSubmit} className="space-y-4">
          <div>
            <label className="block text-sm text-pir-text-secondary mb-1">Email</label>
            <input
              type="email"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              placeholder="user@company.com"
              className="w-full bg-pir-base border border-pir rounded px-3 py-2 text-sm text-pir-text-primary focus:outline-none focus:border-pir-accent"
              autoFocus
              disabled={loading}
            />
          </div>

          <div>
            <label className="block text-sm text-pir-text-secondary mb-1">Role</label>
            <div className="space-y-1.5">
              {ROLES.map((r) => (
                <label
                  key={r.value}
                  className={`flex items-center gap-3 px-3 py-2 rounded border cursor-pointer transition-colors ${
                    role === r.value
                      ? "border-pir-accent bg-pir-accent/5"
                      : "border-pir hover:border-pir-accent/50"
                  }`}
                >
                  <input
                    type="radio"
                    name="role"
                    value={r.value}
                    checked={role === r.value}
                    onChange={() => setRole(r.value)}
                    className="accent-[var(--pir-accent)]"
                  />
                  <div>
                    <div className="text-sm font-medium text-pir-text-primary">{r.label}</div>
                    <div className="text-[10px] text-pir-text-muted">{r.desc}</div>
                  </div>
                </label>
              ))}
            </div>
          </div>

          <div className="flex gap-2 justify-end">
            <button
              type="button"
              onClick={onClose}
              className="px-4 py-2 text-sm text-pir-text-secondary hover:text-pir-text-primary transition-colors"
            >
              Cancel
            </button>
            <button
              type="submit"
              disabled={!emailValid || loading}
              className="px-4 py-2 bg-pir-accent text-white text-sm font-medium rounded hover:bg-pir-accent/90 disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
            >
              {loading ? "Inviting..." : "Send Invite"}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}
