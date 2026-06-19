// v1.0.0 - 2026-03-13 - Workspace context with cookie persistence
"use client";

import { createContext, useCallback, useContext, useEffect, useState, type ReactNode } from "react";
import { getWorkspaces } from "@/lib/api";
import type { Workspace } from "@/lib/types";

interface WorkspaceContextValue {
  workspaceId: string;
  workspaces: Workspace[];
  setWorkspace: (id: string) => void;
  loading: boolean;
}

const WorkspaceContext = createContext<WorkspaceContextValue>({
  workspaceId: "ws_default",
  workspaces: [],
  setWorkspace: () => {},
  loading: true,
});

function getCookie(name: string): string | null {
  const match = document.cookie.match(new RegExp(`(?:^|; )${name}=([^;]*)`));
  return match ? decodeURIComponent(match[1]) : null;
}

function setCookie(name: string, value: string, days = 365) {
  const expires = new Date(Date.now() + days * 864e5).toUTCString();
  document.cookie = `${name}=${encodeURIComponent(value)};expires=${expires};path=/;SameSite=Lax`;
}

export function WorkspaceProvider({ children }: { children: ReactNode }) {
  const [workspaceId, setWorkspaceIdState] = useState("ws_default");
  const [workspaces, setWorkspaces] = useState<Workspace[]>([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    // Local single-user tier has one implicit workspace (ws_default) and no
    // /api/v1/workspaces endpoint — calling it just logs a 404 (gh #22 QA gap).
    if (
      process.env.NEXT_PUBLIC_LOCAL_MODE === "1" ||
      process.env.NEXT_PUBLIC_LOCAL_MODE === "true"
    ) {
      setLoading(false);
      return;
    }

    const saved = getCookie("pir_workspace");
    if (saved) setWorkspaceIdState(saved);

    getWorkspaces()
      .then((ws) => {
        setWorkspaces(ws);
        // If saved workspace doesn't exist in list, use first one
        if (ws.length > 0) {
          const valid = ws.some((w) => w.id === (saved || "ws_default"));
          if (!valid) {
            setWorkspaceIdState(ws[0].id);
            setCookie("pir_workspace", ws[0].id);
          }
        }
      })
      .catch(() => {
        // Workspace API may not exist yet — use default silently
      })
      .finally(() => setLoading(false));
  }, []);

  const setWorkspace = useCallback((id: string) => {
    setWorkspaceIdState(id);
    setCookie("pir_workspace", id);
  }, []);

  return (
    <WorkspaceContext.Provider value={{ workspaceId, workspaces, setWorkspace, loading }}>
      {children}
    </WorkspaceContext.Provider>
  );
}

export function useWorkspace() {
  return useContext(WorkspaceContext);
}
