"use client";

import { useEffect, useState } from "react";
import type { ReactNode } from "react";
import { getPrograms } from "@/lib/api";
import { API_BASE_URL } from "@/lib/config";

const CONSOLE_VERSION = "1.0.0";
const API_BASE_LABEL = API_BASE_URL || "same origin";

function InfoRow({ label, value }: { label: string; value: ReactNode }) {
  return (
    <div className="flex items-center justify-between py-3 border-b border-pir last:border-b-0">
      <span className="text-body text-pir-text-secondary">{label}</span>
      <span className="text-body text-pir-text-primary font-medium">{value}</span>
    </div>
  );
}

function SectionCard({
  title,
  children,
}: {
  title: string;
  children: ReactNode;
}) {
  return (
    <div className="bg-pir-surface-1 border border-pir rounded-lg overflow-hidden">
      <div className="px-5 py-3 border-b border-pir">
        <h2 className="text-label text-pir-text-muted uppercase tracking-wider">{title}</h2>
      </div>
      <div className="px-5">{children}</div>
    </div>
  );
}

export default function GeneralPage() {
  const [projectCount, setProjectCount] = useState<number | null>(null);
  const [loadingProjects, setLoadingProjects] = useState(true);

  useEffect(() => {
    getPrograms()
      .then((programs) => {
        const total = programs.reduce(
          (acc, p) => acc + (p.projects?.length ?? 0),
          0
        );
        setProjectCount(total);
      })
      .catch(() => setProjectCount(null))
      .finally(() => setLoadingProjects(false));
  }, []);

  return (
    <div className="h-full overflow-y-auto">
      <div className="p-6 max-w-2xl space-y-6">
        <div>
          <h1 className="text-heading text-pir-text-primary">General</h1>
          <p className="text-body text-pir-text-muted mt-1">
            System information and configuration overview.
          </p>
        </div>

        <SectionCard title="System">
          <InfoRow
            label="Projects"
            value={
              loadingProjects ? (
                <span className="text-pir-text-muted">Loading...</span>
              ) : projectCount !== null ? (
                projectCount
              ) : (
                <span className="text-pir-text-muted">—</span>
              )
            }
          />
          <InfoRow
            label="Console version"
            value={
              <span className="font-mono text-sm bg-pir-surface-2 px-2 py-0.5 rounded border border-pir">
                v{CONSOLE_VERSION}
              </span>
            }
          />
          <InfoRow
            label="API endpoint"
            value={
              <span className="font-mono text-sm text-pir-text-secondary">
                {API_BASE_LABEL}
              </span>
            }
          />
          <InfoRow
            label="Data retention"
            value="Session data retained for 90 days"
          />
        </SectionCard>

        <SectionCard title="About Marvis">
          <InfoRow label="Product" value="Marvis" />
          <InfoRow
            label="Version"
            value={
              <span className="font-mono text-sm bg-pir-surface-2 px-2 py-0.5 rounded border border-pir">
                v4.0
              </span>
            }
          />
          <InfoRow
            label="Documentation"
            value={<span className="text-sm text-pir-text-secondary">Local console</span>}
          />
          <InfoRow
            label="Stack"
            value={
              <span className="text-pir-text-secondary text-sm">
                Next.js 15 + FastAPI + SQLite
              </span>
            }
          />
        </SectionCard>

        <div className="text-caption text-pir-text-muted px-1">
          Settings are managed server-side. Contact the administrator to change configuration values.
        </div>
      </div>
    </div>
  );
}
