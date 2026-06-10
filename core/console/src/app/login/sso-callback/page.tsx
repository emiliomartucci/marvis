// v1.0.1 - 2026-03-13 - SSO callback handler with Suspense boundary for Next.js 15
"use client";

import { Suspense, useEffect, useRef, useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import { completeSSOCallback } from "@/lib/api";
import { ErrorAlert } from "@/components/ui/ErrorAlert";

type CallbackState = "processing" | "success" | "error";

const ERROR_MESSAGES: Record<string, string> = {
  invalid_state: "SSO session expired. Please try again.",
  invalid_code: "Invalid authorization code. Please try again.",
  provider_error: "SSO provider returned an error.",
  user_not_found: "No account found for this SSO identity.",
  domain_mismatch: "Your email domain is not configured for SSO.",
};

export default function SSOCallbackPage() {
  return (
    <Suspense fallback={<CallbackLoading />}>
      <SSOCallbackContent />
    </Suspense>
  );
}

function CallbackLoading() {
  return (
    <div className="flex items-center justify-center h-screen">
      <div className="bg-pir-surface-0 border border-pir rounded p-8 w-full max-w-sm text-center space-y-4">
        <svg width="24" height="24" viewBox="0 0 24 24" fill="none" className="mx-auto animate-spin text-pir-accent">
          <circle cx="12" cy="12" r="9" stroke="currentColor" strokeWidth="2" opacity="0.2" />
          <path d="M12 3a9 9 0 016.36 2.64" stroke="currentColor" strokeWidth="2" strokeLinecap="round" />
        </svg>
        <p className="text-sm text-pir-text-secondary">Completing sign in...</p>
      </div>
    </div>
  );
}

function SSOCallbackContent() {
  const router = useRouter();
  const searchParams = useSearchParams();
  const [state, setState] = useState<CallbackState>("processing");
  const [error, setError] = useState("");
  const processedRef = useRef(false);

  useEffect(() => {
    if (processedRef.current) return;
    processedRef.current = true;

    const code = searchParams.get("code");
    const stateParam = searchParams.get("state");
    const errorParam = searchParams.get("error");

    if (errorParam) {
      const description = searchParams.get("error_description") || "SSO authentication failed";
      setError(ERROR_MESSAGES[errorParam] || description);
      setState("error");
      return;
    }

    if (!code || !stateParam) {
      setError("Missing authorization parameters. Please try logging in again.");
      setState("error");
      return;
    }

    async function exchangeCode() {
      try {
        await completeSSOCallback(code!, stateParam!);
        setState("success");
        sessionStorage.removeItem("sso_login_email");
        router.push("/terminal/");
      } catch (err) {
        const msg = err instanceof Error ? err.message : "SSO authentication failed";
        setError(ERROR_MESSAGES[msg] || msg);
        setState("error");
      }
    }

    exchangeCode();
  }, [searchParams, router]);

  return (
    <div className="flex items-center justify-center h-screen">
      <div className="bg-pir-surface-0 border border-pir rounded p-8 w-full max-w-sm text-center">
        {state === "processing" && (
          <div className="space-y-4">
            <svg width="24" height="24" viewBox="0 0 24 24" fill="none" className="mx-auto animate-spin text-pir-accent">
              <circle cx="12" cy="12" r="9" stroke="currentColor" strokeWidth="2" opacity="0.2" />
              <path d="M12 3a9 9 0 016.36 2.64" stroke="currentColor" strokeWidth="2" strokeLinecap="round" />
            </svg>
            <p className="text-sm text-pir-text-secondary">Completing sign in...</p>
          </div>
        )}

        {state === "success" && (
          <div className="space-y-4">
            <svg width="24" height="24" viewBox="0 0 24 24" fill="none" className="mx-auto text-emerald-500">
              <path d="M5 12l5 5L19 7" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round" />
            </svg>
            <p className="text-sm text-pir-text-secondary">Signed in. Redirecting...</p>
          </div>
        )}

        {state === "error" && (
          <div className="space-y-4">
            <ErrorAlert message={error} />
            <button
              onClick={() => router.push("/login/")}
              className="text-sm text-pir-accent hover:underline"
            >
              Back to login
            </button>
          </div>
        )}
      </div>
    </div>
  );
}
