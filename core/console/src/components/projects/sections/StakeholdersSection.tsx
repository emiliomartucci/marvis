// v1.0.0 - 2026-04-22 - §6 Stakeholders (Phase 1 placeholder) (PR #9)
"use client";

import { useEffect, useState } from "react";
import SectionShell from "./SectionShell";
import { getProjectRaci, getMe } from "@/lib/api";
import type { RaciEntry } from "@/lib/types";

function initials(name: string): string {
  const parts = name.trim().split(/\s+/).slice(0, 2);
  return parts.map((p) => p[0]?.toUpperCase() ?? "").join("") || "—";
}

const ROLE_LABEL: Record<string, string> = {
  responsible: "R · RESPONSIBLE",
  accountable: "A · ACCOUNTABLE",
  consulted:   "C · CONSULTED",
  informed:    "I · INFORMED",
};

function StakeholdersSection({
  slug,
  onOpenRaci,
}: {
  slug: string;
  onOpenRaci: () => void;
}) {
  const [entries, setEntries] = useState<RaciEntry[] | null>(null);
  const [loading, setLoading] = useState(true);
  const [me, setMe] = useState<string>("Owner");

  useEffect(() => {
    getMe()
      .then((u) => setMe(u.display_name || u.username))
      .catch(() => {});
    getProjectRaci(slug)
      .then((res) => setEntries(res))
      .catch(() => setEntries([]))
      .finally(() => setLoading(false));
  }, [slug]);

  const hasEntries = (entries?.length ?? 0) > 0;
  const action = (
    <button
      type="button"
      onClick={onOpenRaci}
      className="text-pir-text-tertiary hover:text-pir-accent transition-colors bg-transparent border border-pir cursor-pointer"
      style={{
        fontFamily: "var(--pir-font-mono, ui-monospace, monospace)",
        fontSize: 10,
        fontWeight: 500,
        letterSpacing: "0.12em",
        textTransform: "uppercase",
        padding: "4px 8px",
        borderRadius: 2,
      }}
    >
      {hasEntries ? "↗ Full RACI" : "Phase 2 · team wiring"}
    </button>
  );

  return (
    <SectionShell
      anchorId="stakeholders"
      eyebrow="Stakeholders · Phase 1"
      title={hasEntries ? "RACI · team assignments" : "RACI · single operator"}
      action={action}
    >
      {loading ? (
        <div className="text-pir-text-tertiary text-sm px-2 py-4">Loading RACI…</div>
      ) : (
        <div className="grid gap-2.5" style={{ gridTemplateColumns: "repeat(auto-fit, minmax(200px, 1fr))" }}>
          {hasEntries ? (
            entries!.map((entry) => (
              <div
                key={`${entry.role}-${entry.user.id}`}
                className="bg-pir-surface-0 border border-pir flex items-center gap-2.5"
                style={{ borderRadius: 4, padding: "10px 12px" }}
              >
                <div
                  className="bg-pir-accent/15 text-pir-accent inline-flex items-center justify-center shrink-0"
                  style={{
                    width: 28,
                    height: 28,
                    borderRadius: "50%",
                    fontFamily: "var(--pir-font-mono, ui-monospace, monospace)",
                    fontWeight: 700,
                    fontSize: 11,
                  }}
                >
                  {initials(entry.user.display_name || entry.user.slug)}
                </div>
                <div>
                  <div className="text-pir-text-primary font-semibold text-[12.5px] leading-tight">
                    {entry.user.display_name || entry.user.slug}
                  </div>
                  <div
                    className="text-pir-text-tertiary uppercase mt-1"
                    style={{
                      fontFamily: "var(--pir-font-mono, ui-monospace, monospace)",
                      fontSize: 10,
                      fontWeight: 500,
                      letterSpacing: "0.14em",
                    }}
                  >
                    {ROLE_LABEL[entry.role] || entry.role}
                  </div>
                </div>
              </div>
            ))
          ) : (
            <>
              <div
                className="bg-pir-surface-0 border border-pir flex items-center gap-2.5"
                style={{ borderRadius: 4, padding: "10px 12px" }}
              >
                <div
                  className="bg-pir-accent/15 text-pir-accent inline-flex items-center justify-center shrink-0"
                  style={{
                    width: 28,
                    height: 28,
                    borderRadius: "50%",
                    fontFamily: "var(--pir-font-mono, ui-monospace, monospace)",
                    fontWeight: 700,
                    fontSize: 11,
                  }}
                >
                  {initials(me)}
                </div>
                <div>
                  <div className="text-pir-text-primary font-semibold text-[12.5px] leading-tight">
                    {me}
                  </div>
                  <div
                    className="text-pir-text-tertiary uppercase mt-1"
                    style={{
                      fontFamily: "var(--pir-font-mono, ui-monospace, monospace)",
                      fontSize: 10,
                      fontWeight: 500,
                      letterSpacing: "0.14em",
                    }}
                  >
                    OWNER · OPERATOR
                  </div>
                </div>
              </div>
              <div
                className="bg-pir-surface-0 border border-pir flex items-center gap-2.5"
                style={{ borderRadius: 4, padding: "10px 12px" }}
              >
                <div
                  className="inline-flex items-center justify-center shrink-0"
                  style={{
                    width: 28,
                    height: 28,
                    borderRadius: "50%",
                    background: "hsl(var(--pir-secondary-bright) / 0.15)",
                    color: "hsl(var(--pir-secondary-bright))",
                    fontFamily: "var(--pir-font-mono, ui-monospace, monospace)",
                    fontWeight: 700,
                    fontSize: 11,
                  }}
                >
                  AI
                </div>
                <div>
                  <div className="text-pir-text-primary font-semibold text-[12.5px] leading-tight">
                    marvisx agent
                  </div>
                  <div
                    className="text-pir-text-tertiary uppercase mt-1"
                    style={{
                      fontFamily: "var(--pir-font-mono, ui-monospace, monospace)",
                      fontSize: 10,
                      fontWeight: 500,
                      letterSpacing: "0.14em",
                    }}
                  >
                    IMPLEMENTER · AGENT
                  </div>
                </div>
              </div>
              <div
                className="text-pir-text-tertiary flex items-center justify-center border border-pir border-dashed"
                style={{
                  borderRadius: 4,
                  padding: "14px 16px",
                  fontFamily: "var(--pir-font-sans, system-ui)",
                  fontSize: 12,
                  textAlign: "center",
                }}
              >
                Phase 2 → team members + external stakeholders
              </div>
            </>
          )}
        </div>
      )}
    </SectionShell>
  );
}

export default StakeholdersSection;
