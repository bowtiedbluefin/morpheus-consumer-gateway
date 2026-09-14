import { defineConfig } from "@playwright/test";
import base from "../playwright.config";
export default defineConfig({
  ...base,
  testDir: ".",
  testMatch: "*.spec.ts",
  webServer: { ...base.webServer, cwd: "../.." },
});
