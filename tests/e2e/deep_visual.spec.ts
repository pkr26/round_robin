import { expect, test, type Page } from "./fixtures";
import rawSample from "../../contracts/sample.json";

const API = process.env.ROUND_ROBIN_E2E_API_URL ?? "http://127.0.0.1:8000";
const meeting = { id: "meeting_fixture", title: "Private comparison fixture" };
const diagram = JSON.stringify({ width: 500, height: 200, nodes: [{ id: "node_private", label: "PRIVATE_SOURCE_SENTINEL", x: 10, y: 10 }], edges: [] });
const capabilities = {
  enabled: true, paid_access_enabled: false, raster_formats: ["image/png", "image/jpeg"],
  maximum_upload_bytes: 10485760, maximum_pixels: 16000000, maximum_references: 4,
  versions: 3, image_submissions: 4, shared_inference_calls: 24, status_polls: 60,
  active_seconds: 1800, critics_required: 2, live_image_provider_configured: false,
  maximum_usd: "0", recipients: [], models: {}, generator_provider: null, generator_model: null,
  owner_storage_bytes: 268435456, deployment_storage_bytes: 1073741824,
  supported_sizes: [],
  native_diagram_enabled: true, native_interface_enabled: false,
  native_diagram_renderer: "structured_source_only", executable_interface_renderer: "disabled", limitations: [],
};
function design(id: string, purpose = "PRIVATE_DESIGN_SENTINEL") {
  const version = {
    id: `version_${id}`, version: 1, parent_version_id: null,
    canonical_hash: "a".repeat(64), original_hash: "b".repeat(64), width: 500, height: 200,
    generator_provider: null, source_kind: "native_diagram", provenance: {},
    image_url: `/api/design-versions/version_${id}/image`, reviews: [],
    coverage: { eligible_critic_providers: [], complete: false, generator_provenance: "unknown", limitations: [] },
  };
  return {
    id, meeting_id: meeting.id, mode: "improve", status: "active", revision: 2,
    brief: { purpose, audience: "Test", domain: "structured_diagram", width: 500, height: 200, required_text: [], locked_requirements: [] },
    recipients: [], selected_version_id: null, selection_recorded: false, versions: [version],
    state: { mode: "improve", maximum_usd: "0", admitted_at: 1000, request_count: 0, submission_count: 0,
      poll_count: 0, version_count: 1, intentional_edits: 0, renderer_attempts: 1, paid_admitted: false,
      fence: 0, epoch: 1, status: "active", step_calls: {}, operations: {} },
    pending_operations: [], operation_failures: [], paid_access_enabled: false, limitations: [],
  };
}

async function mockWorkspace(page: Page, designs = [design("design_one")]) {
  const writes: string[] = [];
  page.on("request", request => { if (["POST", "PATCH", "PUT", "DELETE"].includes(request.method())) writes.push(request.url()); });
  await page.route("**/*", route => {
    const url = new URL(route.request().url());
    if (!["127.0.0.1", "localhost"].includes(url.hostname)) throw new Error("Deep visual check attempted external I/O");
    return route.continue();
  });
  await page.route(`${API}/**`, route => {
    const path = new URL(route.request().url()).pathname;
    if (path === "/auth/session") return route.fulfill({ json: { owner_id: "owner_one", profile: "invited_hosted", csrf_token: "csrf_private_memory" } });
    if (path === "/api/visual/capabilities") return route.fulfill({ json: capabilities });
    if (path === "/api/meetings") return route.fulfill({ json: { items: [meeting] } });
    if (path === `/api/meetings/${meeting.id}/design-sessions`) return route.fulfill({ json: { items: designs } });
    const saved = designs.find(item => path === `/api/design-sessions/${item.id}`);
    if (saved) return route.fulfill({ json: saved });
    if (path.endsWith("/source.json")) return route.fulfill({ contentType: "application/json", body: diagram });
    if (path.endsWith("/image")) return route.fulfill({ status: 404 });
    return route.fulfill({ status: 500, json: { error: { message: "Synthetic unknown outcome" } } });
  });
  await page.goto("/design");
  await expect(page.getByRole("button", { name: /PRIVATE_DESIGN_SENTINEL · 1 versions/ })).toBeEnabled();
  return writes;
}

async function openDesign(page: Page, purpose = "PRIVATE_DESIGN_SENTINEL") {
  await page.getByRole("button", { name: `${purpose} · 1 versions` }).click();
  await expect(page.getByRole("heading", { name: purpose, exact: true })).toBeVisible();
  await page.getByText("Render the exact native source", { exact: true }).click();
}

test("failed source reopening clears previous source and exposes an accessible recoverable error", async ({ page }) => {
  const errors: string[] = [];
  page.on("pageerror", error => errors.push(error.message));
  const writes = await mockWorkspace(page, [design("design_one"), design("design_two", "Second isolated brief")]);
  await openDesign(page);
  await expect(page.getByRole("textbox", { name: "Structured diagram JSON" })).toHaveValue(/PRIVATE_SOURCE_SENTINEL/);
  await page.getByRole("button", { name: "New visual brief", exact: true }).click();
  await page.route(`${API}/api/design-versions/version_design_two/source.json`, route => route.fulfill({ status: 503, json: { error: "unavailable" } }));
  await openDesign(page, "Second isolated brief");
  await expect(page.getByRole("alert").filter({ hasText: "saved native source could not be read" })).toBeVisible();
  await expect(page.getByRole("textbox", { name: "Structured diagram JSON" })).toHaveValue("");
  await expect(page.getByRole("button", { name: "Render a saved native candidate" })).toBeDisabled();
  expect(writes).toEqual([]);
  expect(errors).toEqual([]);
});

test("late native source response cannot overwrite source typed after opening", async ({ page }) => {
  await mockWorkspace(page);
  let release!: () => void;
  const wait = new Promise<void>(resolve => { release = resolve; });
  await page.route(`${API}/api/design-versions/version_design_one/source.json`, async route => {
    await wait;
    return route.fulfill({ contentType: "application/json", body: diagram });
  });
  const requested = page.waitForRequest(`${API}/api/design-versions/version_design_one/source.json`);
  await openDesign(page);
  await requested;
  const source = page.getByRole("textbox", { name: "Structured diagram JSON" });
  await source.fill("PRIVATE_NEWER_SOURCE_SENTINEL");
  const completed = page.waitForResponse(`${API}/api/design-versions/version_design_one/source.json`);
  release();
  await completed;
  await expect(source).toHaveValue("PRIVATE_NEWER_SOURCE_SENTINEL");
  expect(await page.evaluate(() => JSON.stringify({ local: { ...localStorage }, session: { ...sessionStorage } }))).not.toContain("PRIVATE_");
});

test("expired visual access removes private candidates, history and source before offering another action", async ({ page }) => {
  const writes = await mockWorkspace(page);
  await openDesign(page);
  await expect(page.getByRole("textbox", { name: "Structured diagram JSON" })).toHaveValue(/PRIVATE_SOURCE_SENTINEL/);
  await page.route(`${API}/auth/session`, route => route.fulfill({ status: 401, json: { error: { message: "Expired account" } } }));
  await page.getByRole("button", { name: "Stop visual work", exact: true }).click();
  await expect(page.getByRole("alert").filter({ hasText: "Sign in" })).toBeVisible();
  await expect(page.getByText("PRIVATE_DESIGN_SENTINEL", { exact: false })).toHaveCount(0);
  await expect(page.getByRole("textbox", { name: "Structured diagram JSON" })).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Stop visual work", exact: true })).toHaveCount(0);
  expect(writes).toEqual([]);
});

test("a changed account cannot send the previous account's captured visual action", async ({ page }) => {
  const writes = await mockWorkspace(page);
  await openDesign(page);
  await page.route(`${API}/auth/session`, route => route.fulfill({ json: { owner_id: "owner_two", profile: "invited_hosted", csrf_token: "other_private_token" } }));
  await page.getByRole("button", { name: "Stop visual work", exact: true }).click();
  await expect(page.getByRole("alert").filter({ hasText: "signed-in account changed" })).toBeVisible();
  await expect(page.getByText("PRIVATE_DESIGN_SENTINEL", { exact: false })).toHaveCount(0);
  expect(writes).toEqual([]);
  expect(await page.evaluate(() => JSON.stringify({ ...sessionStorage }))).not.toContain("private_token");
});

test("malformed visual history fails validation before hostile text reaches the screen", async ({ page }) => {
  const writes = await mockWorkspace(page);
  await page.route(`${API}/api/design-sessions/design_one`, route => route.fulfill({ json: { ...design("design_one"), status: "invalid", brief: { purpose: "HOSTILE_UNVALIDATED_TEXT" } } }));
  await page.getByRole("button", { name: /PRIVATE_DESIGN_SENTINEL · 1 versions/ }).click();
  await expect(page.getByRole("alert").filter({ hasText: "unsupported visual contract" })).toBeVisible();
  await expect(page.getByText("HOSTILE_UNVALIDATED_TEXT", { exact: false })).toHaveCount(0);
  expect(writes).toEqual([]);
});

test("expired live access removes the previous private draft and saved history", async ({ page }) => {
  const privateMeeting = { ...meeting, title: "PRIVATE_LIVE_TITLE", draft: { ...rawSample.brief, title: "PRIVATE_LIVE_TITLE", question: "PRIVATE_LIVE_QUESTION" }, draft_revision: 1, runs: [] };
  const writes = await mockWorkspace(page);
  await page.route(`${API}/health`, route => route.fulfill({ json: { mode: "simulated", paid_access_configured: false } }));
  await page.route(`${API}/api/meetings`, route => route.fulfill({ json: { items: [privateMeeting], next_cursor: null } }));
  await page.route(`${API}/api/meetings/${meeting.id}/outcomes`, route => route.fulfill({ json: { items: [] } }));
  await page.route(`${API}/api/meetings/${meeting.id}/uploads`, route => route.fulfill({ json: { items: [] } }));
  await page.evaluate(id => sessionStorage.setItem("round-robin:current-local-comparison:v1", JSON.stringify({ meetingId: id, runId: null })), meeting.id);
  await page.goto("/live");
  await expect(page.getByRole("textbox", { name: "The decision you need to make", exact: true })).toHaveValue("PRIVATE_LIVE_QUESTION");
  await page.route(`${API}/auth/session`, route => route.fulfill({ status: 401, json: { error: { message: "Expired account" } } }));
  await page.getByRole("button", { name: "Save draft", exact: true }).click();
  await expect(page.getByRole("link", { name: "Sign in to the invited workspace", exact: true })).toBeVisible();
  await expect(page.getByRole("textbox", { name: "The decision you need to make", exact: true })).toHaveValue("");
  await expect(page.getByRole("textbox", { name: "Comparison title", exact: true })).toHaveValue("");
  await expect(page.getByText("PRIVATE_LIVE_TITLE", { exact: false })).toHaveCount(0);
  expect(writes).toEqual([]);
});
