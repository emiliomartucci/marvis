// v3.0.0 - 2026-07-28 - WorkOS-first hosted login with fail-closed discovery
"use client";

import { useEffect, useRef, useState } from "react";
import { useRouter } from "next/navigation";
import { getSSOConfig, login, startSSOLogin } from "@/lib/api";
import { ErrorAlert } from "@/components/ui/ErrorAlert";

type LoginMode = "checking" | "sso" | "email" | "password" | "error";

export default function LoginPage() {
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);
  const [mode, setMode] = useState<LoginMode>("checking");
  const [configAttempt, setConfigAttempt] = useState(0);
  const passwordRef = useRef<HTMLInputElement>(null);
  const router = useRouter();

  useEffect(() => {
    let cancelled = false;

    async function resolveLoginMode() {
      setError("");
      try {
        const config = await getSSOConfig("ws_default");
        if (!cancelled) {
          setMode(config.enabled ? "sso" : "email");
        }
      } catch {
        if (!cancelled) {
          setError("Login options are temporarily unavailable.");
          setMode("error");
        }
      }
    }

    void resolveLoginMode();
    return () => {
      cancelled = true;
    };
  }, [configAttempt]);

  useEffect(() => {
    if (mode === "password") {
      passwordRef.current?.focus();
    }
  }, [mode]);

  function handleEmailContinue(event: React.FormEvent) {
    event.preventDefault();
    if (!email.trim()) return;
    setError("");
    setMode("password");
  }

  async function handlePasswordSubmit(event: React.FormEvent) {
    event.preventDefault();
    setError("");
    setLoading(true);

    try {
      await login(email.trim(), password);
      router.push("/terminal/");
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Login failed");
    } finally {
      setLoading(false);
    }
  }

  function handleWorkOSLogin() {
    setError("");
    setLoading(true);
    startSSOLogin("ws_default");
  }

  function handleBackToEmail() {
    setMode("email");
    setPassword("");
    setError("");
  }

  function retryDiscovery() {
    setMode("checking");
    setConfigAttempt((attempt) => attempt + 1);
  }

  return (
    <div className="flex items-center justify-center h-screen">
      <div className="bg-pir-surface-0 border border-pir rounded p-8 w-full max-w-sm">
        <h1 className="text-2xl font-semibold mb-6 text-center">
          Console Marvis
        </h1>

        {error && <ErrorAlert message={error} className="mb-4 text-sm" />}

        {mode === "checking" && (
          <div className="flex items-center justify-center gap-2 text-sm text-pir-text-muted">
            <Spinner />
            Checking login options...
          </div>
        )}

        {mode === "error" && (
          <button
            type="button"
            onClick={retryDiscovery}
            className="w-full border border-pir rounded px-4 py-2 text-sm text-pir-text-secondary hover:text-pir-text-primary"
          >
            Retry
          </button>
        )}

        {mode === "sso" && (
          <div className="space-y-4">
            <p className="text-sm text-center text-pir-text-secondary">
              Continue with the WorkOS account assigned to this workspace.
            </p>
            <button
              type="button"
              data-testid="sso-login-button"
              onClick={handleWorkOSLogin}
              disabled={loading}
              className="w-full bg-pir-accent text-white font-medium rounded px-4 py-2 hover:bg-pir-accent/90 disabled:opacity-50 disabled:cursor-not-allowed transition-colors flex items-center justify-center gap-2"
            >
              {loading ? (
                "Redirecting..."
              ) : (
                <>
                  <SSOIcon />
                  Sign in with WorkOS
                </>
              )}
            </button>
          </div>
        )}

        {(mode === "email" || mode === "password") && (
          <form
            onSubmit={
              mode === "password"
                ? handlePasswordSubmit
                : handleEmailContinue
            }
          >
            <label className="block text-sm text-pir-text-secondary mb-2">
              Email
            </label>
            <input
              type="email"
              data-testid="email-input"
              value={email}
              onChange={(event) => setEmail(event.target.value)}
              className="w-full bg-pir-base border border-pir rounded px-3 py-2 text-pir-text-primary focus:outline-none focus:border-pir-accent mb-4"
              autoFocus
              disabled={loading || mode === "password"}
              placeholder="email@example.com"
            />

            {mode === "password" ? (
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
                  onChange={(event) => setPassword(event.target.value)}
                  className="w-full bg-pir-base border border-pir rounded px-3 py-2 text-pir-text-primary focus:outline-none focus:border-pir-accent mb-4"
                  disabled={loading}
                />
                <button
                  type="submit"
                  data-testid="login-button"
                  disabled={loading || !password}
                  className="w-full bg-pir-accent text-white font-medium rounded px-4 py-2 hover:bg-pir-accent/90 disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
                >
                  {loading ? "Logging in..." : "Login"}
                </button>
              </div>
            ) : (
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
        )}
      </div>
    </div>
  );
}

function Spinner() {
  return (
    <svg
      width="12"
      height="12"
      viewBox="0 0 12 12"
      fill="none"
      className="animate-spin"
    >
      <circle
        cx="6"
        cy="6"
        r="4"
        stroke="currentColor"
        strokeWidth="1.2"
        opacity="0.3"
      />
      <path
        d="M6 2a4 4 0 012.83 1.17"
        stroke="currentColor"
        strokeWidth="1.2"
        strokeLinecap="round"
      />
    </svg>
  );
}

function SSOIcon() {
  return (
    <svg
      width="14"
      height="14"
      viewBox="0 0 14 14"
      fill="none"
      className="shrink-0"
    >
      <rect
        x="1"
        y="1"
        width="5"
        height="5"
        rx="1"
        stroke="currentColor"
        strokeWidth="1.2"
      />
      <rect
        x="8"
        y="1"
        width="5"
        height="5"
        rx="1"
        stroke="currentColor"
        strokeWidth="1.2"
      />
      <rect
        x="1"
        y="8"
        width="5"
        height="5"
        rx="1"
        stroke="currentColor"
        strokeWidth="1.2"
      />
      <rect
        x="8"
        y="8"
        width="5"
        height="5"
        rx="1"
        stroke="currentColor"
        strokeWidth="1.2"
      />
    </svg>
  );
}
