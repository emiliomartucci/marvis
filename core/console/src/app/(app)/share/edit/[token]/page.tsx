// v1.0.0 - 2026-04-24 - Authenticated MarkdownEditor for shared workspace files
"use client";

import dynamic from "next/dynamic";
import { useParams } from "next/navigation";
import { useCallback, useEffect, useState } from "react";

import { API_BASE_URL } from "@/lib/config";

const MarkdownEditor = dynamic(
  () => import("@/components/markdown/MarkdownEditor"),
  {
    ssr: false,
    loading: () => <div className="flex-1 bg-pir-base animate-pulse" />,
  },
);

interface SharedFileJson {
  filename: string;
  path: string;
  content: string;
  editable: boolean;
  can_edit: boolean;
  is_authenticated: boolean;
}

type SaveState = "idle" | "saving" | "saved" | "error";

export default function ShareEditPage() {
  const params = useParams<{ token: string }>();
  const token = params?.token;

  const [data, setData] = useState<SharedFileJson | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [saveState, setSaveState] = useState<SaveState>("idle");
  const [saveError, setSaveError] = useState<string | null>(null);

  useEffect(() => {
    if (!token) return;
    let cancelled = false;
    const url = `${API_BASE_URL}/api/v1/shared/${encodeURIComponent(token)}?format=json`;
    fetch(url, { credentials: "include" })
      .then(async (r) => {
        if (!r.ok) {
          throw new Error(`HTTP ${r.status}: ${(await r.text()) || r.statusText}`);
        }
        return r.json() as Promise<SharedFileJson>;
      })
      .then((payload) => {
        if (!cancelled) setData(payload);
      })
      .catch((e) => {
        if (!cancelled) setError(String(e));
      });
    return () => {
      cancelled = true;
    };
  }, [token]);

  const handleSave = useCallback(
    async (md: string) => {
      if (!token || !data?.can_edit) return;
      setSaveState("saving");
      setSaveError(null);
      try {
        const url = `${API_BASE_URL}/api/v1/shared/${encodeURIComponent(token)}`;
        const r = await fetch(url, {
          method: "PUT",
          credentials: "include",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ content: md }),
        });
        if (!r.ok) {
          throw new Error(`HTTP ${r.status}: ${(await r.text()) || r.statusText}`);
        }
        setData((prev) => (prev ? { ...prev, content: md } : prev));
        setSaveState("saved");
        // Auto-clear "saved" badge after 2s.
        window.setTimeout(() => {
          setSaveState((s) => (s === "saved" ? "idle" : s));
        }, 2000);
      } catch (e) {
        setSaveState("error");
        setSaveError(String(e));
      }
    },
    [token, data?.can_edit],
  );

  if (error) {
    return (
      <div className="p-6 text-pir-text-secondary">
        <h1 className="text-h3 text-pir-text-primary mb-2">Unable to load shared file</h1>
        <p className="text-caption">{error}</p>
      </div>
    );
  }

  if (!data) {
    return (
      <div className="flex-1 flex items-center justify-center text-pir-text-tertiary">
        Loading shared file...
      </div>
    );
  }

  const headerStatus = (() => {
    if (!data.can_edit) return <span className="text-pir-warning">Read-only</span>;
    if (saveState === "saving") return <span className="text-pir-accent">Saving...</span>;
    if (saveState === "saved") return <span className="text-pir-success">Saved</span>;
    if (saveState === "error")
      return (
        <span className="text-pir-danger" title={saveError ?? undefined}>
          Save failed
        </span>
      );
    return null;
  })();

  return (
    <div className="flex flex-col flex-1 min-h-0">
      <header className="flex items-center gap-3 px-4 py-3 border-b border-pir-border bg-pir-surface-1">
        <div className="flex flex-col min-w-0">
          <strong className="text-pir-text-primary truncate">{data.filename}</strong>
          <span className="text-caption text-pir-text-tertiary truncate">{data.path}</span>
        </div>
        <div className="ml-auto text-caption flex items-center gap-3">
          {headerStatus}
        </div>
      </header>
      <div className="flex-1 min-h-0">
        <MarkdownEditor
          content={data.content}
          readOnly={!data.can_edit}
          onSave={data.can_edit ? handleSave : undefined}
          defaultMode={data.can_edit ? "wysiwyg" : "view"}
        />
      </div>
    </div>
  );
}
