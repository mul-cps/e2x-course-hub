import { test, expect, type Page } from "@playwright/test";

async function setup(page: Page) {
  const workspace = {
    id: "ws-a",
    course_id: "ml",
    term_id: "t",
    group_id: "team",
    source: "local",
    state: "stopped",
    profile: "interactive-cpu",
  };
  const data = {
    workspace,
    workspaces: [workspace],
    profiles: {
      "interactive-cpu": {
        kind: "interactive",
        enabled: true,
        gpu: { mode: "none" },
      },
      "interactive-shared-5": {
        kind: "interactive",
        enabled: true,
        nativeGroupWorkspace: "ws-a",
        gpu: { mode: "shared" },
      },
    } as Record<string, unknown>,
    grants: [
      {
        id: "grant-a",
        person: "person-a",
        profiles: ["interactive-cpu"],
        allowance: 1,
        reason: "Existing",
        expires: "2026-12-01T00:00:00Z",
      },
    ],
    projects: [{ id: "project-a", name: "Before project" }],
    recordReads: 0,
    held: false,
    writes: 0,
    readHeaders: [] as Record<string, string>[],
  };
  await page.addInitScript(() => {
    window.__APP_CONFIG__ = {
      baseUrl: "",
      apiUrl: "/api",
      user: { name: "admin", admin: true },
    };
    document.cookie = "_xsrf=test-xsrf; path=/";
  });
  await page.clock.install();
  await page.route(
    (url) => url.pathname.startsWith("/api/"),
    async (route) => {
      const req = route.request(),
        url = new URL(req.url());
      if (req.method() !== "GET") {
        data.writes++;
        return route.fulfill({ json: {} });
      }
      if (url.pathname === "/api/compute")
        return route.fulfill({
          json: url.searchParams.has("person")
            ? { grants: data.grants }
            : { profiles: data.profiles },
        });
      if (url.pathname === "/api/local/workspaces") {
        data.recordReads++;
        data.readHeaders.push(req.headers());
        if (data.held) return;
        return route.fulfill({ json: { records: data.workspaces } });
      }
      if (url.pathname === "/api/native-group-approvals")
        return route.fulfill({ status: 403, json: { detail: "Unavailable" } });
      const records =
        url.pathname === "/api/local/projects"
          ? data.projects
          : url.pathname === "/api/local/courses"
            ? [{ id: "ml", name: "ML", source: "local" }]
            : url.pathname === "/api/local/groups"
              ? [{ id: "team", name: "Team", course_id: "ml" }]
              : url.pathname === "/api/local/terms"
                ? [{ id: "t", name: "Term", course_id: "ml" }]
                : [];
      return route.fulfill({ json: { records } });
    },
  );
  return data;
}

test("automatic record refresh updates workspace state without losing search", async ({
  page,
}) => {
  const data = await setup(page);
  await page.goto("/workspaces");
  await expect(
    page.getByRole("cell", { name: "stopped", exact: true }),
  ).toBeVisible();
  await page
    .getByRole("textbox", { name: "Search Shared workspaces" })
    .fill("ws-a");
  data.workspace.state = "running";
  await page.clock.runFor(20_000);
  await expect(
    page.getByRole("cell", { name: "running", exact: true }),
  ).toBeVisible();
  await expect(
    page.getByRole("textbox", { name: "Search Shared workspaces" }),
  ).toHaveValue("ws-a");
});

test("profile refresh preserves the selected edit and focus when permission disappears", async ({
  page,
}) => {
  const data = await setup(page);
  await page.goto("/workspaces");
  await page
    .getByRole("button", { name: "View workspace profile ws-a", exact: true })
    .click();
  const dialog = page.getByRole("dialog", {
    name: "Workspace compute profile",
  });
  await dialog
    .getByRole("button", { name: "Change profile", exact: true })
    .click();
  const selection = dialog.getByLabel("New compute profile", { exact: true });
  await selection.selectOption("interactive-shared-5");
  await selection.focus();
  delete data.profiles["interactive-shared-5"];
  await page.clock.runFor(20_000);
  await expect(selection.locator("option:checked")).toContainText(
    "unavailable",
  );
  await expect(selection).toHaveValue("interactive-shared-5");
  await expect(selection).toBeFocused();
  await expect(
    dialog.getByRole("button", { name: "Save profile", exact: true }),
  ).toBeDisabled();
  await expect(dialog).toBeVisible();
});

test("background polling pauses while hidden and refreshes on return", async ({
  page,
}) => {
  const data = await setup(page);
  await page.goto("/workspaces");
  await expect(
    page.getByRole("cell", { name: "stopped", exact: true }),
  ).toBeVisible();
  await page.evaluate(() => {
    Object.defineProperty(document, "hidden", {
      configurable: true,
      value: true,
    });
    document.dispatchEvent(new Event("visibilitychange"));
  });
  const reads = data.recordReads;
  data.workspace.state = "running";
  await page.clock.runFor(40_000);
  expect(data.recordReads).toBe(reads);
  await page.evaluate(() => {
    Object.defineProperty(document, "hidden", {
      configurable: true,
      value: false,
    });
    document.dispatchEvent(new Event("visibilitychange"));
  });
  await expect(
    page.getByRole("cell", { name: "running", exact: true }),
  ).toBeVisible();
});

test("an eight second read timeout retains records and marks them stale", async ({
  page,
}) => {
  const data = await setup(page);
  await page.goto("/workspaces");
  await expect(
    page.getByRole("cell", { name: "stopped", exact: true }),
  ).toBeVisible();
  data.held = true;
  await page.clock.runFor(20_000);
  await page.clock.runFor(8_000);
  await expect(page.getByTestId("admin-data-freshness")).toContainText("stale");
  await expect(page.getByTestId("admin-data-freshness")).toContainText(
    "timed out",
  );
  await expect(
    page.getByRole("cell", { name: "stopped", exact: true }),
  ).toBeVisible();
  data.held = false;
  data.workspace.state = "running";
  await page.clock.runFor(12_000);
  await expect(
    page.getByRole("cell", { name: "running", exact: true }),
  ).toBeVisible();
  await expect(page.getByTestId("admin-data-freshness")).not.toContainText(
    "stale",
  );
});

test("late read from a superseded person cannot replace the current grants", async ({
  page,
}) => {
  const data = await setup(page);
  await page.addInitScript(() => {
    const original = window.fetch.bind(window);
    window.fetch = (input, options) => {
      const url = new URL(String(input), location.origin);
      if (
        url.pathname === "/api/compute" &&
        url.searchParams.get("person") === "old-person"
      ) {
        // A transport may deliver an already buffered response despite cancellation.
        return new Promise<Response>((resolve) => {
          (window as unknown as { finishOld: () => void }).finishOld = () =>
            resolve(
              new Response(
                JSON.stringify({
                  grants: [{ id: "old-grant", person: "old-person" }],
                }),
                {
                  status: 200,
                  headers: { "Content-Type": "application/json" },
                },
              ),
            );
        });
      }
      return original(input, options);
    };
  });
  await page.goto("/compute");
  const lookup = page.getByPlaceholder("Reviewed canonical person ID");
  await lookup.fill("old-person");
  await page.getByRole("button", { name: "View grants", exact: true }).click();
  await expect
    .poll(() =>
      page.evaluate(
        () =>
          typeof (window as unknown as { finishOld?: () => void }).finishOld,
      ),
    )
    .toBe("function");
  data.grants[0].id = "current-grant";
  await lookup.fill("new-person");
  await page.getByRole("button", { name: "View grants", exact: true }).click();
  await expect(
    page.getByRole("cell", { name: "current-grant", exact: true }),
  ).toBeVisible();
  await page.evaluate(() =>
    (window as unknown as { finishOld: () => void }).finishOld(),
  );
  await page.evaluate(
    () =>
      new Promise<void>((resolve) => requestAnimationFrame(() => resolve())),
  );
  await expect(
    page.getByRole("cell", { name: "current-grant", exact: true }),
  ).toBeVisible();
  await expect(
    page.getByRole("cell", { name: "old-grant", exact: true }),
  ).toHaveCount(0);
});

test("grant polling preserves draft text and removed profile choices", async ({
  page,
}) => {
  const data = await setup(page);
  await page.goto("/compute");
  await page
    .getByRole("textbox", { name: "Search Compute access" })
    .fill("interactive");
  await page.getByRole("button", { name: "Add grant", exact: true }).click();
  const dialog = page.getByRole("dialog", { name: "Compute access" });
  await dialog.getByLabel(/^Identifier/).fill("new-grant");
  await dialog
    .getByLabel("Canonical person ID", { exact: false })
    .fill("person-a");
  await dialog.getByLabel("Expires", { exact: false }).fill("2026-12-01T00:00");
  const reason = dialog.getByLabel("Reason for access", { exact: false });
  await reason.fill("Unsaved teaching reason");
  await dialog
    .getByLabel("Shared workspace", { exact: true })
    .selectOption("ws-a");
  const profiles = dialog.getByLabel("Allowed profiles", { exact: true });
  await profiles.selectOption(["interactive-cpu", "interactive-shared-5"]);
  await reason.focus();
  delete data.profiles["interactive-shared-5"];
  data.workspaces.length = 0;
  await page.clock.runFor(20_000);
  await expect(
    dialog.getByLabel("Shared workspace", { exact: true }),
  ).toHaveValue("ws-a");
  await expect(
    dialog
      .getByLabel("Shared workspace", { exact: true })
      .locator("option:checked"),
  ).toContainText("unavailable");
  await expect(profiles.locator("option:checked")).toHaveText([
    "interactive-shared-5 (unavailable)",
    "interactive-cpu",
  ]);
  await expect(reason).toHaveValue("Unsaved teaching reason");
  await expect(reason).toBeFocused();
  await dialog.getByRole("button", { name: "Save grant", exact: true }).click();
  await expect(dialog.getByRole("alert")).toContainText(
    "Select profiles verified",
  );
  expect(data.writes).toBe(0);
  await expect(
    page.getByRole("textbox", { name: "Search Compute access" }),
  ).toHaveValue("interactive");
});

test("refresh GETs keep same-origin cookies and XSRF authentication", async ({
  page,
}) => {
  const data = await setup(page);
  await page.goto("/workspaces");
  await expect(
    page.getByRole("cell", { name: "stopped", exact: true }),
  ).toBeVisible();
  data.workspace.state = "running";
  await page.clock.runFor(20_000);
  await expect(
    page.getByRole("cell", { name: "running", exact: true }),
  ).toBeVisible();
  expect(data.readHeaders.length).toBeGreaterThanOrEqual(2);
  for (const headers of data.readHeaders) {
    expect(headers["cookie"]).toContain("_xsrf=test-xsrf");
    expect(headers["x-xsrftoken"]).toBe("test-xsrf");
  }
});

test("superseded refresh in the same scope ignores an already buffered old response", async ({
  page,
}) => {
  const data = await setup(page);
  await page.addInitScript(() => {
    const original = window.fetch.bind(window);
    const controls = window as unknown as {
      holdNext?: boolean;
      finishOld?: () => void;
    };
    window.fetch = (input, options) => {
      const url = new URL(String(input), location.origin);
      if (url.pathname === "/api/local/workspaces" && controls.holdNext) {
        controls.holdNext = false;
        return new Promise<Response>((resolve) => {
          controls.finishOld = () =>
            resolve(
              new Response(
                JSON.stringify({
                  records: [
                    {
                      id: "ws-a",
                      state: "stopped",
                      source: "local",
                      profile: "interactive-cpu",
                    },
                  ],
                }),
                {
                  status: 200,
                  headers: { "Content-Type": "application/json" },
                },
              ),
            );
        });
      }
      return original(input, options);
    };
  });
  await page.goto("/workspaces");
  await expect(
    page.getByRole("cell", { name: "stopped", exact: true }),
  ).toBeVisible();
  await page.evaluate(() => {
    (window as unknown as { holdNext: boolean }).holdNext = true;
  });
  await page.clock.runFor(20_000);
  await expect
    .poll(() =>
      page.evaluate(
        () =>
          typeof (window as unknown as { finishOld?: () => void }).finishOld,
      ),
    )
    .toBe("function");
  data.workspace.state = "running";
  await page.getByRole("button", { name: "Refresh", exact: true }).click();
  await expect(
    page.getByRole("cell", { name: "running", exact: true }),
  ).toBeVisible();
  await page.evaluate(() =>
    (window as unknown as { finishOld: () => void }).finishOld(),
  );
  await page.evaluate(
    () =>
      new Promise<void>((resolve) => requestAnimationFrame(() => resolve())),
  );
  await expect(
    page.getByRole("cell", { name: "running", exact: true }),
  ).toBeVisible();
});

test("a resumed grant read must be fresh before submitting a preserved GPU selection", async ({
  page,
}) => {
  const data = await setup(page);
  await page.addInitScript(() => {
    const original = window.fetch.bind(window);
    const control = window as unknown as {
      holdGrant?: boolean;
      finishGrant?: () => void;
    };
    window.fetch = (input, options) => {
      const url = new URL(String(input), location.origin);
      if (
        url.pathname === "/api/compute" &&
        url.searchParams.get("workspace") === "ws-a" &&
        control.holdGrant
      ) {
        control.holdGrant = false;
        return new Promise<Response>((resolve) => {
          control.finishGrant = () =>
            resolve(
              new Response(
                JSON.stringify({
                  profiles: {
                    "interactive-shared-5": {
                      kind: "interactive",
                      enabled: true,
                      nativeGroupWorkspace: "ws-a",
                      gpu: { mode: "shared" },
                    },
                  },
                }),
                {
                  status: 200,
                  headers: { "Content-Type": "application/json" },
                },
              ),
            );
        });
      }
      return original(input, options);
    };
  });
  await page.goto("/compute");
  await page.getByRole("button", { name: "Add grant", exact: true }).click();
  const dialog = page.getByRole("dialog", { name: "Compute access" });
  await dialog.getByLabel(/^Identifier/).fill("fresh-grant");
  await dialog
    .getByLabel("Canonical person ID", { exact: false })
    .fill("person-a");
  await dialog.getByLabel("Expires", { exact: false }).fill("2026-12-01T00:00");
  await dialog
    .getByLabel("Reason for access", { exact: false })
    .fill("Preserved reason");
  await dialog
    .getByLabel("Shared workspace", { exact: true })
    .selectOption("ws-a");
  const profiles = dialog.getByLabel("Allowed profiles", { exact: true });
  await profiles.selectOption("interactive-shared-5");
  await page.evaluate(() => {
    Object.defineProperty(document, "hidden", {
      configurable: true,
      value: true,
    });
    document.dispatchEvent(new Event("visibilitychange"));
    (window as unknown as { holdGrant: boolean }).holdGrant = true;
  });
  data.projects[0].name = "Current project";
  await page.evaluate(() => {
    Object.defineProperty(document, "hidden", {
      configurable: true,
      value: false,
    });
    document.dispatchEvent(new Event("visibilitychange"));
  });
  await expect(
    dialog.getByLabel("Projects", { exact: true }).locator("option"),
  ).toHaveText(["Current project"]);
  await expect
    .poll(() =>
      page.evaluate(
        () =>
          typeof (window as unknown as { finishGrant?: () => void })
            .finishGrant,
      ),
    )
    .toBe("function");
  await dialog.getByRole("button", { name: "Save grant", exact: true }).click();
  expect(data.writes).toBe(0);
  await expect(dialog.getByRole("alert")).toContainText("stale");
  await expect(profiles.locator("option:checked")).toHaveText(
    "interactive-shared-5",
  );
  await expect(
    dialog.getByLabel("Reason for access", { exact: false }),
  ).toHaveValue("Preserved reason");
  await page.evaluate(() =>
    (window as unknown as { finishGrant: () => void }).finishGrant(),
  );
  await expect(page.getByTestId("admin-data-freshness")).toContainText(
    "Data is current",
  );
  await dialog.getByRole("button", { name: "Save grant", exact: true }).click();
  await expect(dialog).toHaveCount(0);
  expect(data.writes).toBe(1);
});
