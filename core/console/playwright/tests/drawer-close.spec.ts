import { expect, test } from "@playwright/test";

test.describe("AutoApproved drawer close UX (P1.5.E1)", () => {
  test("ESC keyboard chiude drawer", async ({ page }) => {
    await page.goto("/inbox/triage/files/");
    await page.locator('button[aria-label*="auto-approvati"]').first().click();
    await expect(page.locator('aside[role="dialog"]')).toBeVisible();
    await page.keyboard.press("Escape");
    await expect(page.locator('aside[role="dialog"]')).not.toBeVisible();
  });

  test("Backdrop click chiude drawer", async ({ page }) => {
    await page.goto("/inbox/triage/files/");
    await page.locator('button[aria-label*="auto-approvati"]').first().click();
    await expect(page.locator('aside[role="dialog"]')).toBeVisible();
    // Click sul backdrop (lato sinistro, fuori dall'aside che ha ml-auto)
    await page.locator('div[role="presentation"]').click({ position: { x: 50, y: 100 } });
    await expect(page.locator('aside[role="dialog"]')).not.toBeVisible();
  });

  test("Click dentro aside NON chiude drawer", async ({ page }) => {
    await page.goto("/inbox/triage/files/");
    await page.locator('button[aria-label*="auto-approvati"]').first().click();
    await expect(page.locator('aside[role="dialog"]')).toBeVisible();
    await page.locator('aside[role="dialog"] header').click();
    await expect(page.locator('aside[role="dialog"]')).toBeVisible();
  });
});
