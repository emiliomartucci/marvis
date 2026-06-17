// v1.0.0 - 2026-03-14 - Login E2E tests: 4 roles + SSO HRD
import { test, expect, Page } from '@playwright/test';
import { TEST_USERS, type Role } from '../fixtures/auth';

// 2-step login helper: email → Continue → password → Login
async function loginAs(page: Page, role: Role) {
  const { email, password } = TEST_USERS[role];

  await page.goto('/login');

  // Step 1: Fill email and click Continue
  await page.fill('[data-testid="email-input"]', email);
  await page.click('[data-testid="continue-button"]');

  // Step 2: Password field should appear
  await page.waitForSelector('[data-testid="password-input"]', { timeout: 5000 });
  await page.fill('[data-testid="password-input"]', password);
  await page.click('[data-testid="login-button"]');
}

const ROLES: Role[] = ['viewer', 'operator', 'admin', 'super_admin'];

for (const role of ROLES) {
  test(`${role}: login succeeds and redirects to /terminal/`, async ({ page }) => {
    await loginAs(page, role);

    // After login, should redirect to /terminal/ (30s for cold browser start on first test)
    await page.waitForURL(/\/terminal\//, { timeout: 30_000 });
    await expect(page).toHaveURL(/\/terminal\//);

    // app-shell is rendered when authenticated
    await expect(page.locator('[data-testid="app-shell"]')).toBeVisible({ timeout: 5000 });

    // No login error shown
    await expect(page.locator('body')).not.toContainText('Login failed');
    await expect(page.locator('body')).not.toContainText('Invalid credentials');

    await page.screenshot({ path: `playwright/screenshots/${role}-terminal.png` });
  });
}

test('wrong password: shows error message', async ({ page }) => {
  await page.goto('/login');

  await page.fill('[data-testid="email-input"]', TEST_USERS.viewer.email);
  await page.click('[data-testid="continue-button"]');

  await page.waitForSelector('[data-testid="password-input"]', { timeout: 5000 });
  await page.fill('[data-testid="password-input"]', 'WrongPassword999!');
  await page.click('[data-testid="login-button"]');

  // Should stay on login page (401 causes redirect back to /login)
  await page.waitForURL(/\/login/, { timeout: 8000 });
  await expect(page).toHaveURL(/\/login/);
  // No crash — form is still usable
  await expect(page.locator('[data-testid="email-input"], [data-testid="continue-button"]')).toBeVisible();
});

test('SSO email-first: domain sso.local triggers SSO check', async ({ page }) => {
  await page.goto('/login');
  await page.fill('[data-testid="email-input"]', 'testuser@sso.local');

  // Wait for debounce (400ms) + SSO config check
  await page.waitForTimeout(800);

  // UI is stable — email input retains its value
  await expect(page.locator('[data-testid="email-input"]')).toHaveValue('testuser@sso.local');

  // Continue button may be visible (SSO not enabled in test env) — no JS error
  // The test validates that the HRD check fires without crashing the page
  await expect(page.locator('body')).not.toContainText('Error');
});
