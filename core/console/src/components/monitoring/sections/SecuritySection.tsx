"use client";

import { useEffect, useRef, useState } from "react";
import { getMonitoringSecurity } from "@/lib/api";
import type { SecurityData, SecuritySummary } from "@/lib/types";

interface Props {
  securitySummary?: SecuritySummary | null;
}

type Tab = "ssh" | "console";

export default function SecuritySection({ securitySummary }: Props) {
  const [data, setData] = useState<SecurityData | null>(null);
  const [loading, setLoading] = useState(false);
  const [loaded, setLoaded] = useState(false);
  const [tab, setTab] = useState<Tab>("ssh");
  const sectionRef = useRef<HTMLElement>(null);

  // Lazy load: fetch only when section scrolls into view
  useEffect(() => {
    if (loaded) return;

    const observer = new IntersectionObserver(
      (entries) => {
        if (entries[0]?.isIntersecting && !loaded) {
          setLoaded(true);
          setLoading(true);
          const controller = new AbortController();
          getMonitoringSecurity({ signal: controller.signal })
            .then(setData)
            .catch(() => {})
            .finally(() => setLoading(false));
        }
      },
      { rootMargin: "200px" }
    );

    if (sectionRef.current) observer.observe(sectionRef.current);
    return () => observer.disconnect();
  }, [loaded]);

  const sshEvents = data?.ssh_events ?? [];
  const consoleLogins = data?.console_logins ?? [];
  const summary = data?.ssh_summary_24h;
  const bans = data?.active_bans ?? [];

  return (
    <section id="security" ref={sectionRef}>
      <h2 className="text-body font-medium text-pir-text-primary mb-3">
        Security
      </h2>

      <div className="space-y-3">
        {/* Inline security summary from snapshot (always visible) */}
        {securitySummary && (
          <div className="flex flex-wrap gap-3 text-caption">
            <div className="flex items-center gap-1.5">
              <span
                className={`w-1.5 h-1.5 rounded-full ${securitySummary.ssh_failed_24h > 20 ? "bg-yellow-400" : "bg-green-500"}`}
              />
              <span className="text-pir-text-muted">SSH failed (24h):</span>
              <span
                className={
                  securitySummary.ssh_failed_24h > 20
                    ? "text-yellow-400 font-mono"
                    : "text-pir-text-secondary font-mono"
                }
              >
                {securitySummary.ssh_failed_24h}
              </span>
            </div>
            <div className="flex items-center gap-1.5">
              <span
                className={`w-1.5 h-1.5 rounded-full ${securitySummary.ssh_success_24h > 0 ? "bg-green-500" : "bg-gray-500"}`}
              />
              <span className="text-pir-text-muted">SSH success (24h):</span>
              <span className="text-pir-text-secondary font-mono">
                {securitySummary.ssh_success_24h}
              </span>
            </div>
            <div className="flex items-center gap-1.5">
              <span
                className={`w-1.5 h-1.5 rounded-full ${securitySummary.bans_active > 0 ? "bg-red-500" : "bg-green-500"}`}
              />
              <span className="text-pir-text-muted">Active bans:</span>
              <span
                className={
                  securitySummary.bans_active > 0
                    ? "text-red-400 font-mono"
                    : "text-pir-text-secondary font-mono"
                }
              >
                {securitySummary.bans_active}
              </span>
            </div>
          </div>
        )}

        {loading ? (
          <div className="text-caption text-pir-text-muted border border-pir rounded p-4 text-center">
            Loading security data...
          </div>
        ) : !data ? (
          <div className="text-caption text-pir-text-muted border border-pir rounded p-4 text-center">
            Scroll here to load security data
          </div>
        ) : (
          <>
            {/* SSH detailed summary */}
            {summary && (
              <div className="grid grid-cols-3 gap-3">
                <div className="border border-pir rounded p-3 bg-pir-surface-0">
                  <div className="text-caption text-pir-text-muted">SSH OK (24h)</div>
                  <div className="text-lg font-mono tabular-nums text-green-400">
                    {summary.success_count}
                  </div>
                </div>
                <div className="border border-pir rounded p-3 bg-pir-surface-0">
                  <div className="text-caption text-pir-text-muted">SSH Failed (24h)</div>
                  <div className="text-lg font-mono tabular-nums text-red-400">
                    {summary.failed_count}
                  </div>
                </div>
                <div className="border border-pir rounded p-3 bg-pir-surface-0">
                  <div className="text-caption text-pir-text-muted">Unique IPs</div>
                  <div className="text-lg font-mono tabular-nums text-pir-text-primary">
                    {summary.unique_ips}
                  </div>
                </div>
              </div>
            )}

            {/* Tab switcher: SSH Events / Console Logins */}
            <div className="border border-pir rounded overflow-hidden">
              <div className="flex border-b border-pir bg-pir-surface-0">
                <button
                  onClick={() => setTab("ssh")}
                  className={`px-3 py-2 text-caption transition-colors ${
                    tab === "ssh"
                      ? "text-pir-text-primary border-b-2 border-pir-accent"
                      : "text-pir-text-muted hover:text-pir-text-secondary"
                  }`}
                >
                  SSH Events ({sshEvents.length})
                </button>
                <button
                  onClick={() => setTab("console")}
                  className={`px-3 py-2 text-caption transition-colors ${
                    tab === "console"
                      ? "text-pir-text-primary border-b-2 border-pir-accent"
                      : "text-pir-text-muted hover:text-pir-text-secondary"
                  }`}
                >
                  Console Logins ({consoleLogins.length})
                </button>
              </div>

              <div className="max-h-[240px] overflow-y-auto">
                {tab === "ssh" ? (
                  sshEvents.length === 0 ? (
                    <div className="px-3 py-4 text-caption text-pir-text-muted text-center">
                      No recent SSH events
                    </div>
                  ) : (
                    <table className="w-full text-caption">
                      <tbody>
                        {sshEvents.map((e, i) => (
                          <tr key={i} className="border-t border-pir first:border-t-0">
                            <td className="px-3 py-1.5 font-mono text-pir-text-muted">
                              {new Date(e.timestamp * 1000).toLocaleString()}
                            </td>
                            <td className="px-3 py-1.5">
                              <span
                                className={
                                  e.event_type === "ssh_login"
                                    ? "text-green-400"
                                    : "text-red-400"
                                }
                              >
                                {e.event_type === "ssh_login" ? "OK" : "FAIL"}
                              </span>
                            </td>
                            <td className="px-3 py-1.5 font-mono text-pir-text-secondary">
                              {e.source_ip ?? "-"}
                            </td>
                            <td className="px-3 py-1.5 text-pir-text-secondary">
                              {e.username ?? "-"}
                            </td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  )
                ) : consoleLogins.length === 0 ? (
                  <div className="px-3 py-4 text-caption text-pir-text-muted text-center">
                    No recent console logins
                  </div>
                ) : (
                  <table className="w-full text-caption">
                    <tbody>
                      {consoleLogins.map((e, i) => (
                        <tr key={i} className="border-t border-pir first:border-t-0">
                          <td className="px-3 py-1.5 font-mono text-pir-text-muted">
                            {new Date(e.timestamp * 1000).toLocaleString()}
                          </td>
                          <td className="px-3 py-1.5 text-pir-text-secondary">
                            {e.username ?? "-"}
                          </td>
                          <td className="px-3 py-1.5 font-mono text-pir-text-muted">
                            {e.source_ip ?? "-"}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                )}
              </div>
            </div>

            {/* Active Bans */}
            {bans.length > 0 && (
              <div className="border border-pir rounded overflow-hidden">
                <div className="bg-pir-surface-0 px-3 py-2 text-caption text-pir-text-muted border-b border-pir">
                  Active Bans ({bans.length})
                </div>
                <div className="divide-y divide-pir">
                  {bans.map((b, i) => (
                    <div key={i} className="flex items-center gap-3 px-3 py-1.5 text-caption">
                      <span className="font-mono text-pir-text-secondary">{b.ip}</span>
                      <span className="text-pir-text-muted">{b.jail}</span>
                      <span className="text-pir-text-muted ml-auto font-mono">
                        {new Date(b.timestamp * 1000).toLocaleString()}
                      </span>
                    </div>
                  ))}
                </div>
              </div>
            )}

            {data.ban_count_24h > 0 && (
              <div className="text-caption text-pir-text-muted">
                Total bans (24h): {data.ban_count_24h}
              </div>
            )}
          </>
        )}
      </div>
    </section>
  );
}
