import { expect, Page, test } from "@playwright/test";
import { TEST_USERS, type Role } from "../fixtures/auth";

const BASE_URL = process.env.BASE_URL ?? "http://localhost:8100";
const API_BASE = process.env.API_BASE ?? "http://localhost:8100";
const ROLES: Role[] = ["viewer", "admin"];
const INSPECTION_ROUTES = [
  "/ui/finder/",
  "/ui/inbox/triage/files/",
  "/ui/projects/detail/?slug=marvisx",
];
const PROJECT_DETAIL_ROUTE = "/ui/projects/detail/?slug=marvisx";
const STATUS_UPDATE_COMPOSER_PLACEHOLDER =
  "Aggiorna su questo progetto… supporta markdown, ⌘↵ per salvare";
const FILE_MUTATION_BUTTON_NAME =
  /^(upload|new folder|rename|move|delete|save|edit|approve|reject|reparse|push|pull)$/i;
const HISTORICAL_MUTATION_ROUTES = [
  { method: "PUT", path: "/api/v1/projects/marvisx/files/docs/probe.md" },
  { method: "PUT", path: "/api/v1/finder/file" },
  { method: "POST", path: "/api/v1/finder/delete" },
  { method: "POST", path: "/terminal/upload" },
  { method: "POST", path: "/api/v1/ingest/pending/missing/approve" },
  { method: "POST", path: "/api/v1/projects/marvisx/git/push" },
  { method: "POST", path: "/api/v1/projects/marvisx/git/pull" },
] as const;

function uiUrl(path: string): string {
  return new URL(path, BASE_URL).toString();
}

function apiUrl(path: string): string {
  return new URL(path, API_BASE).toString();
}

async function loginAs(page: Page, role: Role) {
  const { email, password } = TEST_USERS[role];

  await page.goto(uiUrl("/ui/login/"));
  await page.getByTestId("email-input").fill(email);
  await page.getByTestId("continue-button").click();
  await page.getByTestId("password-input").fill(password);
  await page.getByTestId("login-button").click();
  await page.waitForURL(/\/ui\/terminal\//, { timeout: 30_000 });
}

for (const role of ROLES) {
  test(`${role}: project-file browser surfaces are inspection-only`, async ({ page }) => {
    await loginAs(page, role);

    const unexpectedMutations: string[] = [];
    page.on("request", (request) => {
      if (request.method() === "GET" || request.method() === "HEAD") return;
      if (HISTORICAL_MUTATION_ROUTES.some((route) => request.url().includes(route.path))) {
        unexpectedMutations.push(`${request.method()} ${request.url()}`);
      }
    });

    for (const route of INSPECTION_ROUTES) {
      await page.goto(uiUrl(route));
      await expect(page.getByTestId("app-shell")).toBeVisible({ timeout: 15_000 });
      await expect(page.locator('input[type="file"]')).toHaveCount(0);
      if (route !== PROJECT_DETAIL_ROUTE) {
        await expect(page.locator('textarea, [contenteditable="true"]')).toHaveCount(0);
      }
      await expect(
        page.getByRole("button", { name: FILE_MUTATION_BUTTON_NAME }),
      ).toHaveCount(0);
    }

    expect(unexpectedMutations).toEqual([]);
  });

  if (role === "admin") {
    test("admin: project status update composer is outside project-file mutation scope", async ({ page }) => {
      await loginAs(page, role);
      await page.goto(uiUrl(PROJECT_DETAIL_ROUTE));
      await expect(page.getByTestId("app-shell")).toBeVisible({ timeout: 15_000 });

      await expect(page.getByPlaceholder(STATUS_UPDATE_COMPOSER_PLACEHOLDER)).toBeVisible();
      await expect(page.locator('input[type="file"]')).toHaveCount(0);
      await expect(
        page.getByRole("button", { name: FILE_MUTATION_BUTTON_NAME }),
      ).toHaveCount(0);
    });
  }

  test(`${role}: historical project-file mutation endpoints are denied by the API`, async ({ page }) => {
    await loginAs(page, role);
    const expectedOrigin = new URL(API_BASE).origin;

    for (const route of HISTORICAL_MUTATION_ROUTES) {
      const response = await page.request.fetch(apiUrl(route.path), {
        method: route.method,
        headers: { "content-type": "application/json" },
        data: '{"sentinel":"browser-bytes-must-not-persist"}',
      });

      expect(new URL(response.url()).origin, `${route.method} ${route.path} must target API_BASE`).toBe(expectedOrigin);
      expect(response.status(), `${route.method} ${route.path}`).toBe(404);
    }
  });
}
