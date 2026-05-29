// v1.0.0 - 2026-03-13 - SSO configuration panel (super_admin only)
"use client";

import { useEffect, useState } from "react";
import { getWorkspaceSSOConfig, updateWorkspaceSSOConfig } from "@/lib/api";
import { useWorkspace } from "@/lib/workspace";

const DOMAIN_RE = /^[a-z0-9.-]+\.[a-z]{2,}$/;

export default function SSOConfigPanel() {
  const { workspaceId } = useWorkspace();
  const [enabled, setEnabled] = useState(false);
  const [domains, setDomains] = useState<string[]>([]);
  const [newDomain, setNewDomain] = useState("");
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [success, setSuccess] = useState(false);

  useEffect(() => {
    setLoading(true);
    getWorkspaceSSOConfig(workspaceId)
      .then((config) => {
        setEnabled(config.enabled);
        setDomains(config.email_domains);
      })
      .catch(() => {
        // SSO config endpoint may not exist yet
      })
      .finally(() => setLoading(false));
  }, [workspaceId]);

  function addDomain() {
    const d = newDomain.trim().toLowerCase();
    if (!d || !DOMAIN_RE.test(d)) return;
    if (domains.includes(d)) {
      setError("Domain already added");
      return;
    }
    setDomains([...domains, d]);
    setNewDomain("");
    setError(null);
  }

  function removeDomain(domain: string) {
    setDomains(domains.filter((d) => d !== domain));
  }

  async function handleSave() {
    setSaving(true);
    setError(null);
    setSuccess(false);
    try {
      await updateWorkspaceSSOConfig(workspaceId, { enabled, email_domains: domains });
      setSuccess(true);
      setTimeout(() => setSuccess(false), 3000);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to save SSO config");
    } finally {
      setSaving(false);
    }
  }

  if (loading) {
    return <div className="animate-pulse h-32 bg-pir-surface-0 rounded" />;
  }

  return (
    <div className="space-y-4">
      <h3 className="text-sm font-medium text-pir-text-primary">SSO Configuration</h3>

      {error && (
        <div className="text-xs text-red-600 dark:text-red-400 bg-red-50 dark:bg-red-900/20 border border-red-200 dark:border-red-800 rounded px-3 py-2">
          {error}
        </div>
      )}

      {success && (
        <div className="text-xs text-emerald-600 dark:text-emerald-400 bg-emerald-50 dark:bg-emerald-900/20 border border-emerald-200 dark:border-emerald-800 rounded px-3 py-2">
          SSO configuration saved
        </div>
      )}

      {/* Toggle */}
      <label className="flex items-center gap-3 cursor-pointer">
        <button
          type="button"
          onClick={() => setEnabled(!enabled)}
          className={`relative w-10 h-5 rounded-full transition-colors ${
            enabled ? "bg-pir-accent" : "bg-pir-surface-3"
          }`}
        >
          <span className={`absolute top-0.5 left-0.5 w-4 h-4 bg-white rounded-full transition-transform ${
            enabled ? "translate-x-5" : ""
          }`} />
        </button>
        <span className="text-sm text-pir-text-secondary">
          {enabled ? "SSO enabled" : "SSO disabled"}
        </span>
      </label>

      {/* Email domains */}
      {enabled && (
        <div className="space-y-2">
          <label className="block text-xs text-pir-text-muted">Allowed email domains</label>
          <div className="flex gap-2">
            <input
              type="text"
              value={newDomain}
              onChange={(e) => setNewDomain(e.target.value)}
              onKeyDown={(e) => { if (e.key === "Enter") { e.preventDefault(); addDomain(); } }}
              placeholder="company.com"
              className="flex-1 bg-pir-base border border-pir rounded px-3 py-1.5 text-xs text-pir-text-primary focus:outline-none focus:border-pir-accent"
            />
            <button
              onClick={addDomain}
              disabled={!newDomain.trim() || !DOMAIN_RE.test(newDomain.trim().toLowerCase())}
              className="px-3 py-1.5 bg-pir-surface-2 border border-pir rounded text-xs text-pir-text-secondary hover:bg-pir-surface-3 disabled:opacity-50 transition-colors"
            >
              Add
            </button>
          </div>

          {domains.length > 0 && (
            <div className="flex flex-wrap gap-1.5">
              {domains.map((d) => (
                <span key={d} className="inline-flex items-center gap-1 px-2 py-0.5 bg-pir-surface-2 rounded text-xs text-pir-text-secondary">
                  {d}
                  <button onClick={() => removeDomain(d)} className="text-pir-text-muted hover:text-red-500">
                    <svg width="10" height="10" viewBox="0 0 10 10" fill="none">
                      <path d="M2.5 2.5l5 5M7.5 2.5l-5 5" stroke="currentColor" strokeWidth="1.2" strokeLinecap="round" />
                    </svg>
                  </button>
                </span>
              ))}
            </div>
          )}
        </div>
      )}

      {/* Save button */}
      <button
        onClick={handleSave}
        disabled={saving}
        className="px-4 py-2 bg-pir-accent text-white text-xs font-medium rounded hover:bg-pir-accent/90 disabled:opacity-50 transition-colors"
      >
        {saving ? "Saving..." : "Save SSO Config"}
      </button>
    </div>
  );
}
