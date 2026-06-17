"use client";

import { useState } from "react";
import { API_BASE_URL } from "@/lib/config";

const API_BASE = API_BASE_URL || "";
const API_BASE_LABEL = API_BASE || "same origin";

// Masked token display: shows last 4 chars only
const MASKED_TOKEN = "***...***x4f2";

const CURL_EXAMPLE = `curl -H "Authorization: Bearer {TOKEN}" \\
  ${API_BASE || "<same-origin>"}/api/v1/tasks`;

function SectionCard({
  title,
  children,
}: {
  title: string;
  children: React.ReactNode;
}) {
  return (
    <div className="bg-pir-surface-1 border border-pir rounded-lg overflow-hidden">
      <div className="px-5 py-3 border-b border-pir">
        <h2 className="text-label text-pir-text-muted uppercase tracking-wider">{title}</h2>
      </div>
      <div className="px-5 py-4">{children}</div>
    </div>
  );
}

function CopyButton({ text }: { text: string }) {
  const [copied, setCopied] = useState(false);

  async function handleCopy() {
    try {
      await navigator.clipboard.writeText(text);
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    } catch {
      // fallback: select text
    }
  }

  return (
    <button
      onClick={handleCopy}
      className={`text-caption px-3 py-1.5 rounded border transition-colors ${
        copied
          ? "bg-green-500/20 text-green-400 border-green-500/30"
          : "bg-pir-surface-2 text-pir-text-secondary border-pir hover:border-pir-strong hover:text-pir-text-primary"
      }`}
    >
      {copied ? "Copied!" : "Copy"}
    </button>
  );
}

export default function TokensPage() {
  return (
    <div className="h-full overflow-y-auto">
      <div className="p-6 max-w-2xl space-y-6">
        <div>
          <h1 className="text-heading text-pir-text-primary">Tokens</h1>
          <p className="text-body text-pir-text-muted mt-1">
            API authentication tokens for programmatic access.
          </p>
        </div>

        <SectionCard title="API Token">
          <div className="space-y-4">
            <div>
              <label className="text-label text-pir-text-secondary block mb-2">
                Current Token
              </label>
              <div className="flex items-center gap-3">
                <div className="flex-1 bg-pir-surface-2 border border-pir rounded px-3 py-2 font-mono text-sm text-pir-text-muted select-none">
                  {MASKED_TOKEN}
                </div>
              </div>
              <p className="text-caption text-pir-text-muted mt-2">
                Token is masked for security. Only the last 4 characters are shown.
              </p>
            </div>

            <div className="flex items-center gap-3 pt-1">
              <div className="relative group">
                <button
                  disabled
                  className="px-4 py-2 bg-pir-surface-2 border border-pir rounded text-body text-pir-text-muted opacity-60 cursor-not-allowed"
                >
                  Regenerate Token
                </button>
                <div className="absolute bottom-full left-0 mb-1.5 hidden group-hover:block z-10">
                  <div className="bg-pir-surface-1 border border-pir rounded px-3 py-1.5 text-caption text-pir-text-secondary whitespace-nowrap shadow-lg">
                    Contact admin to regenerate
                  </div>
                </div>
              </div>
            </div>
          </div>
        </SectionCard>

        <SectionCard title="API Usage">
          <div className="space-y-4">
            <div className="flex items-center justify-between">
              <span className="text-body text-pir-text-secondary">Base URL</span>
              <span className="font-mono text-sm text-pir-text-primary bg-pir-surface-2 px-2 py-0.5 rounded border border-pir">
                {API_BASE_LABEL}
              </span>
            </div>

            <div>
              <div className="flex items-center justify-between mb-2">
                <label className="text-label text-pir-text-secondary">
                  Example request
                </label>
                <CopyButton
                  text={`curl -H "Authorization: Bearer {TOKEN}" ${API_BASE || "<same-origin>"}/api/v1/tasks`}
                />
              </div>
              <div className="bg-pir-surface-2 border border-pir rounded p-3 font-mono text-sm text-pir-text-secondary whitespace-pre overflow-x-auto">
                {CURL_EXAMPLE}
              </div>
            </div>

            <div className="bg-pir-surface-2 border border-pir rounded p-4 space-y-2">
              <p className="text-label text-pir-text-secondary">
                Common endpoints
              </p>
              {[
                { method: "GET", path: "/api/v1/tasks", desc: "List tasks" },
                { method: "GET", path: "/api/v1/projects", desc: "List projects" },
                { method: "GET", path: "/api/v1/users", desc: "List users" },
                { method: "GET", path: "/api/v1/sessions", desc: "List sessions" },
              ].map(({ method, path, desc }) => (
                <div key={path} className="flex items-center gap-3">
                  <span className="text-caption font-mono bg-pir-accent/15 text-pir-accent px-1.5 py-0.5 rounded w-10 text-center flex-shrink-0">
                    {method}
                  </span>
                  <span className="font-mono text-sm text-pir-text-primary flex-1">
                    {path}
                  </span>
                  <span className="text-caption text-pir-text-muted">{desc}</span>
                </div>
              ))}
            </div>
          </div>
        </SectionCard>

        <div className="text-caption text-pir-text-muted px-1">
          Tokens are bound to your user account. Do not share your token.
        </div>
      </div>
    </div>
  );
}
