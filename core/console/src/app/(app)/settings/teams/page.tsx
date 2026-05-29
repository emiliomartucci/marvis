"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { getTeams, createTeam } from "@/lib/api";
import type { Team } from "@/lib/types";
import { ErrorAlert } from "@/components/ui/ErrorAlert";

function CreateTeamModal({
  onClose,
  onCreated,
}: {
  onClose: () => void;
  onCreated: (t: Team) => void;
}) {
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (!name.trim()) return;
    setLoading(true);
    setError(null);
    try {
      const team = await createTeam({
        display_name: name.trim(),
        description: description.trim() || undefined,
      });
      onCreated(team);
      onClose();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to create team");
    } finally {
      setLoading(false);
    }
  }

  return (
    <div className="fixed inset-0 bg-black/60 backdrop-blur-sm z-50 flex items-center justify-center p-4">
      <div className="bg-pir-surface-1 border border-pir rounded-xl p-6 w-full max-w-md space-y-4 shadow-2xl">
        <div className="flex items-center justify-between">
          <h2 className="text-heading text-pir-text-primary">New Team</h2>
          <button
            onClick={onClose}
            className="text-pir-text-muted hover:text-pir-text-primary transition-colors"
          >
            X
          </button>
        </div>

        {error && (
          <ErrorAlert message={error} />
        )}

        <form onSubmit={handleSubmit} className="space-y-4">
          <div>
            <label className="text-label text-pir-text-secondary block mb-1">
              Name *
            </label>
            <input
              className="w-full bg-pir-surface-2 border border-pir rounded px-3 py-2 text-body text-pir-text-primary focus:outline-none focus:border-pir-strong"
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder="e.g. Core Team"
              required
              autoFocus
            />
          </div>

          <div>
            <label className="text-label text-pir-text-secondary block mb-1">
              Description
            </label>
            <input
              className="w-full bg-pir-surface-2 border border-pir rounded px-3 py-2 text-body text-pir-text-primary focus:outline-none focus:border-pir-strong"
              value={description}
              onChange={(e) => setDescription(e.target.value)}
              placeholder="Optional description"
            />
          </div>

          <div className="flex gap-3 pt-2">
            <button
              type="button"
              onClick={onClose}
              className="flex-1 px-4 py-2 border border-pir rounded text-body text-pir-text-secondary hover:text-pir-text-primary transition-colors"
            >
              Cancel
            </button>
            <button
              type="submit"
              disabled={loading || !name.trim()}
              className="flex-1 px-4 py-2 bg-pir-accent rounded text-body text-white disabled:opacity-50 hover:opacity-90 transition-opacity"
            >
              {loading ? "Creating..." : "Create"}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}

export default function TeamsPage() {
  const [teams, setTeams] = useState<Team[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [showCreate, setShowCreate] = useState(false);

  useEffect(() => {
    const controller = new AbortController();
    getTeams({ signal: controller.signal })
      .then(setTeams)
      .catch((err) => {
        if (!controller.signal.aborted) {
          setError(err instanceof Error ? err.message : "Failed to load teams");
        }
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false);
      });
    return () => controller.abort();
  }, []);

  return (
    <div className="h-full overflow-y-auto">
      <div className="p-6 max-w-4xl">
        <div className="flex items-center justify-between mb-6">
          <div className="flex items-center gap-3">
            <h1 className="text-heading text-pir-text-primary">Teams</h1>
            {!loading && (
              <span className="text-caption bg-pir-surface-2 px-2 py-0.5 rounded text-pir-text-muted">
                {teams.length}
              </span>
            )}
          </div>
          <button
            onClick={() => setShowCreate(true)}
            className="px-4 py-2 bg-pir-accent rounded text-body text-white hover:opacity-90 transition-opacity"
          >
            + New Team
          </button>
        </div>

        {loading ? (
          <div className="text-body text-pir-text-muted">Loading...</div>
        ) : error ? (
          <ErrorAlert message={error} />
        ) : teams.length === 0 ? (
          <div className="text-body text-pir-text-muted">
            No teams found. Create your first team to get started.
          </div>
        ) : (
          <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
            {teams.map((team) => (
              <Link
                key={team.id}
                href={`/settings/teams/detail/?id=${encodeURIComponent(team.id)}`}
                className="block bg-pir-surface-1 border border-pir rounded-xl p-5 hover:border-pir-strong transition-colors group"
              >
                <div className="flex items-start justify-between">
                  <div className="min-w-0 flex-1">
                    <div className="text-body font-medium text-pir-text-primary group-hover:text-pir-accent transition-colors">
                      {team.display_name}
                    </div>
                    <div className="text-caption text-pir-text-muted mt-0.5">
                      {team.slug}
                    </div>
                    {team.description && (
                      <div className="text-caption text-pir-text-secondary mt-2 line-clamp-2">
                        {team.description}
                      </div>
                    )}
                  </div>
                </div>
                <div className="flex gap-4 text-caption text-pir-text-muted mt-3 pt-3 border-t border-pir/50">
                  <div>
                    <span className="text-pir-text-primary font-medium">
                      {team.member_count ?? 0}
                    </span>{" "}
                    member{(team.member_count ?? 0) !== 1 ? "s" : ""}
                  </div>
                  <div>
                    <span className="text-pir-text-primary font-medium">
                      {team.project_count ?? 0}
                    </span>{" "}
                    project{(team.project_count ?? 0) !== 1 ? "s" : ""}
                  </div>
                </div>
              </Link>
            ))}
          </div>
        )}
      </div>

      {showCreate && (
        <CreateTeamModal
          onClose={() => setShowCreate(false)}
          onCreated={(t) => setTeams((prev) => [...prev, t])}
        />
      )}
    </div>
  );
}
