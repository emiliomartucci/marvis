"use client";

import { useEffect, useState, useCallback, useRef, Suspense } from "react";
import { useSearchParams, useRouter } from "next/navigation";
import Link from "next/link";
import { ErrorAlert } from "@/components/ui/ErrorAlert";
import {
  getTeamMembers,
  getTeamProjects,
  addTeamMember,
  removeTeamMember,
  assignTeamProject,
  removeTeamProject,
  getTeams,
  listUsers,
  updateTeam,
  deleteTeam,
} from "@/lib/api";
import type { TeamMember, TeamProject, Team, User, TeamRole } from "@/lib/types";

function InlineEditText({
  value,
  onSave,
  className = "",
  inputClassName = "",
  placeholder = "",
  multiline = false,
}: {
  value: string;
  onSave: (val: string) => Promise<void>;
  className?: string;
  inputClassName?: string;
  placeholder?: string;
  multiline?: boolean;
}) {
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(value);
  const [saving, setSaving] = useState(false);
  const inputRef = useRef<HTMLInputElement | HTMLTextAreaElement>(null);

  useEffect(() => {
    setDraft(value);
  }, [value]);

  useEffect(() => {
    if (editing) inputRef.current?.focus();
  }, [editing]);

  async function handleSave() {
    if (draft.trim() === value) {
      setEditing(false);
      return;
    }
    setSaving(true);
    try {
      await onSave(draft.trim());
      setEditing(false);
    } catch {
      // revert on error
      setDraft(value);
      setEditing(false);
    } finally {
      setSaving(false);
    }
  }

  function handleKeyDown(e: React.KeyboardEvent) {
    if (e.key === "Enter" && !multiline) {
      e.preventDefault();
      handleSave();
    }
    if (e.key === "Escape") {
      setDraft(value);
      setEditing(false);
    }
  }

  if (editing) {
    const sharedProps = {
      value: draft,
      onChange: (e: React.ChangeEvent<HTMLInputElement | HTMLTextAreaElement>) =>
        setDraft(e.target.value),
      onBlur: handleSave,
      onKeyDown: handleKeyDown,
      disabled: saving,
      className: `bg-pir-surface-2 border border-pir-strong rounded px-2 py-1 focus:outline-none ${inputClassName}`,
    };

    if (multiline) {
      return (
        <textarea
          ref={inputRef as React.RefObject<HTMLTextAreaElement>}
          rows={2}
          {...sharedProps}
        />
      );
    }
    return (
      <input
        ref={inputRef as React.RefObject<HTMLInputElement>}
        type="text"
        {...sharedProps}
      />
    );
  }

  return (
    <span
      onClick={() => setEditing(true)}
      className={`cursor-pointer hover:bg-pir-surface-2 rounded px-1 -mx-1 transition-colors ${className}`}
      title="Click to edit"
    >
      {value || <span className="text-pir-text-muted italic">{placeholder}</span>}
    </span>
  );
}

function DeleteTeamDialog({
  team,
  onClose,
  onDeleted,
}: {
  team: Team;
  onClose: () => void;
  onDeleted: () => void;
}) {
  const [confirmText, setConfirmText] = useState("");
  const [deleting, setDeleting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const canDelete = confirmText === team.display_name;

  async function handleDelete() {
    if (!canDelete) return;
    setDeleting(true);
    setError(null);
    try {
      await deleteTeam(team.id);
      onDeleted();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to delete team");
      setDeleting(false);
    }
  }

  return (
    <div className="fixed inset-0 bg-black/60 backdrop-blur-sm z-50 flex items-center justify-center p-4">
      <div className="bg-pir-surface-1 border border-pir-error/30 rounded-xl p-6 w-full max-w-md space-y-4 shadow-2xl">
        <div>
          <h2 className="text-heading text-pir-error">Delete Team</h2>
          <p className="text-body text-pir-text-secondary mt-2">
            This will soft-delete the team <strong className="text-pir-text-primary">{team.display_name}</strong>.
            Members will lose access to team projects.
          </p>
        </div>

        {error && (
          <div className="text-caption text-pir-error bg-pir-error/10 rounded px-3 py-2">
            {error}
          </div>
        )}

        <div>
          <label className="text-label text-pir-text-secondary block mb-1">
            Type <strong className="text-pir-text-primary">{team.display_name}</strong> to confirm
          </label>
          <input
            className="w-full bg-pir-surface-2 border border-pir rounded px-3 py-2 text-body text-pir-text-primary focus:outline-none focus:border-pir-error"
            value={confirmText}
            onChange={(e) => setConfirmText(e.target.value)}
            placeholder={team.display_name}
            autoFocus
          />
        </div>

        <div className="flex gap-3">
          <button
            type="button"
            onClick={onClose}
            className="flex-1 px-4 py-2 border border-pir rounded text-body text-pir-text-secondary hover:text-pir-text-primary transition-colors"
          >
            Cancel
          </button>
          <button
            onClick={handleDelete}
            disabled={!canDelete || deleting}
            className="flex-1 px-4 py-2 bg-pir-error rounded text-body text-white disabled:opacity-50 hover:opacity-90 transition-opacity"
          >
            {deleting ? "Deleting..." : "Delete Team"}
          </button>
        </div>
      </div>
    </div>
  );
}

function TeamDetailContent() {
  const searchParams = useSearchParams();
  const router = useRouter();
  const teamId = searchParams.get("id") ?? "";

  const [team, setTeam] = useState<Team | null>(null);
  const [members, setMembers] = useState<TeamMember[]>([]);
  const [projects, setProjects] = useState<TeamProject[]>([]);
  const [allUsers, setAllUsers] = useState<User[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [showDeleteDialog, setShowDeleteDialog] = useState(false);

  // Add member form
  const [addingMember, setAddingMember] = useState(false);
  const [selectedUserId, setSelectedUserId] = useState("");
  const [selectedRole, setSelectedRole] = useState<TeamRole>("member");
  const [addMemberLoading, setAddMemberLoading] = useState(false);
  const [addMemberError, setAddMemberError] = useState<string | null>(null);

  // Add project form
  const [addingProject, setAddingProject] = useState(false);
  const [projectSlug, setProjectSlug] = useState("");
  const [addProjectLoading, setAddProjectLoading] = useState(false);
  const [addProjectError, setAddProjectError] = useState<string | null>(null);

  const loadData = useCallback(
    (signal?: AbortSignal) => {
      if (!teamId) return;
      setLoading(true);
      Promise.all([
        getTeams({ signal }),
        getTeamMembers(teamId, { signal }),
        getTeamProjects(teamId, { signal }),
        listUsers(),
      ])
        .then(([teams, memberList, projectList, users]) => {
          const found = teams.find((t) => t.id === teamId) ?? null;
          setTeam(found);
          setMembers(memberList);
          setProjects(projectList);
          setAllUsers(users);
          setError(null);
        })
        .catch((err) => {
          if (!signal?.aborted) {
            setError(
              err instanceof Error ? err.message : "Failed to load team data"
            );
          }
        })
        .finally(() => {
          if (!signal?.aborted) setLoading(false);
        });
    },
    [teamId]
  );

  useEffect(() => {
    const controller = new AbortController();
    loadData(controller.signal);
    return () => controller.abort();
  }, [loadData]);

  async function handleUpdateName(val: string) {
    if (!val) return;
    const updated = await updateTeam(teamId, { display_name: val });
    setTeam(updated);
  }

  async function handleUpdateDescription(val: string) {
    const updated = await updateTeam(teamId, { description: val || undefined });
    setTeam(updated);
  }

  async function handleAddMember(e: React.FormEvent) {
    e.preventDefault();
    if (!selectedUserId) return;
    setAddMemberLoading(true);
    setAddMemberError(null);
    try {
      await addTeamMember(teamId, { user_id: selectedUserId, role: selectedRole });
      setSelectedUserId("");
      setSelectedRole("member");
      setAddingMember(false);
      loadData();
    } catch (err) {
      setAddMemberError(
        err instanceof Error ? err.message : "Failed to add member"
      );
    } finally {
      setAddMemberLoading(false);
    }
  }

  async function handleRemoveMember(userId: string) {
    try {
      await removeTeamMember(teamId, userId);
      setMembers((prev) => prev.filter((m) => m.user_id !== userId));
    } catch (err) {
      alert(err instanceof Error ? err.message : "Failed to remove member");
    }
  }

  async function handleAddProject(e: React.FormEvent) {
    e.preventDefault();
    if (!projectSlug.trim()) return;
    setAddProjectLoading(true);
    setAddProjectError(null);
    try {
      await assignTeamProject(teamId, { project: projectSlug.trim() });
      setProjectSlug("");
      setAddingProject(false);
      loadData();
    } catch (err) {
      setAddProjectError(
        err instanceof Error ? err.message : "Failed to assign project"
      );
    } finally {
      setAddProjectLoading(false);
    }
  }

  async function handleRemoveProject(slug: string) {
    try {
      await removeTeamProject(teamId, slug);
      setProjects((prev) => prev.filter((p) => p.project !== slug));
    } catch (err) {
      alert(err instanceof Error ? err.message : "Failed to remove project");
    }
  }

  async function handleToggleVisibility(slug: string, currentIsPublic: boolean) {
    try {
      await assignTeamProject(teamId, {
        project: slug,
        is_public: !currentIsPublic,
      });
      setProjects((prev) =>
        prev.map((p) =>
          p.project === slug ? { ...p, is_public: !currentIsPublic } : p
        )
      );
    } catch (err) {
      alert(err instanceof Error ? err.message : "Failed to update visibility");
    }
  }

  const memberUserIds = new Set(members.map((m) => m.user_id));
  const availableUsers = allUsers.filter(
    (u) => !memberUserIds.has(u.id)
  );

  if (!teamId) {
    return (
      <div className="p-6 text-body text-pir-text-muted">No team selected.</div>
    );
  }

  if (loading) {
    return (
      <div className="p-6 text-body text-pir-text-muted">Loading...</div>
    );
  }

  if (error) {
    return <div className="p-6"><ErrorAlert message={error} /></div>;
  }

  return (
    <div className="h-full overflow-y-auto">
      <div className="p-6 max-w-4xl space-y-8">
        {/* Header with inline edit */}
        <div>
          <div className="flex items-center gap-2 text-caption text-pir-text-muted mb-2">
            <Link
              href="/settings/teams/"
              className="hover:text-pir-text-primary transition-colors"
            >
              Teams
            </Link>
            <span>/</span>
            <span>{team?.display_name ?? teamId}</span>
          </div>
          <h1 className="text-heading text-pir-text-primary">
            <InlineEditText
              value={team?.display_name ?? ""}
              onSave={handleUpdateName}
              className="text-heading"
              inputClassName="text-heading text-pir-text-primary w-full"
              placeholder="Team name"
            />
          </h1>
          <div className="mt-1">
            <InlineEditText
              value={team?.description ?? ""}
              onSave={handleUpdateDescription}
              className="text-body text-pir-text-secondary"
              inputClassName="text-body text-pir-text-secondary w-full"
              placeholder="Add a description..."
              multiline
            />
          </div>
        </div>

        {/* Members */}
        <div>
          <div className="flex items-center justify-between mb-4">
            <h2 className="text-subheading text-pir-text-primary">
              Members ({members.length})
            </h2>
            <button
              onClick={() => setAddingMember(true)}
              className="px-3 py-1.5 bg-pir-accent rounded text-caption text-white hover:opacity-90 transition-opacity"
            >
              + Add Member
            </button>
          </div>

          {addingMember && (
            <form
              onSubmit={handleAddMember}
              className="mb-4 bg-pir-surface-1 border border-pir rounded-lg p-4 space-y-3"
            >
              <h3 className="text-body font-medium text-pir-text-primary">
                Add Member
              </h3>
              {addMemberError && (
                <ErrorAlert message={addMemberError} />
              )}
              <select
                className="w-full bg-pir-surface-2 border border-pir rounded px-3 py-2 text-body text-pir-text-primary focus:outline-none focus:border-pir-strong"
                value={selectedUserId}
                onChange={(e) => setSelectedUserId(e.target.value)}
                required
              >
                <option value="">Select user...</option>
                {availableUsers.map((u) => (
                  <option key={u.id} value={u.id}>
                    {u.display_name} ({u.slug})
                  </option>
                ))}
              </select>
              <div>
                <label className="text-label text-pir-text-secondary block mb-1">
                  Team Role
                </label>
                <select
                  className="w-full bg-pir-surface-2 border border-pir rounded px-3 py-2 text-body text-pir-text-primary focus:outline-none focus:border-pir-strong"
                  value={selectedRole}
                  onChange={(e) => setSelectedRole(e.target.value as TeamRole)}
                >
                  <option value="member">Member</option>
                  <option value="admin">Team Admin</option>
                </select>
              </div>
              <div className="flex gap-2">
                <button
                  type="button"
                  onClick={() => {
                    setAddingMember(false);
                    setAddMemberError(null);
                  }}
                  className="flex-1 px-3 py-2 border border-pir rounded text-body text-pir-text-secondary hover:text-pir-text-primary transition-colors"
                >
                  Cancel
                </button>
                <button
                  type="submit"
                  disabled={addMemberLoading || !selectedUserId}
                  className="flex-1 px-3 py-2 bg-pir-accent rounded text-body text-white disabled:opacity-50 hover:opacity-90 transition-opacity"
                >
                  {addMemberLoading ? "Adding..." : "Add"}
                </button>
              </div>
            </form>
          )}

          {members.length === 0 ? (
            <div className="text-body text-pir-text-muted">No members yet.</div>
          ) : (
            <div className="bg-pir-surface-1 border border-pir rounded-lg overflow-hidden">
              <table className="w-full">
                <thead>
                  <tr className="border-b border-pir">
                    <th className="text-left text-label text-pir-text-muted px-4 py-3">
                      User
                    </th>
                    <th className="text-left text-label text-pir-text-muted px-4 py-3 w-28">
                      System Role
                    </th>
                    <th className="text-left text-label text-pir-text-muted px-4 py-3 w-28">
                      Team Role
                    </th>
                    <th className="px-4 py-3 w-16" />
                  </tr>
                </thead>
                <tbody>
                  {members.map((m) => (
                    <tr
                      key={m.user_id}
                      className="border-b border-pir last:border-b-0 group hover:bg-pir-surface-2/50 transition-colors"
                    >
                      <td className="px-4 py-3">
                        <div className="text-body text-pir-text-primary font-medium">
                          {m.display_name}
                        </div>
                        <div className="text-caption text-pir-text-muted">
                          {m.user_id}
                        </div>
                      </td>
                      <td className="px-4 py-3">
                        <span className="text-caption text-pir-text-secondary">
                          {m.system_role}
                        </span>
                      </td>
                      <td className="px-4 py-3">
                        {m.role === "admin" ? (
                          <span className="text-caption px-2 py-0.5 rounded bg-pir-accent/20 text-pir-accent border border-pir-accent/30">
                            admin
                          </span>
                        ) : (
                          <span className="text-caption text-pir-text-muted">
                            member
                          </span>
                        )}
                      </td>
                      <td className="px-4 py-3 text-right">
                        <button
                          onClick={() => handleRemoveMember(m.user_id)}
                          className="opacity-0 group-hover:opacity-100 w-7 h-7 flex items-center justify-center rounded text-pir-text-muted hover:text-pir-error hover:bg-pir-error/10 transition-all text-sm ml-auto"
                          title="Remove"
                        >
                          X
                        </button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>

        {/* Projects */}
        <div>
          <div className="flex items-center justify-between mb-4">
            <h2 className="text-subheading text-pir-text-primary">
              Projects ({projects.length})
            </h2>
            <button
              onClick={() => setAddingProject(true)}
              className="px-3 py-1.5 bg-pir-accent rounded text-caption text-white hover:opacity-90 transition-opacity"
            >
              + Assign Project
            </button>
          </div>

          {addingProject && (
            <form
              onSubmit={handleAddProject}
              className="mb-4 bg-pir-surface-1 border border-pir rounded-lg p-4 space-y-3"
            >
              <h3 className="text-body font-medium text-pir-text-primary">
                Assign Project
              </h3>
              {addProjectError && (
                <ErrorAlert message={addProjectError} />
              )}
              <input
                className="w-full bg-pir-surface-2 border border-pir rounded px-3 py-2 text-body text-pir-text-primary focus:outline-none focus:border-pir-strong"
                placeholder="project-slug"
                value={projectSlug}
                onChange={(e) => setProjectSlug(e.target.value)}
                required
              />
              <div className="flex gap-2">
                <button
                  type="button"
                  onClick={() => {
                    setAddingProject(false);
                    setAddProjectError(null);
                  }}
                  className="flex-1 px-3 py-2 border border-pir rounded text-body text-pir-text-secondary hover:text-pir-text-primary transition-colors"
                >
                  Cancel
                </button>
                <button
                  type="submit"
                  disabled={addProjectLoading || !projectSlug.trim()}
                  className="flex-1 px-3 py-2 bg-pir-accent rounded text-body text-white disabled:opacity-50 hover:opacity-90 transition-opacity"
                >
                  {addProjectLoading ? "Assigning..." : "Assign"}
                </button>
              </div>
            </form>
          )}

          {projects.length === 0 && !addingProject ? (
            <div className="text-body text-pir-text-muted">
              No projects assigned to this team.
            </div>
          ) : (
            projects.length > 0 && (
              <div className="bg-pir-surface-1 border border-pir rounded-lg overflow-hidden">
                <table className="w-full">
                  <thead>
                    <tr className="border-b border-pir">
                      <th className="text-left text-label text-pir-text-muted px-4 py-3">
                        Project
                      </th>
                      <th className="text-left text-label text-pir-text-muted px-4 py-3 w-28">
                        Visibility
                      </th>
                      <th className="px-4 py-3 w-16" />
                    </tr>
                  </thead>
                  <tbody>
                    {projects.map((p) => (
                      <tr
                        key={p.project}
                        className="border-b border-pir last:border-b-0 group hover:bg-pir-surface-2/50 transition-colors"
                      >
                        <td className="px-4 py-3">
                          <Link
                            href={`/projects/detail/?slug=${encodeURIComponent(p.project)}`}
                            className="text-body text-pir-accent hover:underline"
                          >
                            {p.project}
                          </Link>
                        </td>
                        <td className="px-4 py-3">
                          <button
                            onClick={() =>
                              handleToggleVisibility(p.project, p.is_public)
                            }
                            className={`text-caption px-2 py-0.5 rounded border cursor-pointer transition-colors ${
                              p.is_public
                                ? "bg-emerald-500/20 text-emerald-400 border-emerald-500/30 hover:bg-emerald-500/30"
                                : "bg-pir-surface-3 text-pir-text-muted border-pir hover:bg-pir-surface-2"
                            }`}
                            title={`Click to make ${p.is_public ? "private" : "public"}`}
                          >
                            {p.is_public ? "public" : "private"}
                          </button>
                        </td>
                        <td className="px-4 py-3 text-right">
                          <button
                            onClick={() => handleRemoveProject(p.project)}
                            className="opacity-0 group-hover:opacity-100 w-7 h-7 flex items-center justify-center rounded text-pir-text-muted hover:text-pir-error hover:bg-pir-error/10 transition-all text-sm ml-auto"
                            title="Remove"
                          >
                            X
                          </button>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )
          )}
        </div>

        {/* Danger Zone */}
        {team && (
          <div className="border border-pir-error/30 rounded-lg p-5">
            <h2 className="text-subheading text-pir-error mb-2">Danger Zone</h2>
            <p className="text-body text-pir-text-secondary mb-4">
              Deleting this team will remove all member access to associated projects.
              This action can be reversed by a database admin.
            </p>
            <button
              onClick={() => setShowDeleteDialog(true)}
              className="px-4 py-2 border border-pir-error/50 rounded text-body text-pir-error hover:bg-pir-error/10 transition-colors"
            >
              Delete Team
            </button>
          </div>
        )}
      </div>

      {showDeleteDialog && team && (
        <DeleteTeamDialog
          team={team}
          onClose={() => setShowDeleteDialog(false)}
          onDeleted={() => router.push("/settings/teams/")}
        />
      )}
    </div>
  );
}

export default function TeamDetailPage() {
  return (
    <Suspense
      fallback={
        <div className="p-6 text-body text-pir-text-muted">Loading...</div>
      }
    >
      <TeamDetailContent />
    </Suspense>
  );
}
