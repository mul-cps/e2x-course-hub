import { test, expect, type Page } from "@playwright/test";

async function workspacePage(
  page: Page,
  changes: Record<string, unknown> = {},
  deny = false,
) {
  const workspace = {
    id: "ws-a",
    course_id: "ml",
    group_id: "g",
    source: "local",
    state: "stopped",
    profile: "interactive-native-qualification-5",
    ...changes,
  };
  const writes: unknown[] = [],
    scopes: (string | null)[] = [];
  await page.addInitScript(() => {
    window.__APP_CONFIG__ = {
      baseUrl: "",
      apiUrl: "/api",
      user: { name: "manager", admin: false },
    };
  });
  await page.route(
    (url) => url.pathname.startsWith("/api/"),
    async (route) => {
      const req = route.request(),
        url = new URL(req.url());
      if (req.method() === "POST") {
        writes.push({ path: url.pathname, body: req.postDataJSON() });
        if (deny)
          return route.fulfill({
            status: 409,
            json: { detail: "Native cleanup remains unresolved" },
          });
        workspace.profile = req.postDataJSON().profile;
        return route.fulfill({ json: { workspace } });
      }
      if (url.pathname === "/api/compute") {
        const scope = url.searchParams.get("workspace");
        scopes.push(scope);
        return route.fulfill({
          json: {
            profiles:
              scope === "ws-a"
                ? {
                    "interactive-shared-5": {
                      kind: "interactive",
                      enabled: true,
                      nativeGroupWorkspace: "ws-a",
                      gpu: { mode: "shared" },
                    },
                    "interactive-shared-10": {
                      kind: "interactive",
                      enabled: true,
                      nativeGroupWorkspace: "ws-a",
                      gpu: { mode: "shared" },
                    },
                    "interactive-shared-global": {
                      kind: "interactive",
                      enabled: true,
                      gpu: { mode: "shared" },
                    },
                    foreign: {
                      kind: "interactive",
                      enabled: true,
                      nativeGroupWorkspace: "other",
                      gpu: { mode: "shared" },
                    },
                    batch: { kind: "batch", enabled: true },
                    disabled: {
                      kind: "interactive",
                      enabled: false,
                      nativeGroupWorkspace: "ws-a",
                    },
                  }
                : {},
          },
        });
      }
      return route.fulfill({
        json: {
          records: url.pathname === "/api/local/workspaces" ? [workspace] : [],
        },
      });
    },
  );
  await page.goto("/workspaces");
  await page
    .getByRole("button", { name: "View workspace profile ws-a", exact: true })
    .click();
  return {
    workspace,
    writes,
    scopes,
    dialog: page.getByRole("dialog", { name: "Workspace compute profile" }),
  };
}

test("stopped profile edit uses permitted workspace choices and refreshes the record", async ({
  page,
}) => {
  const { dialog, writes, scopes } = await workspacePage(page);
  await dialog
    .getByRole("button", { name: "Change profile", exact: true })
    .click();
  const selector = dialog.getByLabel("New compute profile", { exact: true });
  await expect(selector.locator("option")).toHaveText([
    "Select a profile",
    "interactive-shared-5",
    "interactive-shared-10",
  ]);
  await selector.selectOption("interactive-shared-10");
  await dialog
    .getByRole("button", { name: "Save profile", exact: true })
    .click();
  await expect(dialog).toHaveCount(0);
  await expect(
    page.getByRole("cell", { name: "interactive-shared-10", exact: true }),
  ).toBeVisible();
  expect(writes).toEqual([
    {
      path: "/api/workspaces/ws-a/profile",
      body: { profile: "interactive-shared-10" },
    },
  ]);
  expect(scopes).toContain("ws-a");
});

test("profile API failures remain visible and preserve the stored profile", async ({
  page,
}) => {
  const { dialog, writes } = await workspacePage(page, {}, true);
  await dialog
    .getByRole("button", { name: "Change profile", exact: true })
    .click();
  await dialog
    .getByLabel("New compute profile", { exact: true })
    .selectOption("interactive-shared-5");
  await dialog
    .getByRole("button", { name: "Save profile", exact: true })
    .click();
  await expect(dialog.getByRole("alert")).toHaveText(
    "Native cleanup remains unresolved",
  );
  await expect(dialog.getByTestId("stored-workspace-profile")).toHaveText(
    "interactive-native-qualification-5",
  );
  await expect(
    dialog.getByRole("button", { name: "Save profile", exact: true }),
  ).toBeEnabled();
  expect(writes).toHaveLength(1);
});

for (const change of [
  { state: "running" },
  { state: "starting" },
  { state: "stopping" },
  { state: "reconciling" },
  { archived: true },
  { archive_pending: true },
  { source: "moodle" },
]) {
  test(`profile change unavailable for ${JSON.stringify(change)}`, async ({
    page,
  }) => {
    const { dialog, writes } = await workspacePage(page, change);
    await expect(
      dialog.getByTestId("stored-profile-availability"),
    ).not.toHaveText("Checking availability…");
    await expect(
      dialog.getByRole("button", { name: "Change profile", exact: true }),
    ).toHaveCount(0);
    expect(writes).toEqual([]);
  });
}

test("unverified discovery does not allow a profile update", async ({
  page,
}) => {
  const { dialog, writes } = await workspacePage(page);
  await page.route(
    (url) => url.pathname === "/api/compute",
    (route) => route.fulfill({ status: 403, json: { detail: "Unavailable" } }),
  );
  await dialog.getByRole("button", { name: "Close profile details" }).click();
  await page
    .getByRole("button", { name: "View workspace profile ws-a", exact: true })
    .click();
  await expect(
    dialog.getByRole("button", { name: "Change profile", exact: true }),
  ).toBeDisabled();
  expect(writes).toEqual([]);
});
