import { defineConfig, devices } from "@playwright/test";

/**
 * Visual and interaction checks against the running local stack.
 *
 * Start the API and Vite yourself (`127.0.0.1:8000` and `:5173`). These tests
 * sign in through development authentication and never talk to eRegisters.
 *
 *   npx playwright install chromium
 *   npm run test:e2e
 */
export default defineConfig({
  testDir: "./tests/e2e",
  fullyParallel: false,
  retries: 0,
  timeout: 60_000,
  expect: { timeout: 15_000 },
  reporter: [["list"]],
  use: {
    baseURL: "http://127.0.0.1:5173",
    trace: "off",
    screenshot: "off",
  },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }],
});
