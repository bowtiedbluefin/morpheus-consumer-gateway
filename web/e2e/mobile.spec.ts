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
        budget: {
          max_session_stake_wei: "100000000000000000000",
          max_total_stake_wei: "400000000000000000000",
          max_price_per_second_wei: "1000000000000000000",
          min_liquid_mor_wei: "0",
          min_eth_wei: "100000000000000",
        },
        recovery: {
          enabled: true,
          auto_withdraw: true,
          cleanup_untracked_expired: true,
          cleanup_untracked_live: false,
          orphan_grace_seconds: 600,
          withdrawal_min_wei: "1000000000000000",
          withdrawal_interval_seconds: 300,
          provider_cooldown_seconds: 120,
        },
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
    if (path === "operations")
      json = {
        operations: [
          {
            id: "op1",
            kind: "close",
            state: "failed",
            message: "RPC_ERROR_".repeat(80),
            created_at: 1789350000,
          },
        ],
      };
    if (path === "events")
      json = {
        events: [
          {
            id: 1,
            action: "session_closed",
            time: 1789350000,
            session: "0x" + "a".repeat(64),
            tx: "0x" + "b".repeat(64),
            message: "NODE_TIMEOUT_".repeat(80),
          },
        ],
      };
    await route.fulfill({ json });
  });
  await page.goto("/");
  await expect(
    page.getByRole("heading", { name: "Overview", exact: true }),
  ).toBeVisible();
  await page.getByText("Exact amount", { exact: true }).first().click();
  await expect(
    page.getByText("14.38036530204770431", { exact: true }).first(),
  ).toBeVisible();
  await page.screenshot({
    path: "test-results/production-mobile.png",
    fullPage: true,
  });
  for (const label of [
    "Overview",
    "Wallet & recovery",
    "Models",
    "Providers & rating",
    "Sessions",
    "API keys",
    "Node & activity",
  ]) {
    await page
      .getByRole("navigation")
      .getByRole("button", { name: label })
      .click();
    await expect(
      page.getByRole("heading", { name: label, exact: true }).first(),
    ).toBeVisible();
    expect(
      await page.evaluate(() => document.documentElement.scrollWidth),
      label,
    ).toBeLessThanOrEqual(390);
  }
});
