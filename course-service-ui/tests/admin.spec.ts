import { test, expect } from "@playwright/test";
const initial = {
  courses: [
    {
      id: "ml",
      name: "Machine Learning",
      source: "local",
      resource_ceiling: { cpu: "2", memory: "4Gi", gpuMemoryGiB: 5 },
      workspace_bindings: [
        { group_id: "team-a", hub_user: "collab-a", hub_server: "shared" },
      ],
    },
  ],
  terms: [{ id: "ss26", course_id: "ml", name: "Summer 2026" }],
  memberships: [
    {
      id: "m-alice",
      course_id: "ml",
      term_id: "ss26",
      person_id: "alice",
      role: "student",
      group_id: "team-a",
    },
  ],
  groups: [
    {
      id: "team-a",
      course_id: "ml",
      term_id: "ss26",
      name: "Team A",
      members: ["alice"],
    },
  ],
  groupings: [],
  projects: [{ id: "research", name: "Research", source: "local" }],
  assignments: [
    {
      id: "hw1",
      course_id: "ml",
      term_id: "ss26",
      mode: "random",
      state: "open",
    },
  ],
  workspaces: [
    {
      id: "ws-a",
      course_id: "ml",
      term_id: "ss26",
      group_id: "team-a",
      profile: "cpu",
      state: "stopped",
    },
  ],
  audit: [
    {
      time: "2026-10-06T09:00:00Z",
      actor: "admin",
      kind: "courses",
      target: "ml",
      outcome: "success",
      current: { name: "Machine Learning" },
    },
  ],
};
let writes: { path: string; body: Record<string, unknown> }[];
test.beforeEach(async ({ page }) => {
  writes = [];
  const data = structuredClone(initial) as Record<
    string,
    Record<string, unknown>[]
  >;
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
        url = new URL(req.url()),
        kind = url.pathname.split("/").pop()!;
      if (req.method() === "GET")
        await route.fulfill({
          json:
            url.pathname === "/api/compute"
              ? url.searchParams.has("person")
                ? {
                    grants: [
                      {
                        id: "g1",
                        person: "person-1",
                        profiles: ["cpu"],
                        allowance: 1,
                        expires: "2026-12-01T00:00:00Z",
                        reason: "Teaching",
                      },
                    ],
                  }
                : {
                    profiles: {
                      cpu: {
                        name: "CPU",
                        cpu: "2",
                        memory: "4Gi",
                        enabled: true,
                      },
                    },
                  }
              : { records: data[kind] ?? [] },
        });
      else {
        const body = req.postDataJSON();
        writes.push({ path: url.pathname, body });
        if (req.method() === "POST" && url.pathname.startsWith("/api/local/")) {
          data[kind] = (data[kind] ?? []).filter((r) => r.id !== body.id);
          data[kind].push(body);
        }
        await route.fulfill({ json: {} });
      }
    },
  );
});
test("overview navigation and responsive layout", async ({ page }) => {
  await page.goto("/");
  await expect(
    page.getByRole("heading", { name: "Welcome, admin" }),
  ).toBeVisible();
  await expect(page.locator(".overview-card")).toHaveCount(6);
  await page.screenshot({
    path: "/tmp/cps-admin-ui-qa/overview-desktop.png",
    fullPage: true,
  });
  await page.setViewportSize({ width: 390, height: 844 });
  await page.screenshot({
    path: "/tmp/cps-admin-ui-qa/overview-mobile.png",
    fullPage: true,
  });
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBeTruthy();
});
test("create course without JSON and preserve immutable identifier on edit", async ({
  page,
}) => {
  await page.goto("/courses");
  await page
    .getByRole("button", { name: "Create course", exact: true })
    .click();
  await page.getByLabel("Identifier", { exact: false }).fill("robotics");
  await page.getByLabel(/^Name/).fill("Robotics");
  await page.getByRole("button", { name: "Save record" }).click();
  await expect(page.getByText("Courses saved successfully.")).toBeVisible();
  expect(writes[0].body).toEqual({ id: "robotics", name: "Robotics" });
  await page.getByRole("button", { name: "Edit Robotics" }).click();
  await expect(page.getByLabel("Identifier", { exact: false })).toBeDisabled();
  await page.keyboard.press("Escape");
  await expect(page.getByRole("dialog")).toHaveCount(0);
});
test("member form uses course and term selections", async ({ page }) => {
  await page.goto("/courses");
  await page.getByRole("button", { name: "Members", exact: true }).click();
  await page
    .getByRole("button", { name: "Create membership", exact: true })
    .click();
  await page.getByLabel("Identifier", { exact: false }).fill("bob-membership");
  await page
    .getByRole("dialog")
    .getByLabel(/^Course/)
    .selectOption("ml");
  await page.getByLabel(/^Term/).selectOption("ss26");
  await page.getByLabel("Hub username").fill("bob");
  await page.getByRole("button", { name: "Save record" }).click();
  await expect(page.getByText("Members saved successfully.")).toBeVisible();
  expect(writes[0].body).toMatchObject({
    person_id: "bob",
    course_id: "ml",
    term_id: "ss26",
    role: "student",
  });
});
test("compute profiles are readable and grant lookup reads grants", async ({
  page,
}) => {
  await page.goto("/compute");
  await expect(
    page.getByRole("cell", { name: "CPU", exact: true }),
  ).toBeVisible();
  await expect(page.getByRole("button", { name: "Use for grant" })).toHaveCount(
    0,
  );
  await page.getByLabel("Look up a person’s grants").fill("person-1");
  await page.getByRole("button", { name: "View grants" }).click();
  await expect(
    page.getByRole("cell", { name: "g1", exact: true }),
  ).toBeVisible();
});
test("manual assignment uses plain group lines and strips random-only fields", async ({
  page,
}) => {
  await page.goto("/assignments");
  await page
    .getByRole("button", { name: "Create assignment", exact: true })
    .click();
  await page.getByLabel("Identifier", { exact: false }).fill("hw2");
  await page
    .getByRole("dialog")
    .getByLabel(/^Course/)
    .selectOption("ml");
  await page.getByLabel(/^Term/).selectOption("ss26");
  await page.getByLabel("Allocation method").selectOption("manual");
  await page.getByLabel("Manual groups").fill("team-a: alice");
  await page.getByRole("button", { name: "Allocate groups" }).click();
  await expect(page.getByText("Assignments saved successfully.")).toBeVisible();
  expect(writes[0]).toEqual({
    path: "/api/assignments/allocate",
    body: {
      id: "hw2",
      course_id: "ml",
      term_id: "ss26",
      mode: "manual",
      rows: { "team-a": ["alice"] },
    },
  });
});
test("workspace creation derives approved binding and ceiling", async ({
  page,
}) => {
  await page.goto("/workspaces");
  await page
    .getByRole("button", { name: "Create workspace", exact: true })
    .click();
  await page.getByLabel("Identifier", { exact: false }).fill("ws-b");
  await page
    .getByRole("dialog")
    .getByLabel(/^Course/)
    .selectOption("ml");
  await page.getByLabel(/^Term/).selectOption("ss26");
  await page.getByLabel(/^Group/).selectOption("team-a");
  await page.getByLabel("Compute profile").selectOption("cpu");
  await page.getByRole("button", { name: "Save record" }).click();
  await expect(
    page.getByText("Shared workspaces saved successfully."),
  ).toBeVisible();
  expect(writes[0].body).toMatchObject({
    hub_user: "collab-a",
    hub_server: "shared",
    course_ceiling: initial.courses[0].resource_ceiling,
  });
});
test("member removal selects actual membership and warns about kernel interruption", async ({
  page,
}) => {
  await page.goto("/workspaces");
  await page
    .getByRole("button", { name: "Remove member", exact: true })
    .click();
  await expect(
    page.getByText(
      "This stops the shared kernel, revokes the member’s access and preserves files.",
    ),
  ).toBeVisible();
  await page.getByLabel("Course membership").selectOption("m-alice");
  await page
    .getByRole("dialog")
    .getByRole("button", { name: "Remove member" })
    .click();
  await expect(page.getByText("Member removal requested.")).toBeVisible();
  expect(writes[0]).toEqual({
    path: "/api/workspaces/ws-a/remove-member",
    body: { membership_id: "m-alice" },
  });
});
test("destructive deletion requires explicit confirmation", async ({
  page,
}) => {
  await page.goto("/courses");
  await page.getByRole("button", { name: "Remove Machine Learning" }).click();
  expect(writes).toHaveLength(0);
  await page.getByRole("button", { name: "Cancel", exact: true }).click();
  expect(writes).toHaveLength(0);
});
test("audit readable and integration remains disabled", async ({ page }) => {
  await page.goto("/audit");
  await expect(
    page.getByRole("cell", { name: "admin", exact: true }),
  ).toBeVisible();
  await page.getByText("Details", { exact: true }).click();
  await expect(page.getByText("name: Machine Learning")).toBeVisible();
  await page.goto("/integrations");
  await expect(page.getByText("Planned / Not configured")).toBeVisible();
});
test("backend errors remain visible and do not close editor", async ({
  page,
}) => {
  await page.route("**/api/local/courses", (route) =>
    route.request().method() === "POST"
      ? route.fulfill({
          status: 403,
          json: { detail: "Owned course scope required" },
        })
      : route.fulfill({ json: { records: [] } }),
  );
  await page.goto("/courses");
  await page
    .getByRole("button", { name: "Create course", exact: true })
    .click();
  await page.getByLabel("Identifier", { exact: false }).fill("denied");
  await page.getByLabel(/^Name/).fill("Denied");
  await page.getByRole("button", { name: "Save record" }).click();
  await expect(page.getByRole("dialog").getByRole("alert")).toHaveText(
    "Owned course scope required",
  );
  await expect(page.getByRole("dialog")).toBeVisible();
});

test("unapproved workspace binding blocks submission", async ({ page }) => {
  await page.route("**/api/local/courses", (route) =>
    route.fulfill({
      json: {
        records: [{ id: "ml", name: "Machine Learning", source: "local" }],
      },
    }),
  );
  await page.goto("/workspaces");
  await page
    .getByRole("button", { name: "Create workspace", exact: true })
    .click();
  await page.getByLabel("Identifier", { exact: false }).fill("ws-b");
  await page
    .getByRole("dialog")
    .getByLabel(/^Course/)
    .selectOption("ml");
  await page.getByLabel(/^Term/).selectOption("ss26");
  await page.getByLabel(/^Group/).selectOption("team-a");
  await page.getByLabel("Compute profile").selectOption("cpu");
  await page.getByRole("button", { name: "Save record" }).click();
  await expect(page.getByRole("dialog").getByRole("alert")).toContainText(
    "administrator-approved",
  );
  expect(writes).toHaveLength(0);
});
test("course view renders readable records and editor keyboard focus stays inside", async ({
  page,
}) => {
  await page.goto("/courses");
  await expect(
    page.getByRole("cell", { name: "Machine Learning", exact: true }),
  ).toBeVisible();
  await page.screenshot({
    path: "/tmp/cps-admin-ui-qa/courses-desktop.png",
    fullPage: true,
  });
  await page.getByRole("button", { name: "Edit Machine Learning" }).click();
  await expect(
    page.getByRole("button", { name: "Close editor" }),
  ).toBeFocused();
  await page.keyboard.press("Shift+Tab");
  await expect(page.getByRole("button", { name: "Save record" })).toBeFocused();
  await page.keyboard.press("Tab");
  await expect(
    page.getByRole("button", { name: "Close editor" }),
  ).toBeFocused();
});
test("external records expose no edit or remove controls", async ({ page }) => {
  await page.route("**/api/local/courses", (route) =>
    route.fulfill({
      json: {
        records: [
          { id: "official", name: "Official course", source: "external" },
        ],
      },
    }),
  );
  await page.goto("/courses");
  await expect(
    page.getByRole("cell", { name: "Official course", exact: true }),
  ).toBeVisible();
  await expect(
    page.getByRole("button", { name: "Edit Official course" }),
  ).toHaveCount(0);
  await expect(
    page.getByRole("button", { name: "Remove Official course" }),
  ).toHaveCount(0);
});
