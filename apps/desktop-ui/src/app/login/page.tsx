// v2.0.0 - 2026-03-13 - Email-first login with SSO Home Realm Discovery
"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { useRouter } from "next/navigation";
import { login, getSSOConfig, getSSOLoginUrl } from "@/lib/api";
import { ErrorAlert } from "@/components/ui/ErrorAlert";
import type { SSOConfig } from "@/lib/types";

type LoginMode = "email" | "password" | "sso";

export default function LoginPage() {
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);
  const [mode, setMode] = useState<LoginMode>("email");
  const [ssoConfig, setSsoConfig] = useState<SSOConfig | null>(null);
  const [ssoChecking, setSsoChecking] = useState(false);

  const router = useRouter();
  const passwordRef = useRef<HTMLInputElement>(null);
  const debounceRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const ssoCache = useRef<Record<string, SSOConfig>>({});

  // Extract domain from email
  const domain = email.includes("@") ? email.split("@")[1]?.toLowerCase() : "";

  // Debounced SSO config check on domain change
  const checkSSO = useCallback(
    async (domainToCheck: string) => {
      if (!domainToCheck || domainToCheck.length < 3 || !domainToCheck.includes(".")) {
        return;
      }

      // Check cache first
      if (ssoCache.current[domainToCheck]) {
        const cached = ssoCache.current[domainToCheck];
        setSsoConfig(cached);
        if (cached.enabled) {
          setMode("sso");
        }
        setSsoChecking(false);
        return;
      }

      setSsoChecking(true);
      try {
        const config = await getSSOConfig(domainToCheck);
        ssoCache.current[domainToCheck] = config;
        setSsoConfig(config);
        if (config.enabled) {
          setMode("sso");
        }
      } catch {
        // SSO check failed — fall back to password auth silently
        setSsoConfig(null);
      } finally {
        setSsoChecking(false);
      }
    },
    []
  );

  useEffect(() => {
    if (mode === "password") return; // User already chose password, don't re-check

    if (debounceRef.current) clearTimeout(debounceRef.current);

    if (!domain || domain.length < 3 || !domain.includes(".")) {
      setSsoConfig(null);
      if (mode === "sso") setMode("email");
      return;
    }

    debounceRef.current = setTimeout(() => checkSSO(domain), 400);

    return () => {
      if (debounceRef.current) clearTimeout(debounceRef.current);
    };
  }, [domain, checkSSO, mode]);

  // Focus password field when switching to password mode
  useEffect(() => {
    if (mode === "password") {
      passwordRef.current?.focus();
    }
  }, [mode]);

  function handleEmailContinue(e: React.FormEvent) {
    e.preventDefault();
    if (!email.trim()) return;
    setError("");

    if (mode === "sso" && ssoConfig?.enabled) {
      handleSSOLogin();
    } else {
      setMode("password");
    }
  }

  async function handlePasswordSubmit(e: React.FormEvent) {
    e.preventDefault();
    setError("");
    setLoading(true);

    try {
      await login(email.trim(), password);
      // Landing after login must be a route this product ships: /terminal/
      // belongs to marvisx and is not in the local perimeter, so a successful
      // login used to leave the user on a page that does not exist.
      router.push("/diario/");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Login failed");
    } finally {
      setLoading(false);
    }
  }

  async function handleSSOLogin() {
    setError("");
    setLoading(true);

    try {
      const { redirect_url } = await getSSOLoginUrl();
      // Store email in sessionStorage for callback page to use
      sessionStorage.setItem("sso_login_email", email.trim());
      window.location.href = redirect_url;
    } catch (err) {
      setError(err instanceof Error ? err.message : "SSO login failed");
      setLoading(false);
    }
  }

  function handleBackToEmail() {
    setMode("email");
    setPassword("");
    setError("");
    setSsoConfig(null);
  }

  return (
    <div className="flex items-center justify-center h-screen">
      <div className="bg-pir-surface-0 border border-pir rounded p-8 w-full max-w-sm">
        <h1 className="text-2xl font-semibold mb-6 text-center">
          Console Marvis
        </h1>

        {error && (
          <ErrorAlert message={error} className="mb-4 text-sm" />
        )}

        {/* Step 1: Email input (always visible) */}
        <form onSubmit={mode === "password" ? handlePasswordSubmit : handleEmailContinue}>
          <label className="block text-sm text-pir-text-secondary mb-2">
            Email
          </label>
          <input
            type="email"
            data-testid="email-input"
            value={email}
            onChange={(e) => {
              setEmail(e.target.value);
              if (mode === "password" || mode === "sso") {
                // Reset to email mode if user changes email
                setMode("email");
                setSsoConfig(null);
              }
            }}
            className="w-full bg-pir-base border border-pir rounded px-3 py-2 text-pir-text-primary focus:outline-none focus:border-pir-accent mb-4"
            autoFocus
            disabled={loading}
            placeholder="email@example.com"
          />

          {/* SSO checking indicator */}
          {ssoChecking && (
            <div className="flex items-center gap-2 text-xs text-pir-text-muted mb-4">
              <svg width="12" height="12" viewBox="0 0 12 12" fill="none" className="animate-spin">
                <circle cx="6" cy="6" r="4" stroke="currentColor" strokeWidth="1.2" opacity="0.3" />
                <path d="M6 2a4 4 0 012.83 1.17" stroke="currentColor" strokeWidth="1.2" strokeLinecap="round" />
              </svg>
              Checking login options...
            </div>
          )}

          {/* Step 2a: SSO button */}
          {mode === "sso" && ssoConfig?.enabled && (
            <div className="space-y-3">
              <button
                type="submit"
                data-testid="sso-login-button"
                disabled={loading}
                className="w-full bg-pir-accent text-white font-medium rounded px-4 py-2 hover:bg-pir-accent/90 disabled:opacity-50 disabled:cursor-not-allowed transition-colors flex items-center justify-center gap-2"
              >
                {loading ? (
                  "Redirecting..."
                ) : (
                  <>
                    <SSOIcon />
                    Sign in with SSO
                  </>
                )}
              </button>

              <button
                type="button"
                onClick={() => setMode("password")}
                className="w-full text-xs text-pir-text-muted hover:text-pir-text-secondary transition-colors"
              >
                Use password instead
              </button>
            </div>
          )}

          {/* Step 2b: Password field */}
          {mode === "password" && (
            <div>
              <div className="flex items-center justify-between mb-2">
                <label className="text-sm text-pir-text-secondary">
                  Password
                </label>
                <button
                  type="button"
                  onClick={handleBackToEmail}
                  className="text-[10px] text-pir-text-muted hover:text-pir-text-secondary"
                >
                  Change email
                </button>
              </div>
              <input
                ref={passwordRef}
                type="password"
                data-testid="password-input"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                className="w-full bg-pir-base border border-pir rounded px-3 py-2 text-pir-text-primary focus:outline-none focus:border-pir-accent mb-4"
                disabled={loading}
              />

              <button
                type="submit"
                data-testid="login-button"
                disabled={loading || !email.trim() || !password}
                className="w-full bg-pir-accent text-white font-medium rounded px-4 py-2 hover:bg-pir-accent/90 disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
              >
                {loading ? "Logging in..." : "Login"}
              </button>
            </div>
          )}

          {/* Step 1 continue: Email-only "Continue" button */}
          {mode === "email" && !ssoChecking && (
            <button
              type="submit"
              data-testid="continue-button"
              disabled={!email.trim() || loading}
              className="w-full bg-pir-accent text-white font-medium rounded px-4 py-2 hover:bg-pir-accent/90 disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
            >
              Continue
            </button>
          )}
        </form>
      </div>
    </div>
  );
}

function SSOIcon() {
  return (
    <svg width="14" height="14" viewBox="0 0 14 14" fill="none" className="shrink-0">
      <rect x="1" y="1" width="5" height="5" rx="1" stroke="currentColor" strokeWidth="1.2" />
      <rect x="8" y="1" width="5" height="5" rx="1" stroke="currentColor" strokeWidth="1.2" />
      <rect x="1" y="8" width="5" height="5" rx="1" stroke="currentColor" strokeWidth="1.2" />
      <rect x="8" y="8" width="5" height="5" rx="1" stroke="currentColor" strokeWidth="1.2" />
    </svg>
  );
}
