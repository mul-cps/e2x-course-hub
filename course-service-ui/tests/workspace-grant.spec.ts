import { test, expect, type Page, type Route } from "@playwright/test";
async function grantDialog(
  page: Page,
  scoped: (route: Route) => Promise<void>,
) {
  const writes: unknown[] = [];
  await page.addInitScript(() => {
    window.__APP_CONFIG__ = {
      baseUrl: "",
      apiUrl: "/api",
      user: { name: "admin", admin: true },
    };
  });
  await page.route(
    (url) => url.pathname.startsWith("/api/"),
    async (route) => {
      const req = route.request(),
        url = new URL(req.url());
      if (req.method() === "POST") {
        writes.push(req.postDataJSON());
        return route.fulfill({ json: {} });
      }
      if (url.pathname === "/api/compute") {
        if (url.searchParams.has("workspace")) return scoped(route);
        return route.fulfill({
          json: {
            profiles: {
              cpu: {
                kind: "interactive",
                enabled: true,
                gpu: { mode: "none" },
              },
            },
          },
        });
      }
      return route.fulfill({
        json: {
          records:
            url.pathname === "/api/local/workspaces"
              ? [
                  { id: "ws-a", source: "local" },
                  { id: "ws-b", source: "local" },
                  { id: "external", source: "moodle" },
                ]
              : [],
        },
      });
    },
  );
  await page.goto("/compute");
  await page.getByRole("button", { name: "Add grant", exact: true }).click();
  return { dialog: page.getByRole("dialog"), writes };
}
const profiles = {
  "interactive-shared-5": {
    kind: "interactive",
    enabled: true,
    nativeGroupWorkspace: "ws-a",
    gpu: { mode: "shared" },
  },
  global: { kind: "interactive", enabled: true, gpu: { mode: "shared" } },
  foreign: {
    kind: "interactive",
    enabled: true,
    nativeGroupWorkspace: "ws-other",
    gpu: { mode: "shared" },
  },
  batch: {
    kind: "batch",
    enabled: true,
    nativeGroupWorkspace: "ws-a",
    gpu: { mode: "shared" },
  },
  disabled: {
    kind: "interactive",
    enabled: false,
    nativeGroupWorkspace: "ws-a",
    gpu: { mode: "shared" },
  },
};
test("grant discovery merges approved workspace GPU choices without extending grant payload", async ({
  page,
}) => {
  const scopes: string[] = [];
  const { dialog, writes } = await grantDialog(page, async (route) => {
    scopes.push(new URL(route.request().url()).searchParams.get("workspace")!);
    await route.fulfill({ json: { profiles } });
  });
  await expect(
    dialog.getByLabel("Shared workspace", { exact: true }).locator("option"),
  ).toHaveText(["No shared workspace", "ws-a", "ws-b"]);
  await dialog
    .getByLabel("Shared workspace", { exact: true })
    .selectOption("ws-a");
  const choices = dialog.getByLabel("Allowed profiles", { exact: true });
  await expect(choices.locator("option")).toHaveText([
    "cpu",
    "interactive-shared-5",
  ]);
  await choices.selectOption(["cpu", "interactive-shared-5"]);
  await dialog.getByLabel(/^Identifier/).fill("grant");
  await dialog
    .getByLabel("Canonical person ID", { exact: false })
    .fill("person");
  await dialog
    .getByLabel("Expires (UTC)", { exact: false })
    .fill("2026-12-01T00:00");
  await dialog
    .getByLabel("Reason for access", { exact: false })
    .fill("Course GPU access");
  await dialog.getByRole("button", { name: "Save grant", exact: true }).click();
  await expect(dialog).toHaveCount(0);
  expect(writes).toEqual([
    {
      id: "grant",
      person: "person",
      profiles: ["cpu", "interactive-shared-5"],
      projects: [],
      allowance: 1,
      expires: "2026-12-01T00:00:00.000Z",
      reason: "Course GPU access",
    },
  ]);
  expect(scopes).toEqual(["ws-a"]);
});
test("changing grant workspace clears old GPU selection and ignores delayed scope responses", async ({
  page,
}) => {
  let delayed: Route | undefined;
  const { dialog, writes } = await grantDialog(page, async (route) => {
    const scope = new URL(route.request().url()).searchParams.get("workspace");
    if (scope === "ws-a") {
      delayed = route;
      return;
    }
    await route.fulfill({ json: { profiles: {} } });
  });
  const selector = dialog.getByLabel("Shared workspace", { exact: true });
  await selector.selectOption("ws-a");
  await expect.poll(() => Boolean(delayed)).toBeTruthy();
  await selector.selectOption("ws-b");
  await delayed!.fulfill({ json: { profiles } });
  await expect(
    dialog.getByLabel("Allowed profiles", { exact: true }).locator("option"),
  ).toHaveText(["cpu"]);
  expect(writes).toEqual([]);
});
test("failed workspace discovery preserves CPU choices and exposes the failure", async ({
  page,
}) => {
  const { dialog, writes } = await grantDialog(page, (route) =>
    route.fulfill({
      status: 403,
      json: { detail: "Owned workspace scope required" },
    }),
  );
  await dialog
    .getByLabel("Shared workspace", { exact: true })
    .selectOption("ws-a");
  await expect(dialog.getByRole("alert")).toContainText(
    "Workspace GPU profiles could not be verified",
  );
  await expect(
    dialog.getByLabel("Allowed profiles", { exact: true }).locator("option"),
  ).toHaveText(["cpu"]);
  expect(writes).toEqual([]);
});

test("changing the selected workspace removes its GPU grant choice while preserving CPU selection", async ({
  page,
}) => {
  const { dialog } = await grantDialog(page, (route) =>
    route.fulfill({ json: { profiles } }),
  );
  const workspace = dialog.getByLabel("Shared workspace", { exact: true });
  const choices = dialog.getByLabel("Allowed profiles", { exact: true });
  await workspace.selectOption("ws-a");
  await expect(choices.locator("option")).toHaveText([
    "cpu",
    "interactive-shared-5",
  ]);
  await choices.selectOption(["cpu", "interactive-shared-5"]);
  await workspace.selectOption("");
  await expect(choices.locator("option")).toHaveText(["cpu"]);
  expect(
    await choices.evaluate((element: HTMLSelectElement) =>
      Array.from(element.selectedOptions, (option) => option.value),
    ),
  ).toEqual(["cpu"]);
});
