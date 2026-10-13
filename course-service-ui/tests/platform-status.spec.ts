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
const availability = {
  state: "ready",
  revision: "sha256:" + "a".repeat(64),
  policyHash: "sha256:" + "b".repeat(64),
  approvedProfiles: ["interactive-shared-5"],
  approvedWorkspaceCount: 2,
  checkedAt: new Date().toISOString(),
};
const missingReport = {
  version: 1,
  state: "unavailable",
  title: "Production qualification report: unavailable",
  detail:
    "A current operator qualification report is unavailable. Broader production qualification remains unconfirmed.",
  updatedAt: new Date().toISOString(),
  checks: [],
};
test.beforeEach(async ({ page }) => {
  availability.checkedAt = new Date().toISOString();
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
  for (const path of ["/", "/compute", "/workspaces", "/integrations"]) {
    await page.goto(path);
    const panel = page.getByRole("complementary", {
      name: "GPU group sharing status",
    });
    await expect(panel.getByText(record.title, { exact: true })).toBeVisible();
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
  await expect(page.getByText(record.title, { exact: true })).toBeVisible();
  failed = true;
  await page.clock.fastForward(21000);
  await expect(
    page.getByRole("heading", {
      name: "GPU access: status unavailable",
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
      name: "GPU access: status unavailable",
    }),
  ).toBeVisible();
});

test("live approved GPU access remains visible when the qualification report is missing", async ({
  page,
}) => {
  await page.route("**/api/platform-status", (route) =>
    route.fulfill({ json: { ...missingReport, availability } }),
  );
  await page.goto("/compute");
  const panel = page.getByRole("complementary", {
    name: "GPU group sharing status",
  });
  await expect(
    panel.getByRole("heading", {
      name: "GPU access: approved workspaces ready",
    }),
  ).toBeVisible();
  await expect(panel).toContainText("interactive-shared-5");
  await expect(panel).toContainText("2 approved workspaces");
  await expect(
    panel.getByRole("group", { name: "Production qualification report" }),
  ).toContainText("Unknown");
  await expect(panel).toContainText(
    "Start still checks member grants, course ceilings and global reservations",
  );
  await expect(panel).toContainText(
    "Broader production qualification remains unconfirmed",
  );
});

test("an enabled qualification file cannot claim pending GPU approvals are ready", async ({
  page,
}) => {
  const enabled = {
    ...record,
    state: "enabled",
    checks: [{ id: "packing", label: "Packing", state: "passed" }],
  };
  await page.route("**/api/platform-status", (route) =>
    route.fulfill({
      json: { ...enabled, availability: { ...availability, state: "pending" } },
    }),
  );
  await page.goto("/workspaces");
  const panel = page.getByRole("complementary", {
    name: "GPU group sharing status",
  });
  await expect(
    panel.getByRole("heading", {
      name: "GPU access: approval activation pending",
    }),
  ).toBeVisible();
  await expect(
    panel.getByRole("group", { name: "Production qualification report" }),
  ).toContainText("Enabled");
  await expect(
    panel.getByRole("heading", {
      name: "GPU access: approved workspaces ready",
    }),
  ).toHaveCount(0);
});

test("stale or malformed live observations do not retain a ready GPU label", async ({
  page,
}) => {
  for (const changes of [
    { checkedAt: new Date(Date.now() - 31000).toISOString() },
    { revision: "a".repeat(64) },
    { policyHash: "b".repeat(64) },
    { approvedWorkspaceCount: true },
    { approvedProfiles: ["interactive-exclusive-1"] },
    { approvedProfiles: ["interactive-shared-5", "interactive-shared-5"] },
  ]) {
    await page.route("**/api/platform-status", (route) =>
      route.fulfill({
        json: { ...record, availability: { ...availability, ...changes } },
      }),
    );
    await page.goto("/compute");
    const panel = page.getByRole("complementary", {
      name: "GPU group sharing status",
    });
    await expect(
      panel.getByRole("heading", { name: "GPU access: status unavailable" }),
    ).toBeVisible();
    await expect(panel).toContainText("Blocked: Workspace startup");
    await expect(
      panel.getByRole("heading", {
        name: "GPU access: approved workspaces ready",
      }),
    ).toHaveCount(0);
    await page.unroute("**/api/platform-status");
  }
});

test("failed or stale polling clears previously ready live GPU observations", async ({
  page,
}) => {
  await page.clock.install();
  let failed = false;
  await page.route("**/api/platform-status", (route) =>
    failed
      ? route.fulfill({ status: 503 })
      : route.fulfill({ json: { ...missingReport, availability } }),
  );
  await page.goto("/compute");
  await expect(
    page.getByRole("heading", {
      name: "GPU access: approved workspaces ready",
    }),
  ).toBeVisible();
  failed = true;
  await page.clock.fastForward(21000);
  await expect(
    page.getByRole("heading", { name: "GPU access: status unavailable" }),
  ).toBeVisible();
  await expect(
    page.getByRole("heading", {
      name: "GPU access: approved workspaces ready",
    }),
  ).toHaveCount(0);
});
