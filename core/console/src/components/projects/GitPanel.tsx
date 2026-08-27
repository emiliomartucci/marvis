"use client";

import { useState, useEffect } from "react";
import { getProjectGitLog, getProjectGitBranches, getProjectGitDiff } from "@/lib/api";
import type { GitCommit, GitBranch } from "@/lib/types";

export default function GitPanel({ slug }: { slug: string }) {
  const [commits, setCommits] = useState<GitCommit[]>([]);
  const [branches, setBranches] = useState<GitBranch[]>([]);
  const [diff, setDiff] = useState("");
  const [loading, setLoading] = useState(true);
  const [showDiff, setShowDiff] = useState(false);

  useEffect(() => {
    const controller = new AbortController();
    Promise.all([
      getProjectGitLog(slug, 20, { signal: controller.signal }),
      getProjectGitBranches(slug, { signal: controller.signal }),
    ])
      .then(([c, b]) => {
        setCommits(c);
        setBranches(b);
      })
      .catch(() => {})
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false);
      });
    return () => controller.abort();
  }, [slug]);

  async function handleLoadDiff() {
    setShowDiff(true);
    try {
      const res = await getProjectGitDiff(slug);
      setDiff(res.diff);
    } catch {
      setDiff("Failed to load diff");
    }
  }

  if (loading) return <div className="text-pir-text-muted text-body p-4">Loading git data...</div>;

  const currentBranch = branches.find((b) => b.is_current);

  // Group commits by date
  function getDateLabel(dateStr: string): string {
    const date = new Date(dateStr);
    const today = new Date();
    const yesterday = new Date();
    yesterday.setDate(yesterday.getDate() - 1);

    if (date.toDateString() === today.toDateString()) return "Today";
    if (date.toDateString() === yesterday.toDateString()) return "Yesterday";
    return date.toLocaleDateString("en-US", { month: "short", day: "numeric" });
  }

  const groupedCommits: { label: string; commits: GitCommit[] }[] = [];
  for (const c of commits) {
    const label = getDateLabel(c.date);
    const last = groupedCommits[groupedCommits.length - 1];
    if (last && last.label === label) {
      last.commits.push(c);
    } else {
      groupedCommits.push({ label, commits: [c] });
    }
  }

  return (
    <div className="space-y-4">
      {/* Branch + actions */}
      <div className="flex flex-wrap items-center gap-2 md:gap-3">
        <span className="text-caption text-pir-text-muted">Branch:</span>
        <span className="text-body font-mono text-pir-accent">{currentBranch?.name || "unknown"}</span>
        <div className="ml-auto flex gap-2">
          {!showDiff && (
            <button
              onClick={handleLoadDiff}
              className="px-3 py-1 text-caption bg-pir-surface-0 border border-pir rounded hover:bg-pir-surface-1"
            >
              Diff
            </button>
          )}
        </div>
      </div>

      {/* Diff */}
      {showDiff && diff && (
        <div className="bg-pir-surface-0 border border-pir rounded p-3 max-h-64 overflow-y-auto">
          <pre className="text-caption font-mono text-pir-text-secondary whitespace-pre-wrap">{diff}</pre>
        </div>
      )}

      {/* Commit log — grouped by date */}
      {groupedCommits.map((group) => (
        <div key={group.label}>
          <div className="text-caption font-medium text-pir-text-tertiary uppercase tracking-wider mb-1 px-1">
            {group.label}
          </div>
          <div className="space-y-px">
            {group.commits.map((c) => (
              <div key={c.hash} className="flex items-center gap-2 px-2.5 py-1.5 text-caption bg-pir-surface-1 border border-pir rounded">
                <span className="font-mono text-pir-purple shrink-0 text-caption">{c.hash_short}</span>
                <span className="text-pir-text-secondary truncate">{c.message}</span>
                <span className="text-pir-text-tertiary ml-auto shrink-0 hidden md:inline">{c.author}</span>
              </div>
            ))}
          </div>
        </div>
      ))}

      {/* Other branches */}
      {branches.length > 1 && (
        <div>
          <h4 className="text-caption text-pir-text-muted mb-1">Branches:</h4>
          <div className="flex flex-wrap gap-1">
            {branches.map((b) => (
              <span
                key={b.name}
                className={`text-caption font-mono px-2 py-0.5 rounded ${
                  b.is_current ? "bg-pir-accent/20 text-pir-accent" : "bg-pir-surface-0 text-pir-text-muted"
                }`}
              >
                {b.name}
              </span>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}
