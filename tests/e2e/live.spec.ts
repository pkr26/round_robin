import { expect, test, type APIRequestContext, type Page } from "./fixtures";
import rawSample from "../../contracts/sample.json";

const API = process.env.ROUND_ROBIN_E2E_API_URL ?? "http://127.0.0.1:8000";
const created = new Set<string>();
test.describe.configure({ mode: "serial" });
test.setTimeout(90_000);

test.beforeEach(async ({ page, request }) => {
  const health = await request.get(`${API}/health`);
  expect(health.ok(), "The disposable local API must be running for the connected application checks").toBe(true);
  await page.route("**/*", route => {
    const url = new URL(route.request().url());
    if (!["127.0.0.1", "localhost"].includes(url.hostname)) throw new Error("Application check attempted an external request");
    return route.continue();
  });
});

test.afterEach(async ({ request }) => {
  for (const id of created) {
    await request.delete(`${API}/api/meetings/${id}`, { data: { command_id: crypto.randomUUID() } });
  }
  created.clear();
});

async function draft(page: Page, suffix: string, unknownInstallation = false) {
  await page.goto("/live");
  await expect(page.getByText(/^(Local service|Workspace) connected$/)).toBeVisible();
  await page.getByRole("button", { name: "New comparison" }).click();
  const title = `Browser verification ${suffix} ${crypto.randomUUID().slice(0, 8)}`;
  await page.getByLabel("Comparison title", { exact: true }).fill(title);
  await page.getByRole("textbox", { name: "The decision you need to make", exact: true }).fill("Which of these two label systems fits our cost horizon and budget?");
  await page.getByLabel("What a good result must achieve", { exact: true }).fill("Replace the current handwritten label process with one of the candidate systems.");
  await page.getByLabel("Budget (USD)", { exact: true }).fill("2500");
  await page.getByLabel("Common cost horizon (months)", { exact: true }).fill("12");
  for (const [label, upfront, monthly] of [["Candidate 1", "1000", "50"], ["Candidate 2", "1600", "20"], ["Status quo", "0", "0"]]) {
    const group = page.getByRole("group", { name: label, exact: true });
    await group.getByLabel("upfront cost (USD)", { exact: true }).fill(upfront);
    await group.getByLabel("Monthly recurring cost (USD)", { exact: true }).fill(monthly);
    if (!(unknownInstallation && label === "Candidate 2")) await group.getByLabel("installation cost (USD)", { exact: true }).fill("0");
    await group.getByLabel("Monthly maintenance cost (USD)", { exact: true }).fill("0");
    await group.getByLabel("Tax and delivery cost (USD)", { exact: true }).fill("0");
  }
  await page.getByRole("textbox", { name: "Selected supporting text", exact: true }).fill("Browser-authored test terms: Option A costs 1000 USD upfront plus 50 USD monthly; B costs 1600 USD upfront plus 20 USD monthly. Installation is explicitly zero unless the field is left unknown. Verify actual supplier terms before acting.");
  const response = page.waitForResponse(value => value.request().method() === "POST" && new URL(value.url()).pathname === "/api/meetings");
  await page.getByRole("button", { name: "Save draft", exact: true }).click();
  const result = await (await response).json();
  expect(result.id).toMatch(/^meeting_/);
  created.add(result.id);
  await expect(page.getByText("Saved revision 1", { exact: true })).toBeVisible();
  return result as { id: string; draft_revision: number; draft: typeof rawSample.brief };
}

async function preflight(page: Page) {
  await page.getByRole("button", { name: "Review preflight", exact: true }).click();
  await expect(page.getByRole("heading", { name: "Check the recipients, input and allowance." })).toBeVisible();
  await expect(page.getByRole("button", { name: "Start simulated review", exact: true })).toBeDisabled();
  await page.getByRole("checkbox", { name: /I understand the input scope/ }).check();
}

async function start(page: Page) {
  const response = page.waitForResponse(value => value.request().method() === "POST" && /\/api\/meetings\/[^/]+\/runs$/.test(new URL(value.url()).pathname));
  await page.getByRole("button", { name: "Start simulated review", exact: true }).click();
  const result = await (await response).json();
  expect(result.id).toMatch(/^run_/);
  return result as { id: string; meeting_id: string };
}

async function report(page: Page) {
  await expect(page.getByRole("button", { name: "Export JSON", exact: true })).toBeVisible({ timeout: 60_000 });
  await page.getByRole("tab", { name: "Overview", exact: true }).click();
  await expect(page.getByRole("heading", { name: "Compare the approaches", exact: true })).toBeVisible();
}

async function post(request: APIRequestContext, path: string, body: object) {
  const response = await request.post(`${API}${path}`, { data: { ...body, command_id: crypto.randomUUID() } });
  expect(response.ok(), await response.text()).toBe(true);
  return response.json();
}

test("server-saved comparison reopens, inspects exact evidence and exports its saved version", async ({ page, request }) => {
  const failures: string[] = [];
  page.on("pageerror", error => failures.push(error.message));
  const saved = await draft(page, "saved comparison");
  await preflight(page);
  const run = await start(page);
  await report(page);
  const before = await (await request.get(`${API}/api/runs/${run.id}/snapshot`)).json();
  expect(before.run.execution_mode).toBe("simulated");
  await page.getByRole("button", { name: "Inspect Option A calculation", exact: true }).click();
  await expect(page.getByRole("dialog")).toBeVisible();
  await expect(page.getByRole("dialog")).toContainText("1600");
  await page.keyboard.press("Escape");
  await expect(page.getByRole("button", { name: "Inspect Option A calculation", exact: true })).toBeFocused();
  await page.getByRole("tab", { name: "Evidence", exact: true }).click();
  await page.getByRole("button", { name: "Open exact passage & provenance", exact: true }).first().click();
  await expect(page.getByRole("dialog")).toContainText("Passage SHA-256");
  await page.keyboard.press("Escape");
  const downloadPromise = page.waitForEvent("download");
  await page.getByRole("button", { name: "Export JSON", exact: true }).click();
  const artifact = await downloadPromise;
  const stream = await artifact.createReadStream();
  const chunks: Buffer[] = [];
  for await (const chunk of stream!) chunks.push(Buffer.from(chunk));
  const exported = JSON.parse(Buffer.concat(chunks).toString());
  expect(exported.id ?? exported.report?.id).toBe(before.report.id);
  expect(exported.version ?? exported.report?.version).toBe(before.report.version);
  await page.reload();
  await report(page);
  const reopened = await (await request.get(`${API}/api/meetings/${saved.id}`)).json();
  expect(reopened.runs).toHaveLength(1);
  await page.getByRole("combobox", { name: "Chosen approach", exact: true }).selectOption(before.report.comparison[0].option_id);
  await page.getByLabel("Why you chose it", { exact: true }).fill("A useful reversible test choice.");
  await page.getByRole("button", { name: "Save my choice", exact: true }).click();
  await expect(page.getByText("Your choice is saved separately from the recommendation.", { exact: true })).toBeVisible();
  const after = await (await request.get(`${API}/api/runs/${run.id}/snapshot`)).json();
  expect(after.report).toEqual(before.report);
  expect(failures).toEqual([]);
});

test("lost Start response retains its UUID and reconciles without a second review", async ({ page, request }) => {
  const saved = await draft(page, "lost response");
  await preflight(page);
  const commandIds: string[] = [];
  await page.route(`**/api/meetings/${saved.id}/runs`, async route => {
    if (route.request().method() !== "POST") return route.continue();
    commandIds.push(route.request().postDataJSON().command_id);
    await route.fetch();
    return route.abort("failed");
  });
  await page.getByRole("button", { name: "Start simulated review", exact: true }).click();
  await expect(page.getByText("Action not confirmed", { exact: true })).toBeVisible();
  await page.reload();
  await expect(page.getByText("Action not confirmed", { exact: true })).toBeVisible();
  await page.getByRole("button", { name: "Reconcile pending action", exact: true }).click();
  await expect(page.getByText("The original action has been reconciled with saved server state.", { exact: true })).toBeVisible();
  await report(page);
  const current = await (await request.get(`${API}/api/meetings/${saved.id}`)).json();
  expect(current.runs).toHaveLength(1);
  expect(commandIds).toHaveLength(1);
  expect(commandIds[0]).toMatch(/^[0-9a-f-]{36}$/);
});

test("changed-input follow-up preserves the original report", async ({ page, request }) => {
  const saved = await draft(page, "follow-up");
  await preflight(page);
  const original = await start(page);
  await report(page);
  const originalSnapshot = await (await request.get(`${API}/api/runs/${original.id}/snapshot`)).json();
  await page.getByRole("button", { name: "Create a linked follow-up", exact: true }).click();
  await page.getByLabel("Common cost horizon (months)", { exact: true }).fill("24");
  await page.getByRole("button", { name: "Save linked follow-up draft", exact: true }).click();
  await expect(page.getByText(/Follow-up draft saved/)).toBeVisible();
  await preflight(page);
  const followup = await start(page);
  await report(page);
  const later = await (await request.get(`${API}/api/runs/${followup.id}/snapshot`)).json();
  expect(later.run.parent_run_id).toBe(original.id);
  expect(later.brief.horizon_months).toBe(24);
  const preserved = await (await request.get(`${API}/api/runs/${original.id}/snapshot`)).json();
  expect(preserved.report).toEqual(originalSnapshot.report);
  const meeting = await (await request.get(`${API}/api/meetings/${saved.id}`)).json();
  expect(meeting.runs).toHaveLength(2);
});

test("unknown cost stays unknown and narrow layout remains keyboard operable", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await draft(page, "unknown amount", true);
  await preflight(page);
  await start(page);
  await report(page);
  const row = page.getByRole("row").filter({ hasText: "Option B" });
  await expect(row).toContainText("Unknown");
  expect(await page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth)).toBe(false);
  await page.getByRole("tab", { name: "Overview", exact: true }).focus();
  await page.keyboard.press("ArrowRight");
  await expect(page.getByRole("tab", { name: "Evidence", exact: true })).toBeFocused();
  await expect(page.getByRole("tabpanel")).toContainText("Follow each fact back");
});

test("Stop remains unconfirmed until acknowledgement and preserves saved partial work", async ({ page, request }) => {
  const brief = structuredClone(rawSample.brief);
  brief.title = `Browser verification awaiting-input ${crypto.randomUUID().slice(0, 8)}`;
  brief.options = [];
  const saved = await post(request, "/api/meetings", { brief });
  created.add(saved.id);
  const pre = await post(request, `/api/meetings/${saved.id}/preflight`, { expected_draft_revision: 1, mode: "simulated" });
  const run = await post(request, `/api/meetings/${saved.id}/runs`, { preflight_id: pre.id, expected_draft_revision: 1 });
  await expect.poll(async () => (await (await request.get(`${API}/api/runs/${run.id}`)).json()).status, { timeout: 45_000 }).toBe("awaiting_input");
  await page.addInitScript(({ meetingId, runId }) => sessionStorage.setItem("round-robin:current-local-comparison:v1", JSON.stringify({ meetingId, runId })), { meetingId: saved.id, runId: run.id });
  await page.goto("/live");
  await expect(page.getByRole("heading", { name: "Your input is needed before the review can continue." })).toBeVisible();
  await page.route(`**/api/runs/${run.id}/cancel`, async route => {
    const response = await route.fetch();
    await new Promise(resolve => setTimeout(resolve, 1200));
    await route.fulfill({ response });
  });
  await page.getByRole("button", { name: "Stop review", exact: true }).click();
  await expect(page.getByText("Stop not confirmed…", { exact: true })).toBeVisible();
  await expect(page.getByText(/Stop acknowledged by the local service/)).toBeVisible();
  await expect.poll(async () => (await (await request.get(`${API}/api/runs/${run.id}`)).json()).status, { timeout: 30_000 }).toBe("cancelled");
  await expect(page.getByRole("button", { name: "Export JSON", exact: true })).toBeVisible();
});

test("actual CORS text upload freezes only selected ready bytes in preflight", async ({ page, request }) => {
  const saved = await draft(page, "selected text upload");
  const text = "Browser-owned upload fixture. Option A installation is explicitly included at zero dollars.\n";
  await page.getByLabel("Upload selected text or Markdown", { exact: true }).setInputFiles({
    name: "browser-owned-quote.md", mimeType: "text/markdown", buffer: Buffer.from(text),
  });
  await expect(page.getByText(/Text uploaded and validated/)).toBeVisible();
  await page.getByRole("button", { name: "Save draft", exact: true }).click();
  await expect(page.getByText("Saved revision 2", { exact: true })).toBeVisible();
  const current = await (await request.get(`${API}/api/meetings/${saved.id}`)).json();
  expect(current.draft.selected_attachment_ids).toHaveLength(1);
  const response = page.waitForResponse(value => value.request().method() === "POST" && value.url().endsWith(`/api/meetings/${saved.id}/preflight`));
  await preflight(page);
  const frozen = await (await response).json();
  expect(frozen.input_manifest).toHaveLength(1);
  expect(frozen.input_manifest[0].id).toBe(current.draft.selected_attachment_ids[0]);
  expect(frozen.input_manifest[0].sha256).toMatch(/^[a-f0-9]{64}$/);
  expect(frozen.input_manifest[0].recipients.sort()).toEqual(["anthropic", "google", "openai"]);
  expect(frozen.approved_public_terms).toEqual([]);
});

test("debounced draft save preserves edits made while an older acknowledgement is delayed", async ({ page, request }) => {
  const saved = await draft(page, "autosave generation");
  let delayed = false;
  await page.route(`**/api/meetings/${saved.id}/draft`, async route => {
    if (route.request().method() !== "PATCH" || delayed) return route.continue();
    delayed = true;
    const response = await route.fetch();
    await new Promise(resolve => setTimeout(resolve, 1400));
    return route.fulfill({ response });
  });
  const firstSave = page.waitForRequest(value => value.method() === "PATCH" && value.url().endsWith(`/api/meetings/${saved.id}/draft`));
  await page.getByRole("textbox", { name: "Selected supporting text", exact: true }).fill("PRIVATE_SUPPLIER_AUTOSAVE_SENTINEL: use the exact supplied terms.");
  await firstSave;
  const newerQuestion = "Newer decision wording typed while the first save acknowledgement was delayed.";
  await page.getByRole("textbox", { name: "The decision you need to make", exact: true }).fill(newerQuestion);
  await expect(page.getByRole("textbox", { name: "The decision you need to make", exact: true })).toHaveValue(newerQuestion);
  await expect(page.getByText("Saved revision 3", { exact: true })).toBeVisible({ timeout: 15_000 });
  await expect(page.getByRole("textbox", { name: "The decision you need to make", exact: true })).toHaveValue(newerQuestion);
  const current = await (await request.get(`${API}/api/meetings/${saved.id}`)).json();
  expect(current.draft.question).toBe(newerQuestion);
  expect(current.runs).toEqual([]);
  const storage = await page.evaluate(() => JSON.stringify({ local: { ...localStorage }, session: { ...sessionStorage } }));
  expect(storage).not.toContain("PRIVATE_SUPPLIER_AUTOSAVE_SENTINEL");
  expect(storage).not.toContain(newerQuestion);
});

test("requirement and preference limits preserve entered lines and reserve budget and deadline slots", async ({ page, request }) => {
  const saved = await draft(page, "manual input bounds");
  const requirements = page.getByRole("textbox", { name: /^Hard requirements/ });
  const preferences = page.getByRole("textbox", { name: /^Preferences/ });
  const manualLines = Array.from({ length: 19 }, (_, index) => `Manual requirement ${index + 1}`);
  const preferenceLines = Array.from({ length: 11 }, (_, index) => `Preference ${index + 1}`);
  await page.getByLabel("Deadline", { exact: true }).fill("2027-01-31");
  await requirements.fill(manualLines.join("\n"));
  await preferences.fill(preferenceLines.join("\n"));
  await expect(page.getByText("Use at most 18 manual requirements; saved numeric checks, budget and deadline share the limit of 20.", { exact: true })).toBeVisible();
  await expect(page.getByText("Use at most 10 preferences. Your entered lines are retained.", { exact: true })).toBeVisible();
  await expect(requirements).toHaveValue(manualLines.join("\n"));
  await expect(preferences).toHaveValue(preferenceLines.join("\n"));
  await page.getByRole("button", { name: "Save draft", exact: true }).click();
  expect(await requirements.evaluate(element => (element as HTMLTextAreaElement).checkValidity())).toBe(false);
  const unchanged = await (await request.get(`${API}/api/meetings/${saved.id}`)).json();
  expect(unchanged.draft.hard_requirements).toHaveLength(1);
  expect(unchanged.draft.preferences).toEqual([]);
  await requirements.fill(manualLines.slice(0, 18).join("\n"));
  await preferences.fill(preferenceLines.slice(0, 10).join("\n"));
  const response = page.waitForResponse(value => value.request().method() === "PATCH" && value.url().endsWith(`/api/meetings/${saved.id}/draft`));
  await page.getByRole("button", { name: "Save draft", exact: true }).click();
  const committed = await (await response).json();
  expect(committed.brief.hard_requirements).toHaveLength(20);
  expect(committed.brief.hard_requirements.map((item: { description: string }) => item.description)).toContain("Manual requirement 18");
  expect(committed.brief.preferences).toHaveLength(10);
  expect(committed.brief.preferences[9].description).toBe("Preference 10");
});

test("retry controls use authoritative eligible stages instead of historical failure events", async ({ page, request }) => {
  await draft(page, "exact retry stages");
  await preflight(page);
  const run = await start(page);
  await report(page);
  const snapshot = await (await request.get(`${API}/api/runs/${run.id}/snapshot`)).json();
  const retrySnapshot = { ...snapshot,
    run: { ...snapshot.run, status: "completed_with_limitations" },
    retry_eligible: true, retryable_stages: ["audit"],
    events: [{ ...snapshot.events[0], event_type: "step.failed", payload: { stage: "synthesis", message: "Historical failure; this stage is no longer eligible." } }],
  };
  await page.route(`**/api/runs/${run.id}/snapshot`, route => route.fulfill({ json: retrySnapshot }));
  await page.reload();
  await expect(page.getByRole("button", { name: "Retry audit", exact: true })).toBeVisible();
  await expect(page.getByRole("button", { name: "Retry synthesis", exact: true })).toHaveCount(0);
  const sent = page.waitForRequest(value => value.method() === "POST" && value.url().endsWith(`/api/runs/${run.id}/retries`));
  await page.route(`**/api/runs/${run.id}/retries`, route => route.fulfill({ json: { ...snapshot.run, status: "queued", stage: "audit", version: snapshot.run.version + 1 } }));
  await page.getByRole("button", { name: "Retry audit", exact: true }).click();
  expect((await sent).postDataJSON().stage).toBe("audit");
});

test("unconfirmed private autosave retains a noncontent receipt and reconciles after reload", async ({ page, request }) => {
  const saved = await draft(page, "private pending receipt");
  let sentId = "";
  await page.route(`**/api/meetings/${saved.id}/draft`, async route => {
    if (route.request().method() !== "PATCH") return route.continue();
    sentId = route.request().postDataJSON().command_id;
    await route.fetch();
    return route.abort("failed");
  });
  await page.getByRole("textbox", { name: "Selected supporting text", exact: true }).fill("PRIVATE_SUPPLIER_PENDING_SENTINEL: supplier-only quote conditions.");
  await expect(page.getByText("Action not confirmed", { exact: true })).toBeVisible({ timeout: 15_000 });
  const retained = await page.evaluate(() => JSON.stringify({ local: { ...localStorage }, session: { ...sessionStorage } }));
  expect(retained).toContain(sentId);
  expect(retained).not.toContain("PRIVATE_SUPPLIER_PENDING_SENTINEL");
  expect(retained).not.toContain("supplier-only quote conditions");
  await page.reload();
  await expect(page.getByText("Action not confirmed", { exact: true })).toBeVisible();
  await page.getByRole("button", { name: "Reconcile pending action", exact: true }).click();
  await expect(page.getByText("The original action has been reconciled with saved server state.", { exact: true })).toBeVisible();
  const current = await (await request.get(`${API}/api/meetings/${saved.id}`)).json();
  expect(current.draft_revision).toBe(2);
  expect(current.runs).toEqual([]);
});

test("configured hosted session supplies in-memory CSRF and only exposes live review after operator health permits it", async ({ page }) => {
  await page.route("**/auth/session", route => route.fulfill({ json: { owner_id: "owner_local", profile: "invited_hosted", csrf_token: "synthetic-csrf-memory-only" } }));
  await page.route("**/health", route => route.fulfill({ json: { status: "ok", mode: "live", paid_access_configured: true } }));
  const createdRequest = page.waitForRequest(value => value.method() === "POST" && new URL(value.url()).pathname === "/api/meetings");
  await draft(page, "hosted csrf gating");
  expect((await createdRequest).headers()["x-csrf-token"]).toBe("synthetic-csrf-memory-only");
  const execution = page.getByRole("combobox", { name: "Review execution", exact: true });
  await expect(execution).toContainText("Live provider review");
  await execution.selectOption("live");
  await expect(page.getByText("Live review · explicit authorization required", { exact: true })).toBeVisible();
  await expect(page.getByRole("button", { name: "Sign out", exact: true })).toBeVisible();
  const storage = await page.evaluate(() => JSON.stringify({ local: { ...localStorage }, session: { ...sessionStorage } }));
  expect(storage).not.toContain("synthetic-csrf-memory-only");
});

test("expired hosted access offers sign-in without replaying any saved action", async ({ page }) => {
  const writes: string[] = [];
  page.on("request", request => { if (["POST", "PATCH", "PUT", "DELETE"].includes(request.method())) writes.push(request.url()); });
  await page.route("**/auth/session", route => route.fulfill({ status: 401, json: { error: { message: "Sign in required" } } }));
  await page.goto("/live");
  await expect(page.getByRole("link", { name: "Sign in to the invited workspace", exact: true })).toHaveAttribute("href", `${API}/auth/login`);
  expect(writes).toEqual([]);
});
