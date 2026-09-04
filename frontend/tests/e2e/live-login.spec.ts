/**
 * Live login page evidence. Does not submit credentials.
 */
import { expect, test } from "@playwright/test";

test.describe("live login page", () => {
  test("the entry point is a username and password form", async ({ page }) => {
    await page.goto("/sign-in");
    const chooser = page.getByRole("button", { name: /National Programme Officer/ });
    if (await chooser.count()) {
      test.skip(true, "Demo authentication is serving this port.");
    }
    await expect(page.getByRole("heading", { name: "MARS" })).toBeVisible();
    await expect(page.getByLabel("Username")).toBeVisible();
    await expect(page.getByLabel("Password")).toHaveAttribute("type", "password");
    await expect(page.getByRole("button", { name: "Sign in" })).toBeVisible();
    await expect(
      page.getByText("Use your authorised Ministry eRegisters account."),
    ).toBeVisible();
    await expect(page.getByText("Choose an account")).toHaveCount(0);
  });
});
