import { expect, test } from "@playwright/test";

test("realistic wallet precision fits a 390px mobile viewport", async ({
  page,
}) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.route("**/admin/api/**", async (route) => {
    const path = new URL(route.request().url()).pathname.split("/").pop();
    let json: any = {};
    if (path === "me") json = { csrf: "dummy" };
    if (path === "policy")
      json = {
        revision: 0,
        models: [],
        providers: { mode: "all", allow: [], deny: [] },
        weights: {
          tps: 0.24,
          ttft: 0.08,
          duration: 0.24,
          success: 0.32,
          stake: 0.12,
        },
        max_sessions: 4,
        queue_seconds: 30,
        paused: false,
      };
    if (path === "status")
      json = {
        identity: {
          wallet: "0x" + "4".repeat(40),
          chain: "8453",
          version: "v7.11.0",
        },
        balances: { mor: "14380365302047704310", eth: "2460610244558729" },
        held: { available: "0", hold: "6119634697952295690" },
        active_requests: 0,
        queued: 0,
        live_sessions: 0,
        base_url: "http://localhost:8000/v1",
        helper_connected: true,
        helper_ready: true,
        rating_pending: false,
      };
    if (path === "keys") json = { keys: [] };
    if (path === "sessions") json = { sessions: [] };
    if (path === "operations") json = { operations: [] };
    if (path === "events") json = { events: [] };
    await route.fulfill({ json });
  });
  await page.goto("/");
  await expect(
    page.getByRole("heading", { name: "Overview", exact: true }),
  ).toBeVisible();
  await page.screenshot({
    path: "test-results/production-mobile.png",
    fullPage: true,
  });
  expect(
    await page.evaluate(() => document.documentElement.scrollWidth),
  ).toBeLessThanOrEqual(390);
});
