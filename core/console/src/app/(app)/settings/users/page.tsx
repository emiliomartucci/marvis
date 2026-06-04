"use client";

import { useEffect, useRef, useState } from "react";
import { listUsers, createUser, deleteUser, updateUser, issueResetToken, getTeams, addTeamMember } from "@/lib/api";
import type { User, UserCreateRequest, Team } from "@/lib/types";
import { PermissionGate } from "@/components/PermissionGate";

const AVATAR_COLORS = [
  "#6366f1", "#8b5cf6", "#ec4899", "#ef4444",
  "#f97316", "#eab308", "#22c55e", "#06b6d4",
];

const ROLE_CONFIG: Record<string, { className: string }> = {
  super_admin: { className: "bg-red-500/20 text-red-400 border-red-500/30" },
  admin:       { className: "bg-orange-500/20 text-orange-400 border-orange-500/30" },
  operator:    { className: "bg-pir-accent/20 text-pir-accent border-pir-accent/30" },
  viewer:      { className: "bg-pir-surface-3 text-pir-text-muted border-pir" },
};

type FilterType = "all" | "human" | "agent";

function UserAvatar({ color, initial }: { color: string; initial: string }) {
  return (
    <div
      className="w-8 h-8 rounded-full flex items-center justify-center flex-shrink-0 text-white font-semibold text-sm"
      style={{ backgroundColor: color }}
    >
      {initial}
    </div>
  );
}

function RoleBadge({ role }: { role: string }) {
  const cfg = ROLE_CONFIG[role] ?? { className: "bg-pir-surface-3 text-pir-text-muted border-pir" };
  return (
    <span className={`text-caption px-2 py-0.5 rounded border ${cfg.className}`}>
      {role}
    </span>
  );
}

function TypeBadge({ type }: { type: "human" | "agent" }) {
  if (type === "agent") {
    return (
      <span className="inline-flex items-center gap-1 text-caption px-2 py-0.5 rounded border bg-pir-accent/20 text-pir-accent border-pir-accent/30">
        ⚡ agent
      </span>
    );
  }
  return (
    <span className="inline-flex items-center gap-1 text-caption px-2 py-0.5 rounded border bg-pir-surface-2 text-pir-text-secondary border-pir">
      👤 human
    </span>
  );
}

function ColorPicker({ value, onChange }: { value: string; onChange: (c: string) => void }) {
  return (
    <div className="flex gap-2 flex-wrap">
      {AVATAR_COLORS.map((color) => (
        <button
          key={color}
          type="button"
          onClick={() => onChange(color)}
          className={`w-7 h-7 rounded-full border-2 transition-all ${
            value === color ? "border-white scale-110" : "border-transparent opacity-70 hover:opacity-100"
          }`}
          style={{ backgroundColor: color }}
        />
      ))}
    </div>
  );
}

function EditUserModal({
  user,
  onClose,
  onUpdated,
}: {
  user: User;
  onClose: () => void;
  onUpdated: (u: User) => void;
}) {
  const [form, setForm] = useState({
    display_name: user.display_name,
    email: user.email ?? "",
    avatar_color: user.avatar_color,
    telegram_chat_id: user.telegram_chat_id ?? "",
    system_role: user.system_role,
  });
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const previewInitial = (form.display_name.charAt(0) || user.display_name.charAt(0)).toUpperCase();

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (!form.display_name.trim()) return;
    setLoading(true);
    setError(null);
    try {
      const payload: Partial<UserCreateRequest> = {
        display_name: form.display_name.trim(),
        avatar_color: form.avatar_color,
        system_role: form.system_role,
      };
      if (user.type === "human") {
        payload.email = form.email.trim() || null;
      }
      if (user.type === "agent") {
        payload.telegram_chat_id = form.telegram_chat_id.trim() || null;
      }
      const updated = await updateUser(user.id, payload);
      onUpdated(updated);
      onClose();
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : "Errore aggiornamento");
    } finally {
      setLoading(false);
    }
  }

  return (
    <div className="fixed inset-0 bg-black/60 z-50 flex items-center justify-center p-4">
      <div className="bg-pir-surface-1 border border-pir rounded-lg p-6 w-full max-w-md space-y-4">
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-3">
            <UserAvatar color={form.avatar_color} initial={previewInitial} />
            <div>
              <h2 className="text-heading text-pir-text-primary">Modifica Utente</h2>
              <p className="text-caption text-pir-text-muted">{user.slug}</p>
            </div>
          </div>
          <button onClick={onClose} className="text-pir-text-muted hover:text-pir-text-primary transition-colors">✕</button>
        </div>

        {error && (
          <div className="text-caption text-pir-error bg-pir-error/10 rounded px-3 py-2">{error}</div>
        )}

        <form onSubmit={handleSubmit} className="space-y-4">
          <div>
            <label className="text-label text-pir-text-secondary block mb-1">Nome Display *</label>
            <input
              className="w-full bg-pir-surface-2 border border-pir rounded px-3 py-2 text-body text-pir-text-primary focus:outline-none focus:border-pir-strong"
              value={form.display_name}
              onChange={(e) => setForm((f) => ({ ...f, display_name: e.target.value }))}
              required
            />
          </div>

          {user.type === "human" && (
            <div>
              <label className="text-label text-pir-text-secondary block mb-1">Email</label>
              <input
                type="email"
                className="w-full bg-pir-surface-2 border border-pir rounded px-3 py-2 text-body text-pir-text-primary focus:outline-none focus:border-pir-strong"
                value={form.email}
                onChange={(e) => setForm((f) => ({ ...f, email: e.target.value }))}
                placeholder="email@example.com"
              />
            </div>
          )}

          {user.type === "agent" && (
            <div>
              <label className="text-label text-pir-text-secondary block mb-1">Telegram Chat ID</label>
              <input
                className="w-full bg-pir-surface-2 border border-pir rounded px-3 py-2 text-body text-pir-text-primary focus:outline-none focus:border-pir-strong"
                value={form.telegram_chat_id}
                onChange={(e) => setForm((f) => ({ ...f, telegram_chat_id: e.target.value }))}
                placeholder="231184812"
              />
            </div>
          )}

          <div>
            <label className="text-label text-pir-text-secondary block mb-1">Ruolo (system_role)</label>
            <select
              className="w-full bg-pir-surface-2 border border-pir rounded px-3 py-2 text-body text-pir-text-primary focus:outline-none focus:border-pir-strong"
              value={form.system_role}
              onChange={(e) => setForm((f) => ({ ...f, system_role: e.target.value }))}
            >
              {["viewer", "operator", "admin", "super_admin"].map((r) => (
                <option key={r} value={r}>{r}</option>
              ))}
            </select>
          </div>

          <div>
            <label className="text-label text-pir-text-secondary block mb-1">Avatar</label>
            <ColorPicker value={form.avatar_color} onChange={(c) => setForm((f) => ({ ...f, avatar_color: c }))} />
          </div>

          <div className="flex gap-3 pt-2">
            <button
              type="button"
              onClick={onClose}
              className="flex-1 px-4 py-2 border border-pir rounded text-body text-pir-text-secondary hover:text-pir-text-primary transition-colors"
            >
              Annulla
            </button>
            <button
              type="submit"
              disabled={loading || !form.display_name.trim()}
              className="flex-1 px-4 py-2 bg-pir-accent rounded text-body text-white disabled:opacity-50 hover:opacity-90 transition-opacity"
            >
              {loading ? "Salvando..." : "Salva"}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}

function CreateUserModal({
  onClose,
  onCreated,
}: {
  onClose: () => void;
  onCreated: (u: User) => void;
}) {
  const [form, setForm] = useState<{
    display_name: string;
    type: "human" | "agent";
    email: string;
    avatar_color: string;
  }>({
    display_name: "",
    type: "human",
    email: "",
    avatar_color: "#6366f1",
  });
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [teams, setTeams] = useState<Team[]>([]);
  const [selectedTeamIds, setSelectedTeamIds] = useState<Set<string>>(new Set());
  const teamsLoaded = useRef(false);

  // Fetch teams on mount
  useEffect(() => {
    if (teamsLoaded.current) return;
    teamsLoaded.current = true;
    getTeams().then(setTeams).catch(() => {});
  }, []);

  const slug = form.display_name
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-|-$/g, "")
    .slice(0, 50);

  function toggleTeam(teamId: string) {
    setSelectedTeamIds((prev) => {
      const next = new Set(prev);
      if (next.has(teamId)) {
        next.delete(teamId);
      } else {
        next.add(teamId);
      }
      return next;
    });
  }

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (!form.display_name.trim() || !slug) return;
    setLoading(true);
    setError(null);
    try {
      const req: UserCreateRequest = {
        slug,
        display_name: form.display_name.trim(),
        type: form.type,
        email: form.email.trim() || null,
        avatar_color: form.avatar_color,
      };
      const user = await createUser(req);

      // Add user to selected teams (best-effort, don't block on failure)
      for (const teamId of selectedTeamIds) {
        try {
          await addTeamMember(teamId, { user_id: user.id });
        } catch {
          // Silently skip — team membership is a bonus, not critical
        }
      }

      onCreated(user);
      onClose();
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : "Errore nella creazione");
    } finally {
      setLoading(false);
    }
  }

  return (
    <div className="fixed inset-0 bg-black/60 z-50 flex items-center justify-center p-4">
      <div className="bg-pir-surface-1 border border-pir rounded-lg p-6 w-full max-w-md space-y-4">
        <div className="flex items-center justify-between">
          <h2 className="text-heading text-pir-text-primary">Nuovo Utente</h2>
          <button onClick={onClose} className="text-pir-text-muted hover:text-pir-text-primary transition-colors">X</button>
        </div>

        {error && (
          <div className="text-caption text-pir-error bg-pir-error/10 rounded px-3 py-2">{error}</div>
        )}

        <form onSubmit={handleSubmit} className="space-y-4">
          <div>
            <label className="text-label text-pir-text-secondary block mb-1">Nome Display *</label>
            <input
              className="w-full bg-pir-surface-2 border border-pir rounded px-3 py-2 text-body text-pir-text-primary focus:outline-none focus:border-pir-strong"
              value={form.display_name}
              onChange={(e) => setForm((f) => ({ ...f, display_name: e.target.value }))}
              required
              placeholder="Es. Emilio"
            />
            {slug && (
              <div className="text-caption text-pir-text-muted mt-1">Slug: {slug}</div>
            )}
          </div>

          <div>
            <label className="text-label text-pir-text-secondary block mb-1">Tipo</label>
            <div className="flex gap-2">
              {(["human", "agent"] as const).map((t) => (
                <button
                  key={t}
                  type="button"
                  onClick={() => setForm((f) => ({ ...f, type: t }))}
                  className={`flex-1 px-3 py-2 rounded border text-body transition-colors ${
                    form.type === t
                      ? "bg-pir-accent/20 text-pir-accent border-pir-accent/30"
                      : "bg-pir-surface-2 text-pir-text-secondary border-pir hover:border-pir-strong"
                  }`}
                >
                  {t === "human" ? "Human" : "Agent"}
                </button>
              ))}
            </div>
          </div>

          {form.type === "human" && (
            <div>
              <label className="text-label text-pir-text-secondary block mb-1">Email</label>
              <input
                type="email"
                className="w-full bg-pir-surface-2 border border-pir rounded px-3 py-2 text-body text-pir-text-primary focus:outline-none focus:border-pir-strong"
                value={form.email}
                onChange={(e) => setForm((f) => ({ ...f, email: e.target.value }))}
                placeholder="emilio@example.com"
              />
            </div>
          )}

          <div>
            <label className="text-label text-pir-text-secondary block mb-1">Avatar</label>
            <ColorPicker value={form.avatar_color} onChange={(c) => setForm((f) => ({ ...f, avatar_color: c }))} />
          </div>

          {/* Team selector */}
          {teams.length > 0 && (
            <div>
              <label className="text-label text-pir-text-secondary block mb-1">Teams</label>
              <div className="space-y-1.5 max-h-40 overflow-y-auto bg-pir-surface-2 border border-pir rounded p-2">
                {teams.map((team) => (
                  <label
                    key={team.id}
                    className="flex items-center gap-2 px-2 py-1.5 rounded cursor-pointer hover:bg-pir-surface-3 transition-colors"
                  >
                    <input
                      type="checkbox"
                      checked={selectedTeamIds.has(team.id)}
                      onChange={() => toggleTeam(team.id)}
                      className="accent-pir-accent"
                    />
                    <span className="text-body text-pir-text-primary">{team.display_name}</span>
                    <span className="text-caption text-pir-text-muted ml-auto">
                      {team.member_count ?? 0} members
                    </span>
                  </label>
                ))}
              </div>
            </div>
          )}

          <div className="flex gap-3 pt-2">
            <button
              type="button"
              onClick={onClose}
              className="flex-1 px-4 py-2 border border-pir rounded text-body text-pir-text-secondary hover:text-pir-text-primary transition-colors"
            >
              Annulla
            </button>
            <button
              type="submit"
              disabled={loading || !form.display_name.trim()}
              className="flex-1 px-4 py-2 bg-pir-accent rounded text-body text-white disabled:opacity-50 hover:opacity-90 transition-opacity"
            >
              {loading ? "Creando..." : "Crea"}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}

function ResetTokenModal({
  token,
  userSlug,
  onClose,
}: {
  token: string;
  userSlug: string;
  onClose: () => void;
}) {
  const [copied, setCopied] = useState(false);

  function handleCopy() {
    navigator.clipboard.writeText(token).then(() => {
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    });
  }

  return (
    <div className="fixed inset-0 bg-black/60 z-50 flex items-center justify-center p-4">
      <div className="bg-pir-surface-1 border border-pir rounded-lg p-6 w-full max-w-lg space-y-4">
        <div className="flex items-center justify-between">
          <h2 className="text-heading text-pir-text-primary">Reset Token — {userSlug}</h2>
          <button onClick={onClose} className="text-pir-text-muted hover:text-pir-text-primary transition-colors">X</button>
        </div>
        <div className="text-caption text-pir-text-secondary bg-pir-surface-2 border border-pir rounded px-3 py-2">
          Questo token e valido per 24 ore. Condividilo in modo sicuro con l'utente.
          Il token non sara mostrato di nuovo.
        </div>
        <div className="bg-pir-base border border-pir rounded px-3 py-2 font-mono text-caption text-pir-text-primary break-all">
          {token}
        </div>
        <div className="flex gap-3">
          <button
            onClick={handleCopy}
            className="flex-1 px-4 py-2 bg-pir-accent rounded text-body text-white hover:opacity-90 transition-opacity"
          >
            {copied ? "Copiato!" : "Copia Token"}
          </button>
          <button
            onClick={onClose}
            className="flex-1 px-4 py-2 border border-pir rounded text-body text-pir-text-secondary hover:text-pir-text-primary transition-colors"
          >
            Chiudi
          </button>
        </div>
      </div>
    </div>
  );
}

function UserRow({
  user,
  onEdit,
  onDelete,
  onResetToken,
}: {
  user: User;
  onEdit: (u: User) => void;
  onDelete: (id: string) => void;
  onResetToken: (u: User) => void;
}) {
  const [confirming, setConfirming] = useState(false);
  const [deleting, setDeleting] = useState(false);

  const initial = user.display_name.charAt(0).toUpperCase();

  async function handleConfirmDelete() {
    setDeleting(true);
    try {
      await deleteUser(user.id);
      onDelete(user.id);
    } catch {
      setDeleting(false);
      setConfirming(false);
    }
  }

  return (
    <tr className="border-b border-pir last:border-b-0 group hover:bg-pir-surface-2/50 transition-colors">
      <td className="px-4 py-3">
        <div className="flex items-center gap-3">
          <UserAvatar color={user.avatar_color} initial={initial} />
          <div>
            <div className="text-body text-pir-text-primary font-medium leading-tight">{user.display_name}</div>
            <div className="text-caption text-pir-text-muted">{user.slug}</div>
          </div>
        </div>
      </td>
      <td className="px-4 py-3">
        <TypeBadge type={user.type} />
      </td>
      <td className="px-4 py-3">
        <div className="flex items-center gap-1.5 flex-wrap">
          <RoleBadge role={user.system_role} />
          {user.teams?.map((t) => (
            <a
              key={t.id}
              href={`/settings/teams/detail/?id=${t.id}`}
              className="text-caption px-1.5 py-0.5 rounded bg-pir-surface-2 text-pir-text-secondary border border-pir hover:text-pir-text-primary transition-colors"
            >
              {t.display_name}
            </a>
          ))}
        </div>
      </td>
      <td className="px-4 py-3">
        <span className="text-body text-pir-text-secondary">
          {user.email || <span className="text-pir-text-muted">—</span>}
        </span>
      </td>
      <td className="px-4 py-3">
        <span className="font-mono text-caption text-pir-text-secondary">
          {user.linux_username || <span className="text-pir-text-muted">—</span>}
        </span>
      </td>
      <td className="px-4 py-3">
        {user.onboarding_completed ? (
          <span className="inline-flex items-center gap-1 text-caption px-2 py-0.5 rounded border bg-green-500/20 text-green-400 border-green-500/30">
            completato
          </span>
        ) : (
          <span className="inline-flex items-center gap-1 text-caption px-2 py-0.5 rounded border bg-pir-surface-3 text-pir-text-muted border-pir">
            —
          </span>
        )}
      </td>
      <td className="px-4 py-3">
        <span className="text-caption text-pir-text-secondary">
          {user.provisioned_at
            ? new Date(user.provisioned_at).toLocaleDateString("it-IT", { day: "2-digit", month: "short", year: "numeric" })
            : <span className="text-pir-text-muted">—</span>
          }
        </span>
      </td>
      <td className="px-4 py-3 text-right">
        {confirming ? (
          <div className="flex items-center justify-end gap-2">
            <span className="text-caption text-pir-text-muted">Eliminare?</span>
            <button
              onClick={handleConfirmDelete}
              disabled={deleting}
              className="text-caption px-2 py-1 bg-pir-error/20 text-pir-error border border-pir-error/30 rounded hover:bg-pir-error/30 transition-colors disabled:opacity-50"
            >
              {deleting ? "..." : "Si"}
            </button>
            <button
              onClick={() => setConfirming(false)}
              disabled={deleting}
              className="text-caption px-2 py-1 bg-pir-surface-2 text-pir-text-secondary border border-pir rounded hover:text-pir-text-primary transition-colors"
            >
              No
            </button>
          </div>
        ) : (
          <div className="flex items-center justify-end gap-1">
            {user.type === "human" && (
              <button
                onClick={() => onResetToken(user)}
                className="px-2 py-1 text-caption rounded text-pir-text-muted hover:text-pir-accent hover:bg-pir-accent/10 border border-transparent hover:border-pir-accent/30 transition-colors"
                title="Issue Reset Token"
              >
                Reset
              </button>
            )}
            <button
              onClick={() => onEdit(user)}
              className="w-7 h-7 flex items-center justify-center rounded text-pir-text-muted hover:text-pir-text-primary hover:bg-pir-surface-3 transition-colors text-sm"
              title="Modifica"
            >
              E
            </button>
            <button
              onClick={() => setConfirming(true)}
              className="w-7 h-7 flex items-center justify-center rounded text-pir-text-muted hover:text-pir-error hover:bg-pir-error/10 transition-colors text-sm"
              title="Elimina"
            >
              X
            </button>
          </div>
        )}
      </td>
    </tr>
  );
}

export default function UsersPage() {
  const [users, setUsers] = useState<User[]>([]);
  const [loading, setLoading] = useState(true);
  const [showCreate, setShowCreate] = useState(false);
  const [editingUser, setEditingUser] = useState<User | null>(null);
  const [filter, setFilter] = useState<FilterType>("all");
  const [resetToken, setResetToken] = useState<{ token: string; slug: string } | null>(null);
  const [resetLoading, setResetLoading] = useState(false);

  useEffect(() => {
    listUsers()
      .then(setUsers)
      .finally(() => setLoading(false));
  }, []);

  async function handleIssueResetToken(user: User) {
    setResetLoading(true);
    try {
      const result = await issueResetToken(user.id);
      setResetToken({ token: result.token, slug: result.slug });
    } catch (err) {
      alert(err instanceof Error ? err.message : "Failed to issue reset token");
    } finally {
      setResetLoading(false);
    }
  }

  function handleDeletedUser(id: string) {
    setUsers((us) => us.filter((u) => u.id !== id));
  }

  function handleUpdatedUser(updated: User) {
    setUsers((us) => us.map((u) => (u.id === updated.id ? updated : u)));
  }

  const humanCount = users.filter((u) => u.type === "human").length;
  const agentCount = users.filter((u) => u.type === "agent").length;

  const filtered =
    filter === "all" ? users : users.filter((u) => u.type === filter);

  const filterOptions: { key: FilterType; label: string; count: number }[] = [
    { key: "all", label: "Tutti", count: users.length },
    { key: "human", label: "Human", count: humanCount },
    { key: "agent", label: "Agent", count: agentCount },
  ];

  return (
    <PermissionGate minRole="admin" fallback={
      <div className="p-6 text-center text-pir-text-muted text-sm">You need admin access to view this page.</div>
    }>
    <div className="h-full overflow-y-auto">
      <div className="p-6 max-w-4xl">
        <div className="flex items-center justify-between mb-6">
          <div className="flex items-center gap-3">
            <h1 className="text-heading text-pir-text-primary">Utenti</h1>
            {!loading && (
              <span className="text-caption bg-pir-surface-2 px-2 py-0.5 rounded text-pir-text-muted">
                {users.length}
              </span>
            )}
          </div>
          <button
            onClick={() => setShowCreate(true)}
            className="px-4 py-2 bg-pir-accent rounded text-body text-white hover:opacity-90 transition-opacity"
          >
            + Nuovo Utente
          </button>
        </div>

        {loading ? (
          <div className="text-body text-pir-text-muted">Caricamento...</div>
        ) : (
          <>
            <div className="flex gap-2 mb-5">
              {filterOptions.map(({ key, label, count }) => (
                <button
                  key={key}
                  onClick={() => setFilter(key)}
                  className={`text-caption px-3 py-1.5 rounded border transition-colors ${
                    filter === key
                      ? "bg-pir-accent/20 text-pir-accent border-pir-accent/30"
                      : "bg-pir-surface-1 text-pir-text-secondary border-pir hover:border-pir-strong hover:text-pir-text-primary"
                  }`}
                >
                  {label} ({count})
                </button>
              ))}
            </div>

            <div className="bg-pir-surface-1 border border-pir rounded-lg overflow-hidden">
              <table className="w-full">
                <thead>
                  <tr className="border-b border-pir">
                    <th className="text-left text-label text-pir-text-muted px-4 py-3">Utente</th>
                    <th className="text-left text-label text-pir-text-muted px-4 py-3 w-28">Tipo</th>
                    <th className="text-left text-label text-pir-text-muted px-4 py-3">Ruolo</th>
                    <th className="text-left text-label text-pir-text-muted px-4 py-3">Email</th>
                    <th className="text-left text-label text-pir-text-muted px-4 py-3">Linux User</th>
                    <th className="text-left text-label text-pir-text-muted px-4 py-3 w-28">Onboarding</th>
                    <th className="text-left text-label text-pir-text-muted px-4 py-3 w-32">Provisioned</th>
                    <th className="px-4 py-3 w-24" />
                  </tr>
                </thead>
                <tbody>
                  {filtered.length === 0 ? (
                    <tr>
                      <td colSpan={8} className="px-4 py-16 text-center text-body text-pir-text-muted">
                        Nessun utente trovato.
                      </td>
                    </tr>
                  ) : (
                    filtered.map((user) => (
                      <UserRow
                        key={user.id}
                        user={user}
                        onEdit={setEditingUser}
                        onDelete={handleDeletedUser}
                        onResetToken={handleIssueResetToken}
                      />
                    ))
                  )}
                </tbody>
              </table>
            </div>
          </>
        )}
      </div>

      {showCreate && (
        <CreateUserModal
          onClose={() => setShowCreate(false)}
          onCreated={(u) => setUsers((us) => [...us, u])}
        />
      )}

      {editingUser && (
        <EditUserModal
          user={editingUser}
          onClose={() => setEditingUser(null)}
          onUpdated={handleUpdatedUser}
        />
      )}

      {resetToken && (
        <ResetTokenModal
          token={resetToken.token}
          userSlug={resetToken.slug}
          onClose={() => setResetToken(null)}
        />
      )}
    </div>
    </PermissionGate>
  );
}
