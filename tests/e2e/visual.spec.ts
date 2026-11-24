import { expect, test, type APIRequestContext, type Page } from "./fixtures";
import { spawn, type ChildProcess } from "node:child_process";
import { mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { createServer } from "node:net";
import rawSample from "../../contracts/sample.json";

test.describe.configure({ mode: "serial" });
let processHandle: ChildProcess;
let directory = "";
let origin = "";
let meetingId = "";

test.beforeAll(async ({ request }) => {
  directory = await mkdtemp(join(tmpdir(), "round-robin-visual-e2e-"));
  const probe = createServer();
  await new Promise<void>(resolve => probe.listen(0, "127.0.0.1", resolve));
  const port = (probe.address() as { port: number }).port;
  await new Promise<void>(resolve => probe.close(() => resolve()));
  origin = `http://127.0.0.1:${port}`;
  processHandle = spawn("uv", ["run", "python", "-m", "uvicorn", "visual_api:app", "--app-dir", "tests/e2e", "--host", "127.0.0.1", "--port", String(port)], {
    cwd: process.cwd(), stdio: "pipe", detached: true, env: { ...process.env,
      PYTHONPATH: "backend/src:.", ROUND_ROBIN_ENV: "development", ROUND_ROBIN_EXECUTION_MODE: "simulated",
      ROUND_ROBIN_PAID_ACCESS: "0", ROUND_ROBIN_IMAGE_CONFIG_FILE: "", ROUND_ROBIN_VISUAL_ENABLED: "1",
      ROUND_ROBIN_NATIVE_DIAGRAM_ENABLED: "1", ROUND_ROBIN_NATIVE_INTERFACE_ENABLED: "0",
      ROUND_ROBIN_VISUAL_TEST_DIRECTORY: directory, ROUND_ROBIN_DATABASE_URL: `sqlite:///${directory}/test.sqlite`,
      OPENAI_API_KEY: "", ANTHROPIC_API_KEY: "", GEMINI_API_KEY: "", GOOGLE_API_KEY: "",
      BRAVE_SEARCH_API_KEY: "",
    },
  });
  await expect.poll(async () => { try { return (await request.get(`${origin}/health`)).ok(); } catch { return false; } }, { timeout: 30_000 }).toBe(true);
  const created = await request.post(`${origin}/api/meetings`, { data: { command_id: crypto.randomUUID(), brief: { ...rawSample.brief, title: "Synthetic visual browser checks" } } });
  expect(created.ok()).toBe(true);
  meetingId = (await created.json()).id;
});

test.afterAll(async ({ request }) => {
  if (meetingId) await request.delete(`${origin}/api/meetings/${meetingId}`, { data: { command_id: crypto.randomUUID() } }).catch(() => {});
  if (processHandle?.pid) {
    const group = -processHandle.pid;
    try { process.kill(group, "SIGTERM"); } catch { /* Already stopped. */ }
    const deadline = Date.now() + 5000;
    let alive = true;
    while (alive && Date.now() < deadline) {
      try { process.kill(group, 0); } catch { alive = false; }
      if (alive) await new Promise(resolve => setTimeout(resolve, 50));
    }
    if (alive) { try { process.kill(group, "SIGKILL"); } catch { /* The owned group exited before the signal. */ } }
  }
  if (directory) await rm(directory, { recursive: true, force: true });
});

test.beforeEach(async ({ page }) => {
  await page.route("**/*", route => {
    const url = new URL(route.request().url());
    if (!["127.0.0.1", "localhost"].includes(url.hostname)) throw new Error("Visual browser check attempted an external request");
    return route.continue();
  });
  await page.route(`${process.env.ROUND_ROBIN_E2E_API_URL ?? "http://127.0.0.1:8000"}/**`, async route => {
    const target = new URL(route.request().url());
    const upstream = origin + target.pathname + target.search;
    const response = await route.fetch({ url: upstream });
    return route.fulfill({ response });
  });
});

async function createBrief(page: Page, domain: string) {
  await page.goto("/design");
  await expect(page.getByRole("heading", { name: "Freeze the purpose and scope.", exact: true })).toBeVisible();
  await page.getByRole("combobox", { name: "Saved comparison", exact: true }).selectOption(meetingId);
  await page.getByRole("combobox", { name: "Mode", exact: true }).selectOption(domain === "raster_concept" ? "review_only" : "improve");
  await page.getByRole("combobox", { name: "Output domain", exact: true }).selectOption(domain);
  await page.getByRole("textbox", { name: "Purpose", exact: true }).fill("Synthetic browser visual review fixture");
  await page.getByRole("textbox", { name: "Audience", exact: true }).fill("Browser test reviewer");
  await page.getByRole("button", { name: "Save frozen visual brief", exact: true }).click();
  await expect(page.getByRole("heading", { name: "Synthetic browser visual review fixture", exact: true })).toBeVisible();
}

test("canonical original remains honestly unreviewed, selectable and bounded behind preflight", async ({ page, request }) => {
  await createBrief(page, "raster_concept");
  const file = await request.get(`${origin}/_synthetic_raster`);
  await page.getByLabel("Upload original PNG or JPEG", { exact: true }).setInputFiles({ name: "synthetic.png", mimeType: "image/png", buffer: await file.body() });
  await expect(page.getByRole("heading", { name: "V1", exact: true })).toBeVisible();
  await expect(page.getByText("Independent review incomplete", { exact: true })).toBeVisible();
  await expect(page.getByText(/No actual critic review is saved/)).toBeVisible();
  await page.getByRole("button", { name: "Keep V1 with incomplete review", exact: true }).click();
  await expect(page.getByText(/Selected V1/)).toBeVisible();
  await page.getByRole("button", { name: "Review visual preflight", exact: true }).click();
  await expect(page.getByRole("heading", { name: "Confirm the exact visual work.", exact: true })).toBeVisible();
  await expect(page.getByRole("button", { name: "Start bounded visual review", exact: true })).toBeDisabled();
  await page.getByRole("button", { name: "Decline all candidates", exact: true }).click();
  await expect(page.getByText(/All candidates declined/)).toBeVisible();
  // Reopening must read the saved revision and pixels, rather than the history
  // card's initial zero-version snapshot from before upload and selection.
  await page.getByRole("button", { name: "New visual brief", exact: true }).click();
  await page.getByRole("button", { name: /Synthetic browser visual review fixture · 1 versions/ }).click();
  await expect(page.getByRole("heading", { name: "V1", exact: true })).toBeVisible();
  await expect(page.getByText(/All candidates declined/)).toBeVisible();
  await page.getByRole("button", { name: "Keep V1 with incomplete review", exact: true }).click();
  await expect(page.getByText(/Selected V1/)).toBeVisible();
  expect(await page.evaluate(() => JSON.stringify({ ...sessionStorage }))).not.toContain("Synthetic browser visual review fixture");
});

test("structured native source produces immutable editable pixels without an image provider", async ({ page }) => {
  await createBrief(page, "structured_diagram");
  await page.getByText("Render the exact native source", { exact: true }).click();
  await page.getByRole("button", { name: "Render a saved native candidate", exact: true }).click();
  await expect(page.getByRole("heading", { name: "V1", exact: true })).toBeVisible();
  await expect(page.getByText("native diagram", { exact: false })).toBeVisible();
  const download = page.waitForEvent("download");
  await page.getByRole("button", { name: "Download editable source", exact: true }).click();
  expect((await download).suggestedFilename()).toMatch(/\.svg$/);
  await expect(page.getByText("Independent review incomplete", { exact: true })).toBeVisible();
  await page.setViewportSize({ width: 390, height: 844 });
  expect(await page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth)).toBe(false);
});
