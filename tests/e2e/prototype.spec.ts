import { expect, test, type Page } from "./fixtures";

test.beforeEach(async ({ page }) => {
  await page.route("**/*", (route) => {
    const host = new URL(route.request().url()).hostname;
    if (host !== "127.0.0.1" && host !== "localhost")
      throw new Error("Offline prototype attempted an external request.");
    return route.continue();
  });
});

async function begin(page: Page, scenario = "base12") {
  await page.getByRole("button", { name: "Edit brief", exact: true }).click();
  await page.getByLabel("Prepared fictional scenarios").selectOption(scenario);
  await page.getByRole("button", { name: "Review preflight" }).click();
  await expect(
    page.getByRole("button", { name: "Start simulated review" }),
  ).toBeDisabled();
  await page.getByRole("checkbox", { name: /I understand/ }).check();
  await page.getByRole("button", { name: "Start simulated review" }).click();
}

test("sample passages, exact calculations, keyboard dialog and immutable JSON export", async ({
  page,
}) => {
  const errors: string[] = [];
  page.on("pageerror", (error) => errors.push(error.message));
  await page.goto("/");
  await expect(
    page.getByRole("heading", { name: "Choose a workshop label system" }),
  ).toBeVisible();
  await expect(
    page.getByText("Fictional sample.", { exact: true }),
  ).toBeVisible();
  await page
    .getByRole("button", {
      name: "Inspect Option A · lower upfront cost calculation",
    })
    .click();
  await expect(page.getByRole("dialog")).toBeVisible();
  await expect(page.getByRole("dialog")).toContainText("1600.00");
  await page.keyboard.press("Escape");
  await expect(page.getByRole("dialog")).not.toBeVisible();
  await expect(
    page.getByRole("button", {
      name: "Inspect Option A · lower upfront cost calculation",
    }),
  ).toBeFocused();
  await page.getByRole("tab", { name: /Evidence/ }).click();
  await page
    .getByRole("button", { name: "Open exact passage & provenance" })
    .first()
    .click();
  await expect(page.getByRole("dialog")).toContainText("Passage SHA-256");
  await page.keyboard.press("Escape");
  const download = page.waitForEvent("download");
  await page.getByRole("button", { name: "Export JSON" }).click();
  const artifact = await download;
  expect(artifact.suggestedFilename()).toMatch(/^fictional-.*-v1\.json$/);
  const stream = await artifact.createReadStream();
  const chunks: Buffer[] = [];
  for await (const chunk of stream!) chunks.push(Buffer.from(chunk));
  const report = JSON.parse(Buffer.concat(chunks).toString());
  expect(report.fictional).toBe(true);
  expect(report.usage.request_count).toBe(0);
  expect(report.comparison[0].total).toBe("1600.00");
  expect(errors).toEqual([]);
});

test("custom draft saves without presenting an unrelated report and opt-in storage restores", async ({
  page,
}) => {
  await page.goto("/");
  await page.getByRole("button", { name: "Edit brief", exact: true }).click();
  await page.getByLabel("Budget (USD)").fill("1900");
  await page.getByRole("button", { name: "Review preflight" }).click();
  await expect(
    page.getByRole("alert").filter({ hasText: "Save the current draft" }),
  ).toBeVisible();
  await page
    .getByRole("checkbox", { name: /Keep this fictional draft/ })
    .check();
  await page.getByRole("button", { name: "Save draft", exact: true }).click();
  await page.getByRole("button", { name: "Review preflight" }).click();
  await expect(
    page.getByRole("alert").filter({ hasText: "no corresponding report" }),
  ).toBeVisible();
  await page.reload();
  await expect(page.getByLabel("Budget (USD)")).toHaveValue("1900");
  await page
    .getByRole("checkbox", { name: /Keep this fictional draft/ })
    .uncheck();
  expect(await page.evaluate(() => localStorage.length)).toBe(0);
});

test("changed horizon creates a linked walkthrough and preserves the original report", async ({
  page,
}) => {
  await page.goto("/");
  await page
    .getByRole("button", { name: "Explore a changed-input follow-up" })
    .click();
  await expect(page.getByLabel("Common cost horizon (months)")).toHaveValue(
    "24",
  );
  await page.getByRole("button", { name: "Review preflight" }).click();
  await page.getByRole("checkbox", { name: /I understand/ }).check();
  await page.getByRole("button", { name: "Start simulated review" }).click();
  await expect(
    page.getByRole("heading", {
      name: "Option B · lower recurring",
      exact: true,
    }),
  ).toBeVisible({ timeout: 10_000 });
  await expect(
    page.getByRole("button", { name: "Explore a changed-input follow-up" }),
  ).toHaveCount(0);
  await page.getByRole("button", { name: "Open original report" }).click();
  await expect(
    page.getByRole("heading", {
      name: "Option A · lower upfront",
      exact: true,
    }),
  ).toBeVisible();
});

test("unknown installation retains unknown total and conditional readiness", async ({
  page,
}) => {
  await page.goto("/");
  await begin(page, "missing_installation");
  await expect(
    page.getByRole("button", { name: "Conditional decision" }),
  ).toBeVisible({ timeout: 10_000 });
  const row = page
    .getByRole("row")
    .filter({ hasText: "Option B · lower recurring" });
  await expect(row).toContainText("Unknown");
  await expect(row).toContainText("$2,080.00");
  await expect(row).toContainText("unknown");
  await page.getByRole("tab", { name: /Evidence/ }).click();
  await expect(
    page.getByRole("heading", {
      name: "Fictional changed offer B",
      exact: true,
    }),
  ).toBeVisible();
  await expect(
    page.getByRole("heading", { name: "Fictional offer B", exact: true }),
  ).toHaveCount(0);
});

test("partial result does not invent a recommendation or readiness", async ({
  page,
}) => {
  await page.goto("/");
  await begin(page);
  await page
    .getByRole("button", { name: "Inspect a partial-result example" })
    .click();
  await expect(
    page.getByRole("heading", { name: "A choice is not established yet." }),
  ).toBeVisible();
  await expect(
    page.getByRole("button", { name: "Readiness unavailable" }),
  ).toBeVisible();
  await expect(
    page.getByText("completed with limitations", { exact: true }),
  ).toBeVisible();
});

test("cancellation acknowledgement is distinct from Stop not confirmed and preserves the selected horizon", async ({
  page,
}) => {
  await page.goto("/");
  await begin(page, "horizon24");
  await page
    .getByRole("button", { name: "Stop simulation", exact: true })
    .click();
  await expect(
    page.getByRole("button", { name: "Stop not confirmed", exact: true }),
  ).toBeDisabled();
  await expect(page.getByText("cancelled", { exact: true })).toBeVisible();
  await expect(
    page.getByRole("heading", { name: "A choice is not established yet." }),
  ).toBeVisible();
  const row = page
    .getByRole("row")
    .filter({ hasText: "Option A · lower upfront" });
  await expect(row).toContainText("$2,200.00");
  await page.getByRole("button", { name: "Decision overview" }).click();
  await expect(page.getByText("cancelled", { exact: true })).toBeVisible();
  await expect(
    page.getByRole("heading", { name: "A choice is not established yet." }),
  ).toBeVisible();
});

test("expired preflight cannot start a new simulated review", async ({
  page,
}) => {
  await page.clock.install();
  await page.goto("/");
  await page.getByRole("button", { name: "Edit brief", exact: true }).click();
  await page.getByRole("button", { name: "Review preflight" }).click();
  await page.getByRole("checkbox", { name: /I understand/ }).check();
  await page.clock.fastForward(601_000);
  await expect(
    page.getByRole("button", { name: "Start simulated review" }),
  ).toBeDisabled();
});

test("narrow viewport reflows and tabs support arrow-key navigation", async ({
  page,
}) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto("/");
  await expect(
    page.getByRole("tab", { name: "Overview", exact: true }),
  ).toBeEnabled();
  const overflow = await page.evaluate(
    () => document.documentElement.scrollWidth > window.innerWidth,
  );
  expect(overflow).toBe(false);
  await page.getByRole("tab", { name: "Overview", exact: true }).focus();
  await page.keyboard.press("ArrowRight");
  await expect(page.getByRole("tab", { name: /Evidence/ })).toBeFocused();
  await expect(page.getByRole("tabpanel")).toContainText(
    "Follow the fact back",
  );
});
