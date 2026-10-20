import { test, expect } from "@playwright/test";

test("profile image and resource fields retain labels, values and order", async ({
  page,
}) => {
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
      const path = new URL(route.request().url()).pathname;
      await route.fulfill({
        json:
          path === "/api/courses/ml/ss26"
            ? {
                course_id: "ml",
                course_name: "Machine Learning",
                term_id: "ss26",
                profiles: ["cpu"],
              }
            : path === "/api/profiles/ml/ss26/cpu"
              ? {
                  name: "cpu",
                  display_name: "Teaching CPU",
                  runtime: {
                    image: {
                      name: "teaching/notebook",
                      tag: "2026",
                      pullPolicy: "IfNotPresent",
                    },
                    resources: {
                      cpu_guarantee: 0,
                      cpu_limit: 2,
                      mem_guarantee: "1Gi",
                      mem_limit: "4Gi",
                    },
                    environment: {},
                  },
                }
              : {},
      });
    },
  );
  await page.goto("/course/ml/ss26/profiles");
  await expect(
    page.getByRole("heading", { name: "Teaching CPU" }),
  ).toBeVisible();
  await expect(page.getByText("Profile Name:", { exact: true })).toBeVisible();
  await page.getByRole("button", { name: "Runtime Configuration" }).click();
  const fields = page.locator('[data-state="open"][role="region"] .grid > div');
  await expect(fields).toHaveText([
    "Name:teaching/notebook",
    "Tag:2026",
    "Pull Policy:IfNotPresent",
    "CPU Guarantee:0",
    "CPU Limit:2",
    "Memory Guarantee:1Gi",
    "Memory Limit:4Gi",
  ]);
});
