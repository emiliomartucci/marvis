"use client";

import { useEffect, useState } from "react";
import { MonitoringSnapshot } from "@/lib/types";

const SECTIONS = [
  { id: "system", label: "System" },
  { id: "docker", label: "Docker" },
  { id: "network", label: "Network" },
  { id: "services", label: "Services" },
  { id: "security", label: "Security" },
];

interface HealthBarProps {
  snapshot: MonitoringSnapshot | null;
  stale: boolean;
}

function getSectionHealth(
  snapshot: MonitoringSnapshot | null,
  sectionId: string
): "ok" | "warn" | "unknown" {
  if (!snapshot) return "unknown";

  if (sectionId === "system") {
    const hasAlert = snapshot.alerts.some((a) =>
      ["cpu_pct", "ram_pct", "disk_pct"].includes(a.metric)
    );
    return hasAlert ? "warn" : "ok";
  }

  if (sectionId === "docker") {
    const unhealthy = snapshot.docker.some(
      (c) => c.status !== "running" && c.status !== "Up"
    );
    return unhealthy ? "warn" : "ok";
  }

  if (sectionId === "network") {
    const { tailscale, cf_tunnel } = snapshot.network;
    if (tailscale === "ok" && cf_tunnel === "ok") return "ok";
    if (tailscale === "unknown" && cf_tunnel === "unknown") return "unknown";
    return "warn";
  }

  if (sectionId === "services") {
    const unhealthy = snapshot.services.some(
      (s) => s.status !== "active" && s.status !== "running"
    );
    return unhealthy ? "warn" : "ok";
  }

  if (sectionId === "security") {
    const { bans_active, ssh_failed_24h } = snapshot.security_summary;
    if (bans_active > 0 || ssh_failed_24h > 20) return "warn";
    return "ok";
  }

  return "ok";
}

const DOT_COLORS = {
  ok: "bg-green-500",
  warn: "bg-yellow-400",
  unknown: "bg-pir-text-muted",
};

export default function HealthBar({ snapshot, stale }: HealthBarProps) {
  const [active, setActive] = useState("system");

  useEffect(() => {
    const observer = new IntersectionObserver(
      (entries) => {
        for (const entry of entries) {
          if (entry.isIntersecting) {
            setActive(entry.target.id);
          }
        }
      },
      { rootMargin: "-20% 0px -70% 0px" }
    );

    for (const section of SECTIONS) {
      const el = document.getElementById(section.id);
      if (el) observer.observe(el);
    }

    return () => observer.disconnect();
  }, []);

  const handleClick = (id: string) => {
    document.getElementById(id)?.scrollIntoView({ behavior: "smooth" });
  };

  const updatedAt = snapshot
    ? new Date(snapshot.timestamp * 1000).toLocaleTimeString()
    : null;

  return (
    <div className="sticky top-0 z-10 bg-pir-surface-0 border-b border-pir flex items-center gap-1 px-4 py-2">
      {SECTIONS.map((section) => {
        const health = getSectionHealth(snapshot, section.id);
        const isActive = active === section.id;
        return (
          <button
            key={section.id}
            onClick={() => handleClick(section.id)}
            className={`flex items-center gap-1.5 px-2.5 py-1 rounded text-caption transition-colors ${
              isActive
                ? "text-pir-text-primary bg-pir-surface-1"
                : "text-pir-text-muted hover:text-pir-text-secondary hover:bg-pir-surface-1"
            }`}
          >
            <span
              className={`w-1.5 h-1.5 rounded-full shrink-0 ${DOT_COLORS[health]}`}
            />
            {section.label}
          </button>
        );
      })}

      <div className="ml-auto flex items-center gap-3 text-caption text-pir-text-muted">
        {stale && <span className="text-yellow-400">stale</span>}
        {updatedAt && <span>{updatedAt}</span>}
      </div>
    </div>
  );
}
