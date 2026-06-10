// v1.0.0 - 2026-03-14 - Test user credentials for Playwright E2E tests
export const TEST_USERS = {
  viewer:      { email: 'pw-viewer@rbac.example.com',     password: 'TestPass123!' },
  operator:    { email: 'pw-operator@rbac.example.com',   password: 'TestPass123!' },
  admin:       { email: 'pw-admin@rbac.example.com',      password: 'TestPass123!' },
  super_admin: { email: 'pw-superadmin@rbac.example.com', password: 'TestPass123!' },
} as const;

export type Role = keyof typeof TEST_USERS;
