"use client";

import { useEffect, useState } from "react";

import { BrainShell } from "@/components/brain/BrainShell";

/** S0 spike (task 7352d7dc): the only server-component in the Console read the
 * role cookie via next/headers, which blocks `output: "export"` (static GUI in
 * the wheel). The sniff is a UI courtesy — the backend stays the source of
 * truth — so read the same cookie client-side instead. */
function useCanRecompute(): boolean {
  const [canRecompute, setCanRecompute] = useState(false);
  useEffect(() => {
    const role = document.cookie
      .split("; ")
      .find((c) => c.startsWith("pir_role="))
      ?.split("=")[1];
    setCanRecompute(role === "operator" || role === "admin" || role === "super_admin");
  }, []);
  return canRecompute;
}

export default function BrainLayout({ children }: { children: React.ReactNode }) {
  const canRecompute = useCanRecompute();
  return (
    <BrainShell userId="console-ui" canRecompute={canRecompute}>
      {children}
    </BrainShell>
  );
}
