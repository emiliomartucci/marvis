// v1.0.0 - 2026-04-29 - Phase 1.5 P1.5.E3: project selector dropdown in upload UI
import { test, expect, Page } from '@playwright/test';
import { TEST_USERS, type Role } from '../fixtures/auth';

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

test.describe('Ingest triage project selector', () => {
  test.beforeEach(async ({ page }) => {
    await loginAs(page, 'operator');
    // Clear localStorage to start each test from the canonical default.
    await page.goto('/inbox/triage/files/');
    await page.evaluate(() => window.localStorage.removeItem('ingestUploadProject'));
  });

  test('default project is marvisx', async ({ page }) => {
    await page.goto('/inbox/triage/files/');
    const button = page.locator('[data-testid="ingest-project-selector"]');
    await expect(button).toBeVisible({ timeout: 10_000 });
    await expect(button).toContainText('marvisx');
  });

  test('opens modal and persists selection across reload', async ({ page }) => {
    await page.goto('/inbox/triage/files/');

    // Pre-seed via localStorage (more deterministic than picking a random
    // project from the live list — exercises both lazy-init and persistence).
    await page.evaluate(() =>
      window.localStorage.setItem('ingestUploadProject', 'cer')
    );
    await page.reload();

    const button = page.locator('[data-testid="ingest-project-selector"]');
    await expect(button).toContainText('cer');
  });

  test('selector button opens ProjectSelectorModal', async ({ page }) => {
    await page.goto('/inbox/triage/files/');
    await page.locator('[data-testid="ingest-project-selector"]').click();
    await expect(page.locator('[role="dialog"]')).toBeVisible({ timeout: 5000 });
    // Search input is focused.
    await expect(
      page.locator('input[placeholder="Search projects..."]')
    ).toBeVisible();
  });

  test('Escape closes the selector without changing slug', async ({ page }) => {
    await page.goto('/inbox/triage/files/');
    const before = await page
      .locator('[data-testid="ingest-project-selector"]')
      .textContent();

    await page.locator('[data-testid="ingest-project-selector"]').click();
    await expect(page.locator('[role="dialog"]')).toBeVisible();
    await page.keyboard.press('Escape');
    await expect(page.locator('[role="dialog"]')).toHaveCount(0);

    const after = await page
      .locator('[data-testid="ingest-project-selector"]')
      .textContent();
    expect(after).toEqual(before);
  });
});
