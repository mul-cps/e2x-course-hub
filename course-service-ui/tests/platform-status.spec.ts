import { test, expect } from "@playwright/test";

const record = {
  version: 1,
  state: "qualification",
  title: "GPU group sharing: qualification in progress",
  detail:
    "Workspace startup is blocked. Standard GPU profiles remain disabled.",
  updatedAt: new Date().toISOString(),
  checks: [
    { id: "apps", label: "Applications", state: "passed" },
    { id: "startup", label: "Workspace startup", state: "blocked" },
    { id: "cleanup", label: "Cleanup", state: "pending" },
  ],
};
test.beforeEach(async ({ page }) => {
  await page.addInitScript(() => {
    window.__APP_CONFIG__ = {
      baseUrl: "",
      apiUrl: "/api",
      user: { name: "admin", admin: true },
    };
  });
  await page.route(
    (url) => url.pathname.startsWith("/api/"),
    (route) => route.fulfill({ json: { records: [], profiles: {} } }),
  );
});

test("operator status shows blockers and remaining checks on both pages", async ({
  page,
}) => {
  await page.route("**/api/platform-status", (route) =>
    route.fulfill({ json: record }),
  );
  for (const path of ["/compute", "/workspaces"]) {
    await page.goto(path);
    const panel = page.getByRole("complementary", {
      name: "GPU group sharing status",
    });
    await expect(
      panel.getByRole("heading", { name: record.title }),
    ).toBeVisible();
    await expect(panel).toContainText("Blocked: Workspace startup");
    await expect(panel).toContainText("Remaining checks: Cleanup");
    await expect(panel).toContainText("Passed: Applications");
    await page.setViewportSize({ width: 390, height: 844 });
    expect(
      await page.evaluate(
        () => document.documentElement.scrollWidth <= innerWidth,
      ),
    ).toBeTruthy();
  }
});

test("poll failure removes previous availability and navigation cancels polling", async ({
  page,
}) => {
  await page.clock.install();
  let calls = 0;
  let failed = false;
  await page.route("**/api/platform-status", (route) => {
    calls += 1;
    return failed
      ? route.fulfill({ status: 503 })
      : route.fulfill({ json: record });
  });
  await page.goto("/compute");
  await expect(page.getByRole("heading", { name: record.title })).toBeVisible();
  failed = true;
  await page.clock.fastForward(21000);
  await expect(
    page.getByRole("heading", {
      name: "GPU group sharing: status unavailable",
    }),
  ).toBeVisible();
  await page.getByRole("link", { name: "Courses", exact: true }).click();
  const stoppedCalls = calls;
  await page.clock.fastForward(60000);
  expect(calls).toBe(stoppedCalls);
});

test("invalid response cannot show an enabled status", async ({ page }) => {
  await page.route("**/api/platform-status", (route) =>
    route.fulfill({ json: { state: "enabled", title: "Ready" } }),
  );
  await page.goto("/workspaces");
  await expect(
    page.getByRole("heading", {
      name: "GPU group sharing: status unavailable",
    }),
  ).toBeVisible();
});
