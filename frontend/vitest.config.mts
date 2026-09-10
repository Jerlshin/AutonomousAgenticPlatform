import react from "@vitejs/plugin-react";
import { fileURLToPath } from "node:url";
import { defineConfig } from "vitest/config";

/**
 * docs/FRONTEND.md §13.
 *
 * `jsdom` rather than a browser runner: the highest-value suite in this app is the
 * protocol suite, which drives a *mock* `WebSocket` through the §3.8 checklist. A real
 * browser would give it a real socket and nothing to assert against.
 *
 * `tests/e2e` is excluded because Playwright owns it; running a Playwright spec under
 * Vitest fails with an error about a missing `test` export, which is a confusing way to
 * learn that two runners are pointed at one directory.
 */
export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: {
      "@": fileURLToPath(new URL("./src", import.meta.url)),
    },
  },
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["./tests/setup.ts"],
    include: ["tests/**/*.test.ts", "tests/**/*.test.tsx"],
    exclude: ["tests/e2e/**", "node_modules/**"],
    restoreMocks: true,
  },
});
