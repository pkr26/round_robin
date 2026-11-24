import { expect, test, type APIRequestContext } from "./fixtures";
import rawSample from "../../contracts/sample.json";

const API = process.env.ROUND_ROBIN_E2E_API_URL ?? "http://127.0.0.1:8000";
let ownedMeeting: string | undefined;
test.setTimeout(120_000);

test.beforeEach(async ({ page, request }) => {
  const endpoint = new URL(API);
  expect(["127.0.0.1", "localhost"]).toContain(endpoint.hostname);
  expect(endpoint.protocol).toBe("http:");
  const health = await request.get(`${API}/health`);
  expect(health.ok()).toBe(true);
  expect((await health.json()).mode).toBe("simulated");
  await page.route("**/*", route => {
    const target = new URL(route.request().url());
    if (!["127.0.0.1", "localhost"].includes(target.hostname)) throw new Error("Connected reuse test refused an external request");
    return route.continue();
  });
});

test.afterEach(async ({ request }) => {
  if (!ownedMeeting) return;
  const response = await request.delete(`${API}/api/meetings/${ownedMeeting}`, { data: { command_id: crypto.randomUUID() } });
  expect(response.ok(), "Delete only the comparison created by this test").toBe(true);
  ownedMeeting = undefined;
});

async function get(request: APIRequestContext, path: string) {
  const response = await request.get(`${API}${path}`);
  expect(response.ok(), await response.text()).toBe(true);
  return response.json();
}

async function post(request: APIRequestContext, path: string, payload: object) {
  const response = await request.post(`${API}${path}`, { data: { ...payload, command_id: crypto.randomUUID() } });
  expect(response.ok(), await response.text()).toBe(true);
  return response.json();
}

async function completed(request: APIRequestContext, runId: string) {
  await expect.poll(async () => {
    const snapshot = await get(request, `/api/runs/${runId}/snapshot`);
    return ["completed", "completed_with_limitations"].includes(snapshot.run.status) && !!snapshot.report;
  }, { timeout: 60_000, intervals: [150, 300, 500] }).toBe(true);
  return get(request, `/api/runs/${runId}/snapshot`);
}

test("explicit parent evidence selection crosses the real browser, preflight and worker boundary without implicit imports", async ({ page, request }) => {
  const title = `Connected explicit reuse ${crypto.randomUUID()}`;
  const meeting = await post(request, "/api/meetings", { brief: { ...rawSample.brief, title } });
  ownedMeeting = meeting.id;
  const quote = await post(request, `/api/meetings/${meeting.id}/preflight`, { expected_draft_revision: meeting.draft_revision, mode: "simulated" });
  expect(quote.blockers).toEqual([]);
  const parent = await post(request, `/api/meetings/${meeting.id}/runs`, { expected_draft_revision: meeting.draft_revision, preflight_id: quote.id });
  const original = await completed(request, parent.id);
  const parentSources = (await get(request, `/api/runs/${parent.id}/sources`)).items;
  expect(parentSources.length).toBeGreaterThan(1);
  const selected = parentSources[0];
  expect(selected.time_sensitive).toBe(true);
  expect(selected.verification_status).toBe("user_supplied");

  await page.goto("/live");
  await expect(page.getByText("Local service connected", { exact: true })).toBeVisible();
  await page.getByRole("navigation", { name: "Saved comparisons" }).getByRole("button").filter({ hasText: title }).click();
  await expect(page.getByRole("button", { name: "Export JSON", exact: true })).toBeVisible();
  await page.getByRole("button", { name: "Create a linked follow-up", exact: true }).click();
  await page.getByLabel("Common cost horizon (months)", { exact: true }).fill("24");
  await page.getByRole("button", { name: "Save linked follow-up draft", exact: true }).click();
  await expect(page.getByText(/Follow-up draft saved/)).toBeVisible();
  await page.getByRole("button", { name: "Review preflight", exact: true }).click();
  await expect(page.getByRole("heading", { name: "Choose earlier evidence and claims to reuse" })).toBeVisible();
  const candidates = page.getByRole("checkbox", { name: /^Evidence ·/ });
  await expect(candidates).toHaveCount(parentSources.length);
  for (const candidate of await candidates.all()) await expect(candidate).not.toBeChecked();
  await expect(page.getByText("0 saved parent items are included in this displayed preflight.", { exact: true })).toBeVisible();
  await candidates.first().check();
  await expect(page.getByRole("button", { name: "Start simulated review", exact: true })).toBeDisabled();
  const refreshedResponse = page.waitForResponse(response => response.request().method() === "POST" && new URL(response.url()).pathname === `/api/meetings/${meeting.id}/preflight`);
  await page.getByRole("button", { name: "Refresh preflight with selected parent items", exact: true }).click();
  const refreshed = await (await refreshedResponse).json();
  expect(refreshed.parent_run_id).toBe(parent.id);
  expect(refreshed.parent_replacement).toBeNull();
  expect(refreshed.reuse_manifest).toEqual([expect.objectContaining({ kind: "evidence", id: selected.id, version: 1, sha256: selected.sha256 })]);
  await expect(page.getByText("1 saved parent items are included in this displayed preflight.", { exact: true })).toBeVisible();
  await page.getByRole("checkbox", { name: /I understand the input scope/ }).check();
  const startedResponse = page.waitForResponse(response => response.request().method() === "POST" && new URL(response.url()).pathname === `/api/meetings/${meeting.id}/runs`);
  await page.getByRole("button", { name: "Start simulated review", exact: true }).click();
  const started = await startedResponse;
  expect(started.ok()).toBe(true);
  expect(started.request().postDataJSON()).not.toHaveProperty("replace_parent_run_version");
  const child = await started.json();
  const result = await completed(request, child.id);
  await expect(page.getByRole("button", { name: "Export JSON", exact: true })).toBeVisible();
  expect(result.run.parent_run_id).toBe(parent.id);
  expect(result.brief.horizon_months).toBe(24);
  expect(result.run.execution_mode).toBe("simulated");

  const childSources = (await get(request, `/api/runs/${child.id}/sources`)).items;
  const parentOrigins = new Set(parentSources.map((source: { origin_group: string }) => source.origin_group));
  // Fresh child inputs may contain the same entered facts. Only imported
  // records retain a parent's origin group, timestamp and downgraded status.
  const imported = childSources.filter((source: { origin_group: string }) => parentOrigins.has(source.origin_group));
  expect(imported).toHaveLength(1);
  expect(imported[0]).toMatchObject({ run_id: child.id, sha256: selected.sha256, text: selected.text, verified_at: selected.verified_at, origin_group: selected.origin_group, verification_status: "not_checked", time_sensitive: true });
  expect(imported[0].id).not.toBe(selected.id);
  expect(imported[0]).not.toHaveProperty("original_alias");
  const parentIds = new Set(parentSources.map((source: { id: string }) => source.id));
  expect(childSources.some((source: { id: string }) => parentIds.has(source.id))).toBe(false);
  const preserved = await get(request, `/api/runs/${parent.id}/snapshot`);
  expect(preserved.run).toEqual(original.run);
  expect(preserved.brief).toEqual(original.brief);
  expect(preserved.report).toEqual(original.report);
  expect(preserved.evidence).toEqual(original.evidence);
  expect((await get(request, `/api/meetings/${meeting.id}`)).runs).toHaveLength(2);
});
