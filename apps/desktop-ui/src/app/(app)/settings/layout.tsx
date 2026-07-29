import type { ReactNode } from "react";

// The local product ships exactly one settings page: the LLM key
// (apps/desktop-ui/surfaces.yaml, reachable_routes). The shared layout rendered
// SettingsSidebar, whose eight entries point at hosted administration surfaces
// this product does not ship — seven dead links around one working page. Local
// settings are a plain container until a second local settings page exists.
export default function SettingsLayout({ children }: { children: ReactNode }) {
  return <div className="flex-1 overflow-y-auto">{children}</div>;
}
