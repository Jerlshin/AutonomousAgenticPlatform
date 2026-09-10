import { defineConfig, devices } from "@playwright/test";

/**
 * docs/FRONTEND.md §13.
 *
 * The suite drives the app against a **recorded stream fixture**, not a live backend:
 * REST and the WebSocket are both intercepted in the browser context, so the whole
 * journey — submit a task, watch a run, approve a gate, read the report — runs in CI in
 * seconds and produces the same result every time. A live backend would make the most
 * valuable assertions (a gate opening, a replay gap) the least reproducible ones.
 */
export default defineConfig({
  testDir: "./tests/e2e",
  fullyParallel: true,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 1 : 0,
  workers: process.env.CI ? 2 : undefined,
  reporter: process.env.CI ? "github" : "list",
  use: {
    baseURL: "http://127.0.0.1:3100",
    trace: "on-first-retry",
    // The deck is specified to be usable at 1280×800 (§8.1); testing at that size is how
    // "usable" stops being an opinion.
    viewport: { width: 1280, height: 800 },
  },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }],
  webServer: {
    command: "npm run build && npx next start --port 3100",
    url: "http://127.0.0.1:3100",
    reuseExistingServer: !process.env.CI,
    timeout: 180_000,
    env: {
      NEXT_PUBLIC_API_BASE: "http://127.0.0.1:8099",
      NEXT_PUBLIC_API_TOKEN: "",
    },
  },
});
