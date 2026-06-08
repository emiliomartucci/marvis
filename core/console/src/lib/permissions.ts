import type { SystemRole } from "./types";

export interface Permissions {
  canWrite: boolean; // operator+
  canAdmin: boolean; // admin+
}

const ROLE_HIERARCHY: Record<SystemRole, number> = {
  viewer: 0,
  operator: 1,
  admin: 2,
  super_admin: 3,
};

export function derivePermissions(role: SystemRole | null): Permissions {
  const level = role !== null ? ROLE_HIERARCHY[role] : -1;
  return {
    canWrite: level >= ROLE_HIERARCHY.operator,
    canAdmin: level >= ROLE_HIERARCHY.admin,
  };
}
