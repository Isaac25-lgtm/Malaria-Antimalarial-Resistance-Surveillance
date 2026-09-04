/**
 * Browser interaction and visual evidence for the operational overview.
 *
 * Figures on screen come from the running API. They are not copied from the
 * mockup. Development authentication remains labelled.
 */
import { expect, test, type Page } from "@playwright/test";
import fs from "node:fs";
import path from "node:path";

const SCREEN = path.join(process.cwd(), "tests", "visual", "screenshots");
fs.mkdirSync(SCREEN, { recursive: true });

async function signIn(page: Page, displayName: string | RegExp) {
  await page.goto("/sign-in");
  await expect(page.getByRole("button", { name: displayName })).toBeVisible();
  await page.getByRole("button", { name: displayName }).click();
  await expect(page.getByText("Development session", { exact: true })).toBeVisible();
}

async function waitForDistrictGeometry(page: Page) {
  await expect
    .poll(async () => {
      const svgPaths = await page.locator("[data-testid='boundary-svg'] path").count();
      if (svgPaths > 10) return svgPaths;
      const map = page.locator("[data-testid='boundary-map']");
      const ready = (await page.locator("[data-map-ready='true']").count()) > 0;
      const count = Number((await map.getAttribute("data-feature-count")) ?? 0);
      return ready ? count : 0;
    })
    .toBeGreaterThan(10);
}

test.describe("overview visual evidence", () => {
  test.beforeEach(async ({ page }) => {
    await page.goto("/sign-in");
    if (await page.getByLabel("Password").count()) {
      test.skip(true, "Live login is serving this port; demo overview tests need 5174.");
    }
  });
  test("national development scope at 1536×1024", async ({ page }) => {
    await page.setViewportSize({ width: 1536, height: 1024 });
    await signIn(page, /National Programme Officer/);
    await expect(page.getByRole("heading", { name: /Overview/ })).toBeVisible();
    await expect(page.getByText("Development session", { exact: true })).toBeVisible();
    await waitForDistrictGeometry(page);
    await page.screenshot({
      path: path.join(SCREEN, "overview-1536x1024-national.png"),
      fullPage: true,
    });
  });

  test("Pader development scope is not labelled national", async ({ page }) => {
    await page.setViewportSize({ width: 1536, height: 1024 });
    await signIn(page, /Pader District Health Officer/);
    await expect(page.getByRole("heading", { name: /Pader Overview/ })).toBeVisible();
    await expect(page.getByRole("heading", { name: "National Overview" })).toHaveCount(0);
    await waitForDistrictGeometry(page);
    await page.screenshot({
      path: path.join(SCREEN, "overview-1536x1024-pader.png"),
      fullPage: true,
    });
  });

  test("compact laptop viewport", async ({ page }) => {
    await page.setViewportSize({ width: 1280, height: 800 });
    await signIn(page, /National Programme Officer/);
    await expect(page.getByRole("heading", { name: /Overview/ })).toBeVisible();
    await page.screenshot({
      path: path.join(SCREEN, "overview-1280x800-laptop.png"),
      fullPage: true,
    });
  });

  test("map geometry is present", async ({ page }) => {
    await page.setViewportSize({ width: 1536, height: 1024 });
    await signIn(page, /National Programme Officer/);
    await waitForDistrictGeometry(page);
    const map = page.locator("[data-testid='boundary-svg'], [data-testid='boundary-map']").first();
    await expect(map).toBeVisible();
    await page.screenshot({
      path: path.join(SCREEN, "overview-map.png"),
    });
  });

  test("loading state keeps the KPI strip", async ({ page }) => {
    await page.setViewportSize({ width: 1536, height: 1024 });
    await page.route("**/api/v1/surveillance/overview**", async (route) => {
      await new Promise((resolve) => setTimeout(resolve, 2500));
      await route.continue();
    });
    await page.goto("/sign-in");
    await page.getByRole("button", { name: /National Programme Officer/ }).click();
    await expect(page.locator(".overview__kpi-skeleton, .measure-grid")).toBeVisible();
    await page.screenshot({
      path: path.join(SCREEN, "overview-loading.png"),
    });
  });

  test("unavailable overview is labelled, not zeroed", async ({ page }) => {
    await page.setViewportSize({ width: 1536, height: 1024 });
    await page.route("**/api/v1/surveillance/overview**", (route) =>
      route.fulfill({
        status: 503,
        contentType: "application/problem+json",
        body: JSON.stringify({
          title: "Unavailable",
          detail: "The overview could not be assembled.",
          status: 503,
        }),
      }),
    );
    await signIn(page, /National Programme Officer/);
    await expect(page.getByRole("alert")).toContainText(/could not be loaded|Unavailable/i);
    await expect(page.getByText("1,342,897")).toHaveCount(0);
    await page.screenshot({
      path: path.join(SCREEN, "overview-unavailable.png"),
    });
  });
});
