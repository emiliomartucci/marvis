// v2.0.0 - 2026-03-09 - One-click RACI assign with team-scoped user filtering
"use client";

import { useEffect, useState, useRef } from "react";
import { getProjectRaci, addRaciEntry, removeRaciEntry, listUsers } from "@/lib/api";
import type { RaciEntry, RaciRole, User } from "@/lib/types";
import { PermissionGate } from "@/components/PermissionGate";
import { ErrorAlert } from "@/components/ui/ErrorAlert";

const RACI_ROLES: RaciRole[] = ["responsible", "accountable", "consulted", "informed"];

const ROLE_CONFIG: Record<RaciRole, { label: string; full: string; color: string; bg: string; border: string }> = {
  responsible: {
    label: "R",
    full: "Responsible",
    color: "text-blue-700 dark:text-blue-400",
    bg: "bg-blue-400/15",
    border: "border-blue-400/30",
  },
  accountable: {
    label: "A",
    full: "Accountable",
    color: "text-amber-700 dark:text-amber-400",
    bg: "bg-amber-400/15",
    border: "border-amber-400/30",
  },
  consulted: {
    label: "C",
    full: "Consulted",
    color: "text-purple-700 dark:text-purple-400",
    bg: "bg-purple-400/15",
    border: "border-purple-400/30",
  },
  informed: {
    label: "I",
    full: "Informed",
    color: "text-pir-text-muted",
    bg: "bg-pir-text-muted/10",
    border: "border-pir/50",
  },
};

function UserChip({
  entry,
  role,
  onRemove,
}: {
  entry: RaciEntry;
  role: RaciRole;
  onRemove: () => void;
}) {
  const cfg = ROLE_CONFIG[role];
  return (
    <div className="group/chip inline-flex items-center gap-1.5 rounded-full border px-2.5 py-1 transition-colors bg-pir-surface-2 border-pir hover:border-pir-strong">
      <div
        className="w-5 h-5 rounded-full flex-shrink-0 flex items-center justify-center text-[10px] font-bold text-white"
        style={{ backgroundColor: entry.user.avatar_color }}
      >
        {entry.user.display_name.charAt(0).toUpperCase()}
      </div>
      <span className="text-caption text-pir-text-primary">
        {entry.user.display_name}
      </span>
      <PermissionGate minRole="operator">
        <button
          onClick={onRemove}
          className="opacity-0 group-hover/chip:opacity-100 w-4 h-4 flex items-center justify-center rounded-full text-pir-text-muted hover:text-pir-error hover:bg-pir-error/10 transition-all text-[10px]"
          title="Remove"
        >
          X
        </button>
      </PermissionGate>
    </div>
  );
}

function AddUserDropdown({
  role,
  users,
  onSelect,
  onClose,
}: {
  role: RaciRole;
  users: User[];
  onSelect: (userId: string) => void;
  onClose: () => void;
}) {
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    function handleClickOutside(e: MouseEvent) {
      if (ref.current && !ref.current.contains(e.target as Node)) {
        onClose();
      }
    }
    document.addEventListener("mousedown", handleClickOutside);
    return () => document.removeEventListener("mousedown", handleClickOutside);
  }, [onClose]);

  if (users.length === 0) {
    return (
      <div
        ref={ref}
        className="absolute z-10 mt-1 w-52 bg-pir-surface-1 border border-pir rounded-lg shadow-lg p-3"
      >
        <div className="text-caption text-pir-text-muted">
          No available users
        </div>
      </div>
    );
  }

  return (
    <div
      ref={ref}
      className="absolute z-10 mt-1 w-52 bg-pir-surface-1 border border-pir rounded-lg shadow-lg overflow-hidden"
    >
      <div className="max-h-48 overflow-y-auto">
        {users.map((u) => (
          <button
            key={u.id}
            onClick={() => onSelect(u.id)}
            className="w-full flex items-center gap-2 px-3 py-2 text-left hover:bg-pir-surface-2 transition-colors"
          >
            <div
              className="w-5 h-5 rounded-full flex-shrink-0 flex items-center justify-center text-[10px] font-bold text-white"
              style={{ backgroundColor: u.avatar_color }}
            >
              {u.display_name.charAt(0).toUpperCase()}
            </div>
            <div className="min-w-0 flex-1">
              <div className="text-caption text-pir-text-primary truncate">
                {u.display_name}
              </div>
              <div className="text-[10px] text-pir-text-muted truncate">
                {u.type}
              </div>
            </div>
          </button>
        ))}
      </div>
    </div>
  );
}

export default function RaciTab({ slug }: { slug: string }) {
  const [entries, setEntries] = useState<RaciEntry[]>([]);
  const [users, setUsers] = useState<User[]>([]);
  const [loading, setLoading] = useState(true);
  const [openDropdown, setOpenDropdown] = useState<RaciRole | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    // Fetch team-scoped users for this project, with fallback to all users
    Promise.all([
      getProjectRaci(slug),
      listUsers({ project: slug }).then((teamUsers) =>
        teamUsers.length > 0 ? teamUsers : listUsers()
      ),
    ])
      .then(([raci, userList]) => {
        setEntries(raci);
        setUsers(userList);
      })
      .catch((e: unknown) =>
        setError(e instanceof Error ? e.message : "Failed to load RACI")
      )
      .finally(() => setLoading(false));
  }, [slug]);

  async function handleAssign(role: RaciRole, userId: string) {
    setError(null);
    setOpenDropdown(null);
    try {
      const updated = await addRaciEntry(slug, userId, role);
      setEntries(updated);
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : "Failed to assign");
    }
  }

  async function handleRemove(userId: string, role: RaciRole) {
    setError(null);
    try {
      await removeRaciEntry(slug, userId, role);
      setEntries((prev) =>
        prev.filter((e) => !(e.user.id === userId && e.role === role))
      );
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : "Failed to remove");
    }
  }

  if (loading) {
    return <div className="text-body text-pir-text-muted">Loading RACI...</div>;
  }

  const byRole: Record<RaciRole, RaciEntry[]> = {
    responsible: [],
    accountable: [],
    consulted: [],
    informed: [],
  };
  for (const e of entries) {
    byRole[e.role].push(e);
  }

  function usersForRole(role: RaciRole): User[] {
    const assigned = new Set(byRole[role].map((e) => e.user.id));
    return users.filter((u) => !assigned.has(u.id) && u.deleted_at === null);
  }

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <div>
          <h2 className="text-heading text-pir-text-primary">RACI</h2>
          <p className="text-body text-pir-text-muted mt-1">
            Project roles and responsibilities
          </p>
        </div>
      </div>

      {error && (
        <ErrorAlert message={error} />
      )}

      <div className="grid grid-cols-1 md:grid-cols-4 gap-3">
        {RACI_ROLES.map((role) => {
          const cfg = ROLE_CONFIG[role];
          const assigned = byRole[role];
          const available = usersForRole(role);
          const isOpen = openDropdown === role;

          return (
            <div
              key={role}
              className="bg-pir-surface-1 border border-pir rounded-lg p-4"
            >
              {/* Column header */}
              <div className="flex items-center gap-2 mb-3">
                <span
                  className={`w-7 h-7 rounded flex items-center justify-center text-sm font-bold ${cfg.bg} ${cfg.color} border ${cfg.border}`}
                >
                  {cfg.label}
                </span>
                <span className="text-label text-pir-text-primary">
                  {cfg.full}
                </span>
              </div>

              {/* Assigned chips */}
              <div className="space-y-2 mb-3">
                {assigned.length === 0 && (
                  <div className="text-caption text-pir-text-muted py-1">
                    No one assigned
                  </div>
                )}
                {assigned.map((entry) => (
                  <UserChip
                    key={entry.user.id}
                    entry={entry}
                    role={role}
                    onRemove={() => handleRemove(entry.user.id, role)}
                  />
                ))}
              </div>

              {/* Add button */}
              <PermissionGate minRole="operator">
                <div className="relative">
                  {available.length > 0 && (
                    <button
                      onClick={() =>
                        setOpenDropdown(isOpen ? null : role)
                      }
                      className={`w-8 h-8 rounded-full border-2 border-dashed flex items-center justify-center transition-colors ${
                        isOpen
                          ? "border-pir-accent text-pir-accent"
                          : "border-pir text-pir-text-muted hover:border-pir-accent hover:text-pir-accent"
                      }`}
                      title={`Add ${cfg.full}`}
                    >
                      +
                    </button>
                  )}
                  {isOpen && (
                    <AddUserDropdown
                      role={role}
                      users={available}
                      onSelect={(userId) => handleAssign(role, userId)}
                      onClose={() => setOpenDropdown(null)}
                    />
                  )}
                </div>
              </PermissionGate>
            </div>
          );
        })}
      </div>
    </div>
  );
}
