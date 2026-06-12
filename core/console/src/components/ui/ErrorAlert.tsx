// v1.0.0 - 2026-03-09 - Reusable error alert with copy-to-clipboard button
"use client";

import { useState, useCallback } from "react";

interface ErrorAlertProps {
  message: string;
  /** "banner" = bg + border block; "inline" = text only */
  variant?: "banner" | "inline";
  /** Extra Tailwind classes */
  className?: string;
  /** If provided, show dismiss (X) and call this on click */
  onDismiss?: () => void;
}

/**
 * Clipboard copy icon (12x12)
 */
function CopyIcon({ size = 12 }: { size?: number }) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
      strokeLinejoin="round"
    >
      <rect x="9" y="9" width="13" height="13" rx="2" />
      <path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1" />
    </svg>
  );
}

/**
 * Check icon shown after successful copy (12x12)
 */
function CheckIcon({ size = 12 }: { size?: number }) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
      strokeLinejoin="round"
    >
      <polyline points="20 6 9 17 4 12" />
    </svg>
  );
}

export function ErrorAlert({
  message,
  variant = "banner",
  className = "",
  onDismiss,
}: ErrorAlertProps) {
  const [copied, setCopied] = useState(false);

  const handleCopy = useCallback(async () => {
    try {
      await navigator.clipboard.writeText(message);
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    } catch {
      // Fallback for non-secure contexts
      const textarea = document.createElement("textarea");
      textarea.value = message;
      textarea.style.position = "fixed";
      textarea.style.opacity = "0";
      document.body.appendChild(textarea);
      textarea.select();
      document.execCommand("copy");
      document.body.removeChild(textarea);
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    }
  }, [message]);

  if (variant === "inline") {
    return (
      <span className={`inline-flex items-center gap-1.5 ${className}`}>
        <span>{message}</span>
        <button
          type="button"
          onClick={handleCopy}
          className="shrink-0 opacity-50 hover:opacity-100 transition-opacity"
          title={copied ? "Copied!" : "Copy error"}
        >
          {copied ? <CheckIcon size={11} /> : <CopyIcon size={11} />}
        </button>
      </span>
    );
  }

  // Banner variant (default)
  return (
    <div
      className={`flex items-start gap-2 bg-pir-error/10 border border-pir-error/30 text-pir-error rounded px-3 py-2 text-caption ${className}`}
    >
      <span className="flex-1 min-w-0">{message}</span>
      <button
        type="button"
        onClick={handleCopy}
        className="shrink-0 mt-0.5 opacity-50 hover:opacity-100 transition-opacity"
        title={copied ? "Copied!" : "Copy error"}
      >
        {copied ? <CheckIcon /> : <CopyIcon />}
      </button>
      {onDismiss && (
        <button
          type="button"
          onClick={onDismiss}
          className="shrink-0 mt-0.5 opacity-50 hover:opacity-100 transition-opacity text-xs leading-none"
          title="Dismiss"
        >
          &times;
        </button>
      )}
    </div>
  );
}
