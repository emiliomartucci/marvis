// v1.0.0 - 2026-03-14 - RBAC UI tests: role-gated buttons, settings access, triage navigation
import { test, expect, Page } from '@playwright/test';
import { TEST_USERS, type Role } from '../fixtures/auth';

// 2-step login helper (same flow as login.spec.ts)
async function loginAs(page: Page, role: Role) {
  const { email, password } = TEST_USERS[role];

  await page.goto('/login');
  await page.fill('[data-testid="email-input"]', email);
  await page.click('[data-testid="continue-button"]');
  await page.waitForSelector('[data-testid="password-input"]', { timeout: 5000 });
  await page.fill('[data-testid="password-input"]', password);
  await page.click('[data-testid="login-button"]');
  await page.waitForURL(/\/terminal\//, { timeout: 30_000 });
}

// ─── New Session button — PermissionGate(minRole="operator") ──────────────────

test('viewer: new-session-button is hidden (PermissionGate)', async ({ page }) => {
  await loginAs(page, 'viewer');
  // Give sidebar time to render
  await expect(page.locator('[data-testid="app-shell"]')).toBeVisible({ timeout: 5000 });
  // PermissionGate renders nothing for viewer — button must not exist in DOM
  await expect(page.locator('[data-testid="new-session-button"]')).toHaveCount(0);
  await page.screenshot({ path: 'playwright/screenshots/viewer-no-new-session-btn.png' });
});

test('operator: new-session-button is visible', async ({ page }) => {
  await loginAs(page, 'operator');
  await expect(page.locator('[data-testid="app-shell"]')).toBeVisible({ timeout: 5000 });
  await expect(page.locator('[data-testid="new-session-button"]')).toBeVisible({ timeout: 5000 });
  await page.screenshot({ path: 'playwright/screenshots/operator-new-session-btn.png' });
});

test('operator: clicking new-session-button opens modal', async ({ page }) => {
  await loginAs(page, 'operator');
  await page.locator('[data-testid="new-session-button"]').click();
  // Modal renders with heading "New Session"
  await expect(page.getByText('New Session')).toBeVisible({ timeout: 5000 });
  await page.screenshot({ path: 'playwright/screenshots/operator-new-session-modal.png' });
});

test('admin: new-session-button is visible', async ({ page }) => {
  await loginAs(page, 'admin');
  await expect(page.locator('[data-testid="app-shell"]')).toBeVisible({ timeout: 5000 });
  await expect(page.locator('[data-testid="new-session-button"]')).toBeVisible({ timeout: 5000 });
});

// ─── Triage page — accessible to all authenticated roles ─────────────────────

const ALL_ROLES: Role[] = ['viewer', 'operator', 'admin', 'super_admin'];

for (const role of ALL_ROLES) {
  test(`${role}: can navigate to /triage/ and see app-shell`, async ({ page }) => {
    await loginAs(page, role);
    await page.goto('/triage/');
    await expect(page.locator('[data-testid="app-shell"]')).toBeVisible({ timeout: 15_000 });
    // Page should not show a hard error
    await expect(page.locator('body')).not.toContainText('Internal Server Error');
    await page.screenshot({ path: `playwright/screenshots/${role}-triage.png` });
  });
}

// ─── Settings / Users — PermissionGate(minRole="admin") ──────────────────────

test('viewer: /settings/users/ shows access-denied message', async ({ page }) => {
  await loginAs(page, 'viewer');
  await page.goto('/settings/users/');
  await expect(page.locator('[data-testid="app-shell"]')).toBeVisible({ timeout: 15_000 });
  await expect(page.getByText('You need admin access to view this page.')).toBeVisible({ timeout: 5000 });
  await page.screenshot({ path: 'playwright/screenshots/viewer-settings-users-denied.png' });
});

test('operator: /settings/users/ shows access-denied message', async ({ page }) => {
  await loginAs(page, 'operator');
  await page.goto('/settings/users/');
  await expect(page.locator('[data-testid="app-shell"]')).toBeVisible({ timeout: 15_000 });
  await expect(page.getByText('You need admin access to view this page.')).toBeVisible({ timeout: 5000 });
});

test('admin: /settings/users/ shows user table', async ({ page }) => {
  await loginAs(page, 'admin');
  await page.goto('/settings/users/');
  await expect(page.locator('[data-testid="app-shell"]')).toBeVisible({ timeout: 15_000 });
  // User table is present — heading "Utenti"
  await expect(page.getByRole('heading', { name: 'Utenti' })).toBeVisible({ timeout: 8000 });
  // No access-denied text
  await expect(page.getByText('You need admin access to view this page.')).toHaveCount(0);
  await page.screenshot({ path: 'playwright/screenshots/admin-settings-users.png' });
});

test('super_admin: /settings/users/ shows user table', async ({ page }) => {
  await loginAs(page, 'super_admin');
  await page.goto('/settings/users/');
  await expect(page.locator('[data-testid="app-shell"]')).toBeVisible({ timeout: 15_000 });
  await expect(page.getByRole('heading', { name: 'Utenti' })).toBeVisible({ timeout: 8000 });
  await expect(page.getByText('You need admin access to view this page.')).toHaveCount(0);
});
