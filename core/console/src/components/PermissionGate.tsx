"use client";

import { useAuth } from "@/lib/auth";
import type { SystemRole } from "@/lib/types";

const ROLE_HIERARCHY: Record<SystemRole, number> = {
  viewer: 0,
  operator: 1,
  admin: 2,
  super_admin: 3,
};

interface PermissionGateProps {
  minRole: SystemRole;
  children: React.ReactNode;
  fallback?: React.ReactNode;
}

export function PermissionGate({
  minRole,
  children,
  fallback = null,
}: PermissionGateProps) {
  const { role, status } = useAuth();
  // Durante loading -> mostra fallback (secure by default, evita layout shift)
  if (status === "loading") return <>{fallback}</>;
  const userLevel = role !== null ? (ROLE_HIERARCHY[role] ?? -1) : -1;
  if (userLevel < ROLE_HIERARCHY[minRole]) return <>{fallback}</>;
  return <>{children}</>;
}
