// Regression tests for the independently reproduced customer-experience findings.
import { expect, test, Page } from "@playwright/test";
const model = {
  id: "0x" + "1".repeat(64),
  alias: "demo",
  enabled: true,
  duration_seconds: 1800,
  retention: "on_demand",
  idle_seconds: 300,
  max_sessions: 1,
  warm_until: null,
};
const policy = {
  revision: 0,
  models: [model],
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
const status = {
  identity: {
    wallet: "0x" + "4".repeat(40),
    chain: "84532",
    version: "7.11.0",
  },
  balances: { mor: "1000000000000000000", eth: "0" },
  held: null,
  node_error: null,
  last_error: null,
  active_requests: 0,
  queued: 0,
  maintenance: false,
  paused: false,
  helper_connected: true,
  base_url: "http://127.0.0.1:8765/v1",
  rating_pending: false,
  demo: true,
};
function latch() {
  let resolve!: () => void;
  const promise = new Promise<void>((r) => (resolve = r));
  return { promise, resolve };
}
async function mock(
  page: Page,
  intercept?: (
    path: string,
    method: string,
    body: any,
  ) => Promise<any | undefined>,
) {
  await page.route("**/admin/api/**", async (route) => {
    const req = route.request(),
      path = new URL(req.url()).pathname.replace("/admin/api", "");
    const body = req.postData() ? req.postDataJSON() : null;
    const special = await intercept?.(path, req.method(), body);
    if (special) {
      await route.fulfill(special);
      return;
    }
    let result: any = {};
    if (path === "/me") result = { csrf: "audit-csrf" };
    if (path === "/policy") result = policy;
    if (path === "/status") result = status;
    if (path === "/sessions") result = { sessions: [] };
    if (path === "/keys") result = { keys: [] };
    if (path === "/operations") result = { operations: [] };
    if (path === "/events") result = { events: [] };
    await route.fulfill({ json: result });
  });
  await page.goto("/");
  await expect(
    page.getByRole("heading", { name: "Overview", exact: true }),
  ).toBeVisible();
}

test("edits made while Save is pending remain in the unsaved draft", async ({
  page,
}) => {
  const sent = latch(),
    release = latch();
  await mock(page, async (path, method, body) => {
    if (path === "/policy" && method === "PUT") {
      sent.resolve();
      await release.promise;
      return { json: { ...body, revision: body.revision + 1 } };
    }
  });
  await page.getByRole("button", { name: "03 Models" }).click();
  const input = page.getByLabel("API model alias");
  await input.fill("first-edit");
  await page
    .getByRole("button", { name: "Save settings", exact: true })
    .click();
  await sent.promise;
  await input.fill("newer-unsaved-edit");
  release.resolve();
  await expect(input).toHaveValue("newer-unsaved-edit");
  await expect(page.getByText("You have unsaved settings.")).toBeVisible();
});

test("expired login can always return to sign-in", async ({ page }) => {
  let expired = false;
  await mock(page, async () =>
    expired
      ? {
          status: 401,
          json: {
            error: { message: "Sign in to administer this installation" },
          },
        }
      : undefined,
  );
  expired = true;
  await page.getByRole("button", { name: "Sign out", exact: true }).click();
  await expect(page.getByLabel("Administrator secret")).toBeVisible();
  await expect(
    page.getByText("Gateway online", { exact: true }),
  ).not.toBeVisible();
});

test("rating apply drains even when immediate restart was selected elsewhere", async ({
  page,
}) => {
  const requested = latch();
  let restartBody: any;
  await mock(page, async (path, method, body) => {
    if (path === "/node/restart" && method === "POST") {
      restartBody = body;
      requested.resolve();
      return { json: { id: "audit-op" } };
    }
  });
  await page.getByRole("button", { name: "07 Node & activity" }).click();
  await page
    .getByLabel("Restart immediately, interrupting active requests")
    .check();
  await page.getByRole("button", { name: "04 Providers & rating" }).click();
  await expect(
    page.getByText("Apply restarts your node after active requests finish.", {
      exact: false,
    }),
  ).toBeVisible();
  await page
    .getByRole("button", { name: "Apply and restart node", exact: true })
    .click();
  await requested.promise;
  expect(restartBody).toEqual({ immediate: false, apply_rating: true });
});

test("one hanging status response leaves fresh data and actions usable", async ({
  page,
}) => {
  let hang = false;
  const entered = latch(),
    release = latch();
  await mock(page, async (path, method, body) => {
    if (path === "/policy" && method === "PUT") {
      hang = true;
      return { json: { ...body, revision: 1 } };
    }
    if (path === "/status" && hang) {
      entered.resolve();
      await release.promise;
      return { json: status };
    }
    if (path === "/sessions" && hang)
      return {
        json: {
          sessions: [
            {
              id: "new-session",
              state: "open",
              alias: "demo",
              chain_id: "0x" + "2".repeat(64),
              provider: "0x" + "3".repeat(40),
              ends_at: 2000000000,
              stake_wei: "1000",
              busy: false,
            },
          ],
        },
      };
  });
  await page.getByRole("button", { name: "03 Models" }).click();
  await page.getByLabel("API model alias").fill("saved-demo");
  await page
    .getByRole("button", { name: "Save settings", exact: true })
    .click();
  await entered.promise;
  await expect(
    page.getByRole("button", { name: "Browse node catalog" }),
  ).toBeEnabled();
  await page.getByRole("button", { name: "01 Overview" }).click();
  await expect(page.locator(".stat").first().locator("strong")).toHaveText("1");
  release.resolve();
  await expect(page.locator(".stat").first().locator("strong")).toHaveText("1");
});
