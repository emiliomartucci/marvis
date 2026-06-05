// Welcome route group layout — no sidebar, no header, focused wizard surface.

import type { ReactNode } from "react";

export default function WelcomeLayout({ children }: { children: ReactNode }) {
  return (
    <div className="flex h-screen flex-col bg-pir-base text-pir-text-primary">
      {children}
    </div>
  );
}
