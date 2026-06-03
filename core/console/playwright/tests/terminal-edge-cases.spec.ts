import { test, expect, Page } from '@playwright/test';
import { TEST_USERS } from '../fixtures/auth';

const LOCAL_API_BASE = 'http://localhost:8111';

async function loginAsAdmin(page: Page) {
  const response = await page.request.post(`${LOCAL_API_BASE}/api/v1/auth/login`, {
    data: {
      email: TEST_USERS.admin.email,
      password: TEST_USERS.admin.password,
    },
  });
  expect(response.ok()).toBeTruthy();
  const setCookie = response.headers()['set-cookie'] ?? '';
  const sessionMatch = setCookie.match(/pir_session=([^;]+)/);
  expect(sessionMatch).toBeTruthy();

  await page.context().addCookies([
    {
      name: 'pir_session',
      value: sessionMatch![1],
      url: LOCAL_API_BASE,
      httpOnly: true,
      sameSite: 'Lax',
      secure: false,
    },
  ]);

  await page.goto('/terminal');
  await expect(page.locator('[data-testid="app-shell"]')).toBeVisible({ timeout: 10000 });
}

async function createTerminalSession(page: Page, name: string) {
  return page.evaluate(async ({ sessionName }) => {
    const res = await fetch('http://localhost:8111/api/v1/sessions', {
      method: 'POST',
      credentials: 'include',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        name: sessionName,
        provider: 'opencode',
      }),
    });
    if (!res.ok) {
      const body = await res.text();
      throw new Error(`createSession failed ${res.status}: ${body}`);
    }
    return res.json();
  }, { sessionName: name });
}

async function startDiagnostics(page: Page) {
  await page.evaluate(() => {
    window.__pirTerminalDiagnostics?.start('playwright');
  });
}

async function getDiagnostics(page: Page) {
  return page.evaluate(() => window.__pirTerminalDiagnostics?.dump());
}

test.describe('terminal edge cases', () => {
  test('recovers terminal after returning from background tab without forced remount', async ({ browser, page }) => {
    await loginAsAdmin(page);
    const sessionName = `pw-tab-${Date.now()}`;
    const session = await createTerminalSession(page, sessionName);
    await page.goto(`/terminal/${session.session_uuid}`);
    await expect(page.locator('[data-testid="app-shell"]')).toBeVisible({ timeout: 10000 });
    await startDiagnostics(page);

    const otherPage = await browser.newPage();
    await otherPage.goto('about:blank');
    await otherPage.bringToFront();
    await otherPage.waitForTimeout(3000);
    await page.bringToFront();
    await page.evaluate(() => {
      window.dispatchEvent(new Event('focus'));
    });
    await page.waitForTimeout(1500);

    const diagnostics = await getDiagnostics(page);
    const events = diagnostics?.events ?? [];
    const remountEvents = events.filter((event: any) => event.type === 'terminal_force_remount_requested');
    const recoveryEvents = events.filter((event: any) =>
      event.type === 'terminal_sync_applied' && event.payload?.reason === 'active-visible'
    );
    const unmountEvents = events.filter((event: any) => event.type === 'terminal_unmount');

    expect(remountEvents.length).toBe(0);
    expect(recoveryEvents.length).toBeGreaterThan(0);
    expect(recoveryEvents.length).toBeLessThanOrEqual(2);
    expect(unmountEvents.length).toBe(0);

    await page.screenshot({ path: 'playwright/screenshots/terminal-tab-return.png', fullPage: true });
  });

  test('remounts terminal after direct page reload', async ({ page }) => {
    await loginAsAdmin(page);
    const sessionName = `pw-refresh-${Date.now()}`;
    const session = await createTerminalSession(page, sessionName);
    await page.goto(`/terminal/${session.session_uuid}`);
    await expect(page.locator('[data-testid="app-shell"]')).toBeVisible({ timeout: 10000 });
    await startDiagnostics(page);

    await page.reload({ waitUntil: 'domcontentloaded' });
    await expect(page.locator('[data-testid="app-shell"]')).toBeVisible({ timeout: 10000 });
    await page.waitForTimeout(1500);

    const diagnostics = await getDiagnostics(page);
    const events = diagnostics?.events ?? [];
    const routeResolved = events.filter((event: any) => event.type === 'route_uuid_resolved');
    const mounts = events.filter((event: any) => event.type === 'terminal_mount');
    expect(routeResolved.length).toBeGreaterThan(0);
    expect(mounts.length).toBeGreaterThan(0);

    await page.screenshot({ path: 'playwright/screenshots/terminal-refresh-return.png', fullPage: true });
  });
});
