import { test, expect, type Page } from "@playwright/test";

async function pendingWorkspace(page: Page) {
  const rows = ["ws-a", "ws-b"].map((id) => ({
    id,
    course_id: "c",
    group_id: id,
    source: "local",
    state: "stopped",
    profile: "interactive-shared-5",
  }));
  const writes: string[] = [];
  let finishStart!: (result: { status: number; detail?: string }) => void;
  let finishStop!: (result: { status: number; detail?: string }) => void;
  const start = new Promise<{ status: number; detail?: string }>((resolve) => {
    finishStart = resolve;
  });
  const stop = new Promise<{ status: number; detail?: string }>((resolve) => {
    finishStop = resolve;
  });
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
      const request = route.request(),
        path = new URL(request.url()).pathname;
      if (request.method() === "POST") {
        writes.push(path);
        const result = await (path.endsWith("/start") ? start : stop);
        if (path.endsWith("/stop") && result.status === 200)
          rows[0].state = "stopped";
        return route.fulfill({
          status: result.status,
          json:
            result.status === 200
              ? { workspace: rows[0] }
              : { detail: result.detail },
        });
      }
      return route.fulfill({
        json: { records: path === "/api/local/workspaces" ? rows : [] },
      });
    },
  );
  await page.goto("/workspaces");
  const a = page
    .getByRole("row")
    .filter({ has: page.getByRole("cell", { name: "ws-a", exact: true }) });
  const b = page
    .getByRole("row")
    .filter({ has: page.getByRole("cell", { name: "ws-b", exact: true }) });
  await a.getByRole("button", { name: "Start", exact: true }).click();
  await page.getByRole("button", { name: "Confirm", exact: true }).click();
  await expect.poll(() => writes.length).toBe(1);
  return { a, b, writes, finishStart, finishStop };
}

for (const first of ["start", "stop"])
  test(`pending Start allows only its Stop and keeps busy until both finish (${first} first)`, async ({
    page,
  }) => {
    const h = await pendingWorkspace(page);
    await expect(
      page.getByRole("dialog", { name: "Confirm action" }),
    ).toHaveCount(0);
    await expect(
      h.a.getByRole("button", { name: "Stop", exact: true }),
    ).toBeEnabled();
    await expect(
      h.b.getByRole("button", { name: "Stop", exact: true }),
    ).toBeDisabled();
    await expect(
      h.a.getByRole("button", { name: "Start", exact: true }),
    ).toBeDisabled();
    await h.a.getByRole("button", { name: "Stop", exact: true }).click();
    await expect.poll(() => h.writes.length).toBe(2);
    await expect(
      h.a.getByRole("button", { name: "Stop", exact: true }),
    ).toBeDisabled();
    const cancel = {
      status: 409,
      detail: "Workspace start cancelled; observed shutdown required",
    };
    if (first === "start") h.finishStart(cancel);
    else h.finishStop({ status: 200 });
    await page.waitForTimeout(50);
    if (first === "stop") {
      await expect(
        h.a.getByRole("button", { name: "Stop", exact: true }),
      ).toBeDisabled();
    }
    await expect(
      h.b.getByRole("button", { name: "Start", exact: true }),
    ).toBeDisabled();
    if (first === "start") h.finishStop({ status: 200 });
    else h.finishStart(cancel);
    await expect(
      h.b.getByRole("button", { name: "Start", exact: true }),
    ).toBeEnabled();
    await expect(page.getByRole("alert")).toHaveCount(0);
    await expect(
      page.getByText("Workspace stopped.", { exact: true }),
    ).toBeVisible();
    expect(h.writes).toEqual([
      "/api/workspaces/ws-a/start",
      "/api/workspaces/ws-a/stop",
    ]);
  });

test("unrelated Start conflict stays visible after a requested Stop", async ({
  page,
}) => {
  const h = await pendingWorkspace(page);
  await h.a.getByRole("button", { name: "Stop", exact: true }).click();
  await expect.poll(() => h.writes.length).toBe(2);
  h.finishStop({ status: 200 });
  h.finishStart({ status: 409, detail: "Native cleanup remains unresolved" });
  await expect(page.getByRole("alert")).toContainText(
    "Native cleanup remains unresolved",
  );
});

test("failed Stop remains visible after intentional Start cancellation", async ({
  page,
}) => {
  const h = await pendingWorkspace(page);
  await h.a.getByRole("button", { name: "Stop", exact: true }).click();
  await expect.poll(() => h.writes.length).toBe(2);
  h.finishStart({
    status: 409,
    detail: "Workspace start cancelled; observed shutdown required",
  });
  h.finishStop({
    status: 409,
    detail: "Hub shutdown not confirmed; GPU reservations retained",
  });
  await expect(page.getByRole("alert")).toContainText(
    "GPU reservations retained",
  );
  await expect(
    page.getByText("Workspace stopped.", { exact: true }),
  ).toHaveCount(0);
});
