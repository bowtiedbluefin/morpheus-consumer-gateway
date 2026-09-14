import { expect, test } from "@playwright/test";
import { mkdirSync } from "node:fs";

test("configure model, create key, infer, enforce blocklist and restart from browser", async ({
  page,
  request,
}) => {
  const errors: string[] = [];
  page.on("pageerror", (e) => errors.push(e.message));
  await page.goto("/");
  await page
    .getByLabel("Administrator secret")
    .fill("browser-test-secret-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx");
  await page.getByRole("button", { name: "Open dashboard" }).click();
  await expect(
    page.getByRole("heading", { name: "Overview", exact: true }),
  ).toBeVisible();
  await expect(
    page.getByText("DEMO · Simulated node", { exact: false }),
  ).toBeVisible();
  await page.getByRole("button", { name: "03 Models" }).click();
  await page.getByRole("button", { name: "Browse node catalog" }).click();
  await page.getByRole("button", { name: /Demo model/ }).click();
  await page.getByLabel("Session duration (minutes)").fill("10");
  await page.getByLabel("Keep sessions").selectOption("until_expiry");
  await page
    .getByRole("button", { name: "Save settings", exact: true })
    .click();
  await expect(page.getByText("You have unsaved settings.")).not.toBeVisible();
  await page.getByRole("button", { name: "06 API keys" }).click();
  await page.getByLabel("Application name").fill("Browser integration");
  await page.getByRole("button", { name: "Create key", exact: true }).click();
  const token = await page.locator(".secret-box code").innerText();
  const body = {
    model: "demo-model",
    messages: [{ role: "user", content: "Hello" }],
  };
  const headers = { Authorization: `Bearer ${token}` };
  expect(
    (
      await request.post("/v1/chat/completions", { data: body, headers })
    ).status(),
  ).toBe(200);
  const stream = await request.post("/v1/chat/completions", {
    data: { ...body, stream: true },
    headers,
  });
  expect(await stream.text()).toContain("data: [DONE]");
  await page.getByRole("button", { name: "01 Overview" }).click();
  await expect(page.locator(".stat").first().locator("strong")).toHaveText(
    "1",
    { timeout: 10000 },
  );
  mkdirSync("../docs/images", { recursive: true });
  await page.screenshot({
    path: "../docs/images/dashboard.png",
    fullPage: true,
  });
  await page.getByRole("button", { name: "04 Providers & rating" }).click();
  await page
    .getByLabel("Blocklist (one address per line)")
    .fill("0x" + "3".repeat(40));
  await page.getByRole("heading", { name: "Provider access" }).click();
  await page
    .getByRole("button", { name: "Save settings", exact: true })
    .last()
    .click();
  await expect(page.getByText("You have unsaved settings.")).not.toBeVisible();
  expect(
    (
      await request.post("/v1/chat/completions", { data: body, headers })
    ).status(),
  ).toBe(503);
  await page.getByRole("button", { name: "07 Node & activity" }).click();
  await page
    .getByRole("button", { name: "Apply rating and restart", exact: true })
    .click();
  await expect(page.getByText("succeeded", { exact: true })).toBeVisible({
    timeout: 10000,
  });
  expect(errors).toEqual([]);
  await page.setViewportSize({ width: 390, height: 844 });
  await page.getByRole("button", { name: "Overview", exact: true }).click();
  await page.screenshot({ path: "test-results/mobile.png", fullPage: true });
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= window.innerWidth,
    ),
  ).toBe(true);
});

test("wallet recovery exposes exact limits, preserves locked balance and schedules a scan", async ({
  page,
}) => {
  await page.goto("/");
  await page
    .getByLabel("Administrator secret")
    .fill("browser-test-secret-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx");
  await page.getByRole("button", { name: "Open dashboard" }).click();
  await page.getByRole("button", { name: "02 Wallet & recovery" }).click();
  await expect(
    page.getByRole("heading", { name: "Automatic recovery", exact: true }),
  ).toBeVisible();
  await expect(
    page.getByText("Withdrawable MOR", { exact: true }),
  ).toBeVisible();
  await expect(
    page.getByText("Still locked MOR", { exact: true }),
  ).toBeVisible();
  await page.getByLabel("Minimum withdrawal (wei)").fill("2000000000000000");
  await page
    .getByRole("button", { name: "Save settings", exact: true })
    .click();
  await expect(page.getByText("You have unsaved settings.")).not.toBeVisible();
  await page
    .getByRole("button", { name: "Run recovery now", exact: true })
    .click();
  await expect(page.getByRole("status")).toContainText(
    "Recovery scan scheduled",
  );
  await page.screenshot({
    path: "test-results/wallet-recovery.png",
    fullPage: true,
  });
});
