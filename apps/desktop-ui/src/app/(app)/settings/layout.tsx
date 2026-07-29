import SettingsSidebar from "@/components/settings/SettingsSidebar";
import { WorkspaceProvider } from "@/lib/workspace";
import type { ReactNode } from "react";

export default function SettingsLayout({ children }: { children: ReactNode }) {
  return (
    <WorkspaceProvider>
      <div className="flex flex-1 min-h-0 h-full">
        <aside className="hidden md:flex w-[240px] shrink-0 flex-col bg-pir-surface-0 border-r border-pir overflow-y-auto">
          <SettingsSidebar />
        </aside>
        <div className="flex-1 overflow-y-auto">
          {children}
        </div>
      </div>
    </WorkspaceProvider>
  );
}
