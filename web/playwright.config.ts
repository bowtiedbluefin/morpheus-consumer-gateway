import { defineConfig } from "@playwright/test";
import { mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

export default defineConfig({
  testDir: "./e2e",
  workers: 1,
  use: {
    baseURL: "http://127.0.0.1:8765",
    viewport: { width: 1440, height: 1080 },
  },
  webServer: {
    command:
      ".venv/bin/python -m uvicorn devtools.demo:create_app --factory --host 127.0.0.1 --port 8765 --no-access-log",
    cwd: "..",
    url: "http://127.0.0.1:8765/healthz",
    reuseExistingServer: false,
    env: {
      ADMIN_TOKEN: "browser-test-secret-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx",
      PUBLIC_ORIGIN: "http://127.0.0.1:8765",
      DATA_DIR: mkdtempSync(join(tmpdir(), "morpheus-e2e-")),
    },
  },
});
