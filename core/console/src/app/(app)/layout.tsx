"use client";

import { usePathname } from "next/navigation";
import AppShell from "@/components/AppShell";
import TerminalPanel from "@/components/TerminalPanel";

export default function AppLayout({ children }: { children: React.ReactNode }) {
  const pathname = usePathname();
  const panelVisible = pathname.startsWith("/terminal");

  return (
    <AppShell>
      {/* Keep TerminalPanel mounted so open/active session state survives route switches,
          but pass panelVisible=false so it can suspend expensive runtime work off-route. */}
      <div
        style={{ display: panelVisible ? "flex" : "none" }}
        className="flex-1 min-h-0"
      >
        <TerminalPanel panelVisible={panelVisible} />
      </div>
      <div
        style={{ display: panelVisible ? "none" : "flex" }}
        className="flex-1 min-h-0 flex-col"
      >
        {children}
      </div>
    </AppShell>
  );
}
