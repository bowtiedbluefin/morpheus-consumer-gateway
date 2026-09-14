import { defineConfig } from "@playwright/test";
import base from "./playwright.config";
export default defineConfig({
  testMatch: "mobile.spec.ts",
  ...base,
  testDir: "./e2e",
});
