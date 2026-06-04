// v1.0.0 - 2026-03-13 - Workspace switcher dropdown for settings
"use client";

import { useWorkspace } from "@/lib/workspace";

export default function WorkspaceSelector() {
  const { workspaceId, workspaces, setWorkspace, loading } = useWorkspace();

  if (loading || workspaces.length <= 1) return null;

  return (
    <div className="px-3 pb-2">
      <select
        value={workspaceId}
        onChange={(e) => setWorkspace(e.target.value)}
        className="w-full bg-pir-surface-0 border border-pir rounded px-2.5 py-1.5 text-xs text-pir-text-secondary focus:outline-none focus:border-pir-accent"
      >
        {workspaces.map((ws) => (
          <option key={ws.id} value={ws.id}>
            {ws.name} ({ws.member_count})
          </option>
        ))}
      </select>
    </div>
  );
}
