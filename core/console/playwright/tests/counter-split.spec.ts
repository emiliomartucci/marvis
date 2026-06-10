// v1.0.0 - 2026-04-29 - Phase 1.5 P1.5.E2: counter "auto · manual oggi" split
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

test('counter mostra split "N auto · M manual oggi"', async ({ page }) => {
  await loginAs(page, 'operator');
  await page.goto('/inbox/triage/files/');

  // Counter appears (any value, even 0)
  const counter = page.locator('button[aria-label*="auto-approvati"]');
  await expect(counter).toBeVisible({ timeout: 10_000 });

  // Verify split rendering: "<n> auto · <m> manual oggi"
  const text = await counter.textContent();
  expect(text).toMatch(/\d+\s*auto\s*·\s*\d+\s*manual\s*oggi/);

  // Tooltip via title attribute for hover discovery
  const title = await counter.getAttribute('title');
  expect(title).toContain('auto');
  expect(title).toContain('manual');
});
