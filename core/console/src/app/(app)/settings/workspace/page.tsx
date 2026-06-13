// v1.0.0 - 2026-03-13 - Workspace settings with SSO config and RBAC matrix
"use client";

import { PermissionGate } from "@/components/PermissionGate";
import SSOConfigPanel from "@/components/settings/SSOConfigPanel";
import RBACMatrix from "@/components/settings/RBACMatrix";

export default function WorkspacePage() {
  return (
    <div className="p-6 max-w-4xl space-y-8">
      <h1 className="text-heading text-pir-text-primary">Workspace</h1>

      {/* RBAC Matrix — admin+ */}
      <PermissionGate minRole="admin">
        <section className="space-y-3">
          <h2 className="text-sm font-medium text-pir-text-primary">Permissions Matrix</h2>
          <div className="bg-pir-surface-0 border border-pir rounded-lg overflow-hidden">
            <RBACMatrix />
          </div>
        </section>
      </PermissionGate>

      {/* SSO Config — super_admin only */}
      <PermissionGate minRole="super_admin">
        <section className="bg-pir-surface-0 border border-pir rounded-lg p-4">
          <SSOConfigPanel />
        </section>
      </PermissionGate>

      {/* Fallback for low-permission users */}
      <PermissionGate minRole="admin" fallback={
        <div className="text-sm text-pir-text-muted">
          Contact an admin to manage workspace settings.
        </div>
      }>
        <></>
      </PermissionGate>
    </div>
  );
}
