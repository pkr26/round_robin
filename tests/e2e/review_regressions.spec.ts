import { expect, test } from './fixtures';
import rawSample from '../../contracts/sample.json';

const API = process.env.ROUND_ROBIN_E2E_API_URL ?? 'http://127.0.0.1:8000';

test('unsaved invalid draft survives cancelled browser navigation and is discarded only after approval', async ({ page, request }) => {
  const ids: string[] = [];
  try {
    const saved = [];
    for (const label of ['source', 'target']) {
      const brief = structuredClone(rawSample.brief);
      brief.title = `Draft navigation ${label} ${crypto.randomUUID().slice(0, 8)}`;
      const response = await request.post(`${API}/api/meetings`, { data: { command_id: crypto.randomUUID(), brief } });
      expect(response.ok(), await response.text()).toBe(true);
      const meeting = await response.json(); saved.push(meeting); ids.push(meeting.id);
    }
    await page.route('**/*', route => {
      if (!['127.0.0.1', 'localhost'].includes(new URL(route.request().url()).hostname)) throw new Error('Draft regression attempted external I/O');
      return route.continue();
    });
    await page.addInitScript(id => sessionStorage.setItem('round-robin:current-local-comparison:v1', JSON.stringify({ meetingId: id, runId: null })), saved[0].id);
    await page.goto('/live');
    await expect(page.getByText(/^(Local service|Workspace) connected$/)).toBeVisible();
    await expect(page.getByLabel('Comparison title', { exact: true })).toHaveValue(saved[0].title);
    const target = page.getByRole('navigation', { name: 'Saved comparisons' }).getByRole('button', { name: new RegExp(saved[1].title) });
    for (let pageIndex = 0; await target.count() === 0 && pageIndex < 5; pageIndex += 1) {
      await page.getByRole('button', { name: 'Load more comparisons', exact: true }).click();
    }
    await expect(target).toBeVisible();
    // Invalid drafts cannot autosave, so this also verifies the guard outside
    // the ordinary 700ms debounce window without relying on a timing race.
    await page.getByLabel('Comparison title', { exact: true }).fill('');
    const dismissEvent = page.waitForEvent('dialog');
    const firstClick = target.click();
    const firstDialog = await dismissEvent;
    expect(firstDialog.type()).toBe('confirm'); expect(firstDialog.message()).toContain('unsaved edits');
    await firstDialog.dismiss(); await firstClick;
    await expect(page.getByRole('heading', { level: 1 })).toHaveText(saved[0].title);
    await expect(page.getByLabel('Comparison title', { exact: true })).toHaveValue('');
    const acceptEvent = page.waitForEvent('dialog');
    const nextClick = target.click();
    await (await acceptEvent).accept(); await nextClick;
    await expect(page.getByRole('heading', { level: 1 })).toHaveText(saved[1].title);
    await expect(page.getByLabel('Comparison title', { exact: true })).toHaveValue(saved[1].draft.title);
    const firstSaved = await (await request.get(`${API}/api/meetings/${saved[0].id}`)).json();
    expect(firstSaved.draft.title).toBe(saved[0].title); expect(firstSaved.draft_revision).toBe(1);
  } finally {
    for (const id of ids) await request.delete(`${API}/api/meetings/${id}`, { data: { command_id: crypto.randomUUID() } });
  }
});
