import { defineConfig, devices } from "@playwright/test";

/**
 * Playwright configuration for the orderbook watcher frontend tests.
 *
 * Unlike the main E2E config, this suite has no Docker or global-setup
 * dependency: each spec serves the real static frontend
 * (orderbook_watcher/static) from an in-process HTTP server together with a
 * deterministic orderbook.json fixture. It is therefore cheap enough to run
 * on every change:
 *
 *   npx playwright test -c playwright.obwatcher.config.ts
 */

const isCI = !!process.env.CI;

export default defineConfig({
  testDir: "./obwatcher",
  outputDir: "./test-results-obwatcher",
  timeout: 30_000,
  expect: { timeout: 5_000 },
  fullyParallel: true,
  forbidOnly: isCI,
  retries: 0,
  reporter: isCI ? [["github"], ["list"]] : [["list"]],

  use: {
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
  },

  projects: [
    {
      name: "chromium",
      use: { ...devices["Desktop Chrome"] },
    },
  ],
});
