import { defineConfig } from "@playwright/test";
import { fileURLToPath } from "node:url";

const testDir = fileURLToPath(new URL("./e2e", import.meta.url));

export default defineConfig({
  testDir,
  testMatch: /.*\.spec\.js/,
  timeout: 30_000,
  fullyParallel: false,
  use: {
    baseURL: "http://127.0.0.1:4173",
    trace: "retain-on-failure",
  },
  webServer: {
    command: "npm run dev -- --port 4173",
    url: "http://127.0.0.1:4173",
    reuseExistingServer: true,
  },
});
