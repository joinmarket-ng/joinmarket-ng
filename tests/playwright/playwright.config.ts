import { defineConfig, devices } from "@playwright/test";
import path from "path";

/**
 * Playwright configuration for JoinMarket NG E2E tests.
 *
 * Prerequisites:
 *   docker compose --profile e2e up -d
 *
 * The jam-playwright container serves both the JAM frontend and the
 * jmwalletd API on port 29183 (mapped from internal 28183).
 *
 * Environment variables:
 *   JAM_URL          - Frontend URL  (default: https://localhost:29183)
 *   JMWALLETD_URL    - Backend URL   (default: https://localhost:29183)
 *   BITCOIN_RPC_URL  - Bitcoin RPC   (default: http://localhost:18443)
 *   BITCOIN_RPC_USER - RPC user      (default: test)
 *   BITCOIN_RPC_PASS - RPC password  (default: test)
 *   CI               - Set in CI for headless-only + screenshot-on-failure
 */

const isCI = !!process.env.CI;
const slowMo = process.env.PLAYWRIGHT_SLOWMO ? parseInt(process.env.PLAYWRIGHT_SLOWMO, 10) : undefined;

export default defineConfig({
  testDir: "./specs",
  globalSetup: path.resolve(__dirname, "global-setup.ts"),
  timeout: 120_000,
  expect: { timeout: 30_000 },
  fullyParallel: false, // Tests depend on shared wallet state
  forbidOnly: isCI,
  retries: isCI ? 1 : 0,
  workers: 1, // Sequential: tests share a single Bitcoin regtest network
  reporter: isCI
    ? [["github"], ["html", { open: "never" }]]
    : [["list"], ["html", { open: "on-failure" }]],

  use: {
    baseURL: process.env.JAM_URL || "https://localhost:29183",
    ignoreHTTPSErrors: true, // self-signed cert on jmwalletd
    trace: isCI ? "on" : "on",
    screenshot: isCI ? "on" : "on",
    video: isCI ? "on" : "on",
    actionTimeout: 15_000,
    navigationTimeout: 30_000,
  },

  projects: [
    {
      name: "chromium",
      use: {
        ...devices["Desktop Chrome"],
        launchOptions: slowMo ? { slowMo } : undefined,
      },
    },
  ],
});
