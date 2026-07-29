"use client";

import AppShell from "@/components/AppShell";

// The terminal Console belongs to marvisx (plan R1/R4). This layout used to
// mount TerminalPanel on every page so its session state survived route
// switches — which meant terminal code was bundled into the local artifact
// even after the terminal route was pruned: a perimeter that held for pages
// but not for the emitted JavaScript. The local product does not ship it.
export default function AppLayout({ children }: { children: React.ReactNode }) {
  return (
    <AppShell>
      <div className="flex-1 min-h-0 flex flex-col">{children}</div>
    </AppShell>
  );
}
