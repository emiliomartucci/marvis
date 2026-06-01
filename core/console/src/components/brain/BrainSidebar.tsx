"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";

const VIEWS = [
  { href: "/brain/", label: "Oggi" },
  { href: "/brain/diario/", label: "Diario" },
  { href: "/brain/stride/", label: "Stride" },
  { href: "/brain/memoria/", label: "Memoria" },
  { href: "/brain/da-decidere/", label: "Da decidere" },
  { href: "/brain/cronologia/", label: "Cronologia" },
] as const;

const SCOPES = ["company", "program", "project"] as const;

/** @public */
export interface BrainSidebarProps {
  cycleKey: string | null;
  scope: (typeof SCOPES)[number];
  onScopeChange?: (scope: (typeof SCOPES)[number]) => void;
  onCycleChange?: (cycle: string) => void;
  canRecompute?: boolean;
  recomputeMode?: string | null;
  onRecompute?: () => void;
  recomputing?: boolean;
}

function shiftCycleKey(cycleKey: string | null, deltaDays: number): string | null {
  if (!cycleKey) return null;
  // cycleKey format YYYY-MM-DD
  const [y, m, d] = cycleKey.split("-").map((v) => Number.parseInt(v, 10));
  if (!Number.isFinite(y) || !Number.isFinite(m) || !Number.isFinite(d)) return null;
  const dt = new Date(Date.UTC(y, m - 1, d));
  dt.setUTCDate(dt.getUTCDate() + deltaDays);
  const yyyy = dt.getUTCFullYear();
  const mm = String(dt.getUTCMonth() + 1).padStart(2, "0");
  const dd = String(dt.getUTCDate()).padStart(2, "0");
  return `${yyyy}-${mm}-${dd}`;
}

function isActive(pathname: string | null, href: string): boolean {
  if (!pathname) return false;
  const normalized = href.replace(/\/$/, "");
  return pathname === normalized || pathname.startsWith(`${normalized}/`);
}

export function BrainSidebar({
  cycleKey,
  scope,
  onScopeChange,
  onCycleChange,
  canRecompute = false,
  recomputeMode,
  onRecompute,
  recomputing = false,
}: BrainSidebarProps) {
  const pathname = usePathname();
  const prevCycle = shiftCycleKey(cycleKey, -1);
  const nextCycle = shiftCycleKey(cycleKey, 1);
  return (
    <aside
      className="flex w-60 flex-col gap-4 border-r border-pir-border bg-[hsl(var(--pir-surface-1))] px-4 py-4 text-pir-text-primary"
      style={{ borderRadius: "2px" }}
    >
      <section className="flex flex-col gap-1">
        <span className="font-[var(--font-jetbrains-mono)] text-[10px] uppercase tracking-[0.22em] text-pir-text-tertiary">
          CYCLE
        </span>
        <div className="flex items-center gap-2">
          <button
            type="button"
            onClick={() => prevCycle && onCycleChange?.(prevCycle)}
            disabled={!prevCycle || !onCycleChange}
            aria-label="Cycle precedente"
            className="border border-pir-border px-1.5 py-0.5 font-[var(--font-jetbrains-mono)] text-[12px] text-pir-text-secondary hover:text-pir-text-primary disabled:opacity-30"
            style={{ borderRadius: "2px" }}
          >
            ←
          </button>
          <span className="flex-1 font-[var(--font-exo-2)] text-base font-semibold text-center">
            {cycleKey ?? "—"}
          </span>
          <button
            type="button"
            onClick={() => nextCycle && onCycleChange?.(nextCycle)}
            disabled={!nextCycle || !onCycleChange}
            aria-label="Cycle successivo"
            className="border border-pir-border px-1.5 py-0.5 font-[var(--font-jetbrains-mono)] text-[12px] text-pir-text-secondary hover:text-pir-text-primary disabled:opacity-30"
            style={{ borderRadius: "2px" }}
          >
            →
          </button>
        </div>
        {onCycleChange && (
          <button
            type="button"
            onClick={() => onCycleChange("latest")}
            className="self-start font-[var(--font-jetbrains-mono)] text-[10px] uppercase tracking-[0.16em] text-pir-text-tertiary hover:text-[hsl(var(--pir-accent))]"
          >
            ↻ oggi
          </button>
        )}
      </section>

      <section className="flex flex-col gap-2">
        <span className="font-[var(--font-jetbrains-mono)] text-[10px] uppercase tracking-[0.22em] text-pir-text-tertiary">
          SCOPE
        </span>
        <div
          className="flex gap-1"
          title="In v1 lo scope cambia solo il diario (company / program / project). Drift, memoria e finding restano in vista globale finché v1.2 non aggiunge lo scope_key picker."
        >
          {SCOPES.map((s) => (
            <button
              type="button"
              key={s}
              onClick={() => onScopeChange?.(s)}
              className={`flex-1 border px-2 py-1 font-[var(--font-jetbrains-mono)] text-[11px] uppercase tracking-[0.16em] transition-colors ${
                scope === s
                  ? "border-[hsl(var(--pir-accent))] bg-[hsl(var(--pir-accent)/0.12)] text-pir-text-primary"
                  : "border-pir-border text-pir-text-secondary hover:text-pir-text-primary"
              }`}
              style={{ borderRadius: "2px" }}
            >
              {s}
            </button>
          ))}
        </div>
        <span className="font-[var(--font-jetbrains-mono)] text-[9px] uppercase tracking-[0.18em] text-pir-text-muted">
          v1 · filtra solo il diario
        </span>
      </section>

      <section className="flex flex-col gap-1">
        <span className="font-[var(--font-jetbrains-mono)] text-[10px] uppercase tracking-[0.22em] text-pir-text-tertiary">
          VIEW
        </span>
        <nav aria-label="Brain tab" className="flex flex-col gap-0.5">
          {VIEWS.map((view) => {
            const active = isActive(pathname, view.href);
            return (
              <Link
                key={view.href}
                href={view.href}
                prefetch={false}
                className={`px-2 py-1.5 font-[var(--font-exo-2)] text-sm transition-colors ${
                  active
                    ? "bg-[hsl(var(--pir-surface-2))] text-pir-text-primary"
                    : "text-pir-text-secondary hover:text-pir-text-primary"
                }`}
                style={{ borderRadius: "2px" }}
              >
                {view.label}
              </Link>
            );
          })}
        </nav>
      </section>

      <section className="mt-auto flex items-center justify-between gap-2">
        <span className="font-[var(--font-jetbrains-mono)] text-[10px] uppercase tracking-[0.18em] text-pir-text-tertiary">
          {recomputeMode ?? "shadow"} mode
        </span>
        {canRecompute && (
          <button
            type="button"
            onClick={onRecompute}
            disabled={recomputing}
            className="border border-pir-border-strong bg-[hsl(var(--pir-surface-2))] px-2 py-1 font-[var(--font-jetbrains-mono)] text-[11px] uppercase tracking-[0.16em] text-pir-text-primary hover:bg-[hsl(var(--pir-surface-3))] disabled:opacity-50"
            style={{ borderRadius: "2px" }}
          >
            {recomputing ? "…" : "recompute"}
          </button>
        )}
      </section>
    </aside>
  );
}
