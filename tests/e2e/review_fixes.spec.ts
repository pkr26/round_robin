import { expect, test, type APIRequestContext, type Page } from './fixtures';
import rawSample from '../../contracts/sample.json';
import type { DraftBrief } from '../../frontend/app/api-client';

const API = process.env.ROUND_ROBIN_E2E_API_URL;
const CURRENT = 'round-robin:current-local-comparison:v1';
const PENDING = 'round-robin:pending-local-command:v1';
const created = new Set<string>();
test.describe.configure({ mode: 'serial' });
test.setTimeout(90_000);
test.skip(!API, 'These regressions require the explicitly configured disposable API harness.');

test.beforeEach(async ({ page, request }) => {
  expect((await request.get(`${API}/health`)).ok()).toBe(true);
  await page.route('**/*', route => {
    if (!['127.0.0.1', 'localhost'].includes(new URL(route.request().url()).hostname)) throw new Error('Review regression attempted external I/O');
    return route.continue();
  });
});

test.afterEach(async ({ request }) => {
  for (const id of created) await request.delete(`${API}/api/meetings/${id}`, { data: { command_id: crypto.randomUUID() } });
  created.clear();
});

async function createDraft(request: APIRequestContext, brief: DraftBrief) {
  const response = await request.post(`${API}/api/meetings`, { data: { command_id: crypto.randomUUID(), brief } });
  expect(response.ok(), await response.text()).toBe(true);
  const meeting = await response.json(); created.add(meeting.id);
  return meeting as { id: string; title: string; draft_revision: number; draft: DraftBrief };
}

async function openDraft(page: Page, meetingId: string, title: string) {
  await page.addInitScript(({ key, id }) => sessionStorage.setItem(key, JSON.stringify({ meetingId: id, runId: null })), { key: CURRENT, id: meetingId });
  await page.goto('/live');
  await expect(page.getByText(/^(Local service|Workspace) connected$/)).toBeVisible();
  await expect(page.getByLabel('Comparison title', { exact: true })).toHaveValue(title);
}

for (const committed of [false, true]) {
  test(`lost private draft acknowledgement can ${committed ? 'recover committed input' : 'fence a late original request'} after reload`, async ({ page, request }) => {
    const brief = structuredClone(rawSample.brief) as DraftBrief;
    brief.title = `Receipt recovery ${crypto.randomUUID().slice(0, 8)}`;
    const meeting = await createDraft(request, brief);
    await openDraft(page, meeting.id, brief.title);

    const privateTitle = `Private in-memory mutation ${crypto.randomUUID().slice(0, 8)}`;
    let original: { command_id: string; expected_revision: number; brief: DraftBrief } | undefined;
    let lostRequests = 0;
    await page.route(`${API}/api/meetings/${meeting.id}/draft`, async route => {
      if (route.request().method() !== 'PATCH' || original) return route.continue();
      original = route.request().postDataJSON(); lostRequests += 1;
      if (committed) {
        const saved = await route.fetch();
        expect(saved.ok(), await saved.text()).toBe(true);
      }
      return route.abort('failed');
    });
    await page.getByLabel('Comparison title', { exact: true }).fill(privateTitle);
    await page.getByRole('button', { name: 'Save draft', exact: true }).click();
    await expect(page.getByText('Action not confirmed', { exact: true })).toBeVisible();
    expect(original?.command_id).toBeTruthy();
    expect(await page.evaluate(key => sessionStorage.getItem(key), PENDING)).not.toContain(privateTitle);
    await page.reload();
    await expect(page.getByText('Action not confirmed', { exact: true })).toBeVisible();
    await expect(page.getByLabel('Comparison title', { exact: true })).toHaveValue(committed ? privateTitle : brief.title);
    await expect(page.getByRole('button', { name: 'Save draft', exact: true })).toBeDisabled();

    if (!committed) {
      await page.getByRole('button', { name: 'Reconcile pending action', exact: true }).click();
      await expect(page.getByText(/The action is not yet confirmed/)).toBeVisible();
      expect((await request.get(`${API}/api/commands/${original!.command_id}`)).status()).toBe(404);
      await page.getByRole('button', { name: 'Retry the same action', exact: true }).click();
      await expect(page.getByText(/Private inputs were kept only in memory/)).toBeVisible();
      await expect(page.getByRole('button', { name: 'Save draft', exact: true })).toBeDisabled();
    }

    const resolution = page.waitForResponse(response => response.request().method() === 'POST' && new URL(response.url()).pathname === `/api/commands/${original!.command_id}/abandon`);
    await page.getByRole('button', { name: 'Abandon unconfirmed action', exact: true }).click();
    expect((await (await resolution).json()).status).toBe(committed ? 'committed' : 'abandoned');
    await expect(page.getByText('Action not confirmed', { exact: true })).toHaveCount(0);
    await expect(page.getByRole('button', { name: 'Save draft', exact: true })).toBeEnabled();
    expect(await page.evaluate(key => sessionStorage.getItem(key), PENDING)).toBeNull();
    expect(lostRequests).toBe(1);

    // This is the exact delayed original, sent through the real API after the
    // browser's recovery. Its UUID must either remain idempotent or be fenced.
    const late = await request.patch(`${API}/api/meetings/${meeting.id}/draft`, { data: original });
    expect(late.status()).toBe(committed ? 200 : 410);
    if (!committed) expect((await late.json()).error.code).toBe('command_abandoned');
    const receipt = await (await request.get(`${API}/api/commands/${original!.command_id}`)).json();
    expect(receipt.outcome).toBe(committed ? 'committed' : 'abandoned');
    const saved = await (await request.get(`${API}/api/meetings/${meeting.id}`)).json();
    expect(saved.draft.title).toBe(committed ? privateTitle : brief.title);
    expect(saved.draft_revision).toBe(committed ? 2 : 1);

    const replacement = `New explicitly saved input ${crypto.randomUUID().slice(0, 8)}`;
    const nextSave = page.waitForResponse(response => response.request().method() === 'PATCH' && new URL(response.url()).pathname === `/api/meetings/${meeting.id}/draft`);
    await page.getByLabel('Comparison title', { exact: true }).fill(replacement);
    await page.getByRole('button', { name: 'Save draft', exact: true }).click();
    const next = await nextSave;
    expect(next.ok(), await next.text()).toBe(true);
    expect(next.request().postDataJSON().command_id).not.toBe(original!.command_id);
    expect((await (await request.get(`${API}/api/meetings/${meeting.id}`)).json()).draft.title).toBe(replacement);
  });
}

test('edited currency, cost inclusion and numeric requirements reach the real calculator unchanged', async ({ page, request }) => {
  const brief = structuredClone(rawSample.brief) as DraftBrief;
  brief.title = `Form semantics ${crypto.randomUUID().slice(0, 8)}`; brief.currency = 'EUR';
  for (const option of brief.options) {
    for (const cost of option.cost_components) cost.currency = 'EUR';
    for (const kind of ['maintenance', 'tax_delivery'] as const) {
      if (!option.cost_components.some(cost => cost.kind === kind)) option.cost_components.push({
        id: `${option.id}_${kind}`, label: kind, kind, amount: '0', currency: 'EUR', quantity: '1', frequency: 'once',
        status: 'not_applicable', source_ref: `${option.id}_${kind}_not_applicable`, unit: 'item', unknown_reason: null, quote_valid_until: null,
      });
    }
  }
  brief.hard_requirements[0].description = 'Total cost must remain within EUR 2500.';
  const numeric = { id: 'minimum_project_spend', description: 'Spend at least EUR 1200 on the selected approach', origin: 'user_supplied', metric: 'total_cost', operator: 'gte', threshold: '1200' } as const;
  brief.hard_requirements.push(numeric);
  brief.options[0].cost_components[0].status = 'not_applicable'; brief.options[0].cost_components[0].amount = '0';
  const meeting = await createDraft(request, brief);
  await openDraft(page, meeting.id, brief.title);
  await expect(page.getByLabel('Budget (EUR)', { exact: true })).toHaveValue('2500');
  await expect(page.getByLabel('Budget (USD)', { exact: true })).toHaveCount(0);
  await expect(page.getByRole('heading', { name: 'Saved numeric requirements', exact: true })).toBeVisible();

  // Keep the draft temporarily invalid so its autosave cannot race the group
  // of related form edits. The final title restores validity before Save.
  await page.getByLabel('Comparison title', { exact: true }).fill('');
  const candidate = page.getByRole('group', { name: 'Candidate 1', exact: true });
  const price = candidate.getByLabel('Upfront purchase (EUR)', { exact: true });
  await expect(price).toBeDisabled();
  await candidate.getByRole('combobox', { name: 'Upfront purchase treatment', exact: true }).selectOption('included');
  await expect(price).toBeEnabled(); await price.fill('9000');
  const manual = page.getByLabel(/Hard requirements — one per line/);
  expect(await manual.inputValue()).not.toContain(numeric.description);
  await manual.fill(`${await manual.inputValue()}\nKeep the workshop open`);
  await page.getByLabel('Comparison title', { exact: true }).fill(brief.title);
  const save = page.waitForResponse(response => response.request().method() === 'PATCH' && new URL(response.url()).pathname === `/api/meetings/${meeting.id}/draft`);
  await page.getByRole('button', { name: 'Save draft', exact: true }).click();
  expect((await save).ok()).toBe(true);
  await expect(page.getByText('Saved revision 2', { exact: true })).toBeVisible();
  const stored = await (await request.get(`${API}/api/meetings/${meeting.id}`)).json();
  expect(stored.draft.currency).toBe('EUR');
  expect(stored.draft.options[0].cost_components[0]).toMatchObject({ amount: '9000', currency: 'EUR', status: 'included' });
  expect(stored.draft.hard_requirements.find((item: { id: string }) => item.id === numeric.id)).toEqual(numeric);

  const prepared = page.waitForResponse(response => response.request().method() === 'POST' && new URL(response.url()).pathname === `/api/meetings/${meeting.id}/preflight`);
  await page.getByRole('button', { name: 'Review preflight', exact: true }).click();
  const preflight = await prepared;
  expect(preflight.ok(), await preflight.text()).toBe(true);
  expect((await preflight.json()).blockers).toEqual([]);
  await expect(page.getByRole('heading', { name: 'Check the recipients, input and allowance.', exact: true })).toBeVisible();
  await page.getByRole('checkbox', { name: /I understand the input scope/ }).check();
  await expect(page.getByRole('button', { name: 'Start simulated review', exact: true })).toBeEnabled();
  const accepted = page.waitForResponse(response => response.request().method() === 'POST' && new URL(response.url()).pathname === `/api/meetings/${meeting.id}/runs`);
  await page.getByRole('button', { name: 'Start simulated review', exact: true }).click();
  const run = await (await accepted).json();
  await expect(page.getByRole('button', { name: 'Export JSON', exact: true })).toBeVisible({ timeout: 60_000 });
  const snapshot = await (await request.get(`${API}/api/runs/${run.id}/snapshot`)).json();
  const compared = snapshot.report.comparison.find((item: { option_id: string }) => item.option_id === brief.options[0].id);
  expect(compared).toMatchObject({ currency: 'EUR', total: '9600.00' });
  expect(compared.constraint_results.find((item: { requirement_id: string }) => item.requirement_id === numeric.id).result).toBe('pass');
  expect(compared.constraint_results.find((item: { requirement_id: string }) => item.requirement_id === 'requirement_budget').result).toBe('fail');
});
