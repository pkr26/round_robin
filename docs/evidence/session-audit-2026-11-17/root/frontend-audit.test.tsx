import { beforeEach, expect, test, vi } from 'vitest';
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import LiveWorkspace from '/Users/pradeepreddy/Desktop/round_robin/frontend/app/live-workspace';
import * as api from '/Users/pradeepreddy/Desktop/round_robin/frontend/app/api-client';
import fixture from '/Users/pradeepreddy/Desktop/round_robin/frontend/tests/fixtures/runtime-snapshot.json';
import { validateDraft } from '/Users/pradeepreddy/Desktop/round_robin/frontend/app/contracts';

vi.mock('/Users/pradeepreddy/Desktop/round_robin/frontend/app/api-client', async original => ({
  ...await original<typeof import('/Users/pradeepreddy/Desktop/round_robin/frontend/app/api-client')>(),
  readApi: vi.fn(), sendCommand: vi.fn(), refreshSession: vi.fn(), pendingCommand: vi.fn(),
  reconcileCommand: vi.fn(), retryPendingCommand: vi.fn(), abandonPendingCommand: vi.fn(), downloadSavedExport: vi.fn(),
  uploadTextFile: vi.fn(), signOut: vi.fn(), onAccessChange: vi.fn(),
}));

const key = 'round-robin:current-local-comparison:v1';
const copy = <T,>(value: T): T => structuredClone(value);
// Shape mutations below exercise nullable and defensive rendering contracts;
// the base snapshot was produced by the actual local simulated pipeline.
let model: any, quote: any, uploads: any[], outcomes: any[], receipt: any;
let accessChanged: () => void;
let readOverride: ((path: string) => unknown) | undefined;
const deferred = <T,>() => { let resolve!: (value: T) => void; let reject!: (error: unknown) => void; const promise = new Promise<T>((yes, no) => { resolve = yes; reject = no; }); return { promise, resolve, reject }; };

beforeEach(() => {
  // Existing navigation journeys explicitly approve discarding their local draft.
  vi.spyOn(window, 'confirm').mockReturnValue(true);
  vi.clearAllMocks(); model = copy(fixture); uploads = []; outcomes = []; receipt = null; readOverride = undefined;
  quote = { id: 'preflight_synthetic', draft_revision: 1, expires_at: '2099-01-01T00:00:00Z', recipients: ['openai', 'anthropic', 'google'], mode: 'simulated', configuration: {}, protocol: 'independent_review', limits: { max_requests: 24 }, approved_public_terms: [], warnings: [], blockers: [], paid_access_enabled: false, parent_run_id: null, parent_replacement: null, reuse_manifest: [] };
  vi.mocked(api.refreshSession).mockResolvedValue({ owner_id: 'owner_test', profile: 'development', csrf_token: null });
  vi.mocked(api.onAccessChange).mockImplementation(callback => { accessChanged = callback; return () => {}; });
  vi.mocked(api.pendingCommand).mockImplementation(() => receipt);
  vi.mocked(api.readApi).mockImplementation(async (path: string) => {
    const override = readOverride?.(path);
    if (override !== undefined) return await override as never;
    if (path === '/health') return { mode: 'simulated', paid_access_configured: false } as never;
    if (path === '/api/meetings') return { items: [copy(model.meeting)], next_cursor: null } as never;
    if (path.includes('/snapshot')) return copy(model.snapshot) as never;
    if (path.endsWith('/outcomes')) return { items: copy(outcomes) } as never;
    if (path.endsWith('/uploads')) return { items: copy(uploads) } as never;
    if (path.includes('/calculations/')) return copy(model.snapshot.report.calculations[0]) as never;
    if (path.includes('/sources/')) return copy(model.snapshot.evidence[0]) as never;
    if (path.includes('/sources')) return { items: copy(model.snapshot.evidence), next_cursor: null } as never;
    if (path.endsWith('/claims')) return { items: [] } as never;
    return copy(model.meeting) as never;
  });
  vi.mocked(api.sendCommand).mockImplementation(async (_intent, path, body) => {
    if (path.endsWith('/preflight')) { quote.reuse_manifest = (body as any).selected_parent_items ?? []; return copy(quote) as never; }
    if (path.endsWith('/outcomes')) return { id: `outcome_${outcomes.length}`, created_at: '2026-11-16T12:00:00Z', payload: body } as never;
    if (path.endsWith('/draft')) { model.meeting.draft = copy((body as any).brief); model.meeting.draft_revision++; return { brief: copy(model.meeting.draft), revision: model.meeting.draft_revision, meeting_id: model.meeting.id } as never; }
    if (path.endsWith('/follow-ups')) { model.meeting.draft = copy((body as any).brief); model.meeting.draft_revision++; return copy(model.meeting) as never; }
    if (path === '/api/meetings') { model.meeting.draft = copy((body as any).brief); return copy(model.meeting) as never; }
    return copy(model.snapshot.run) as never;
  });
  vi.mocked(api.downloadSavedExport).mockResolvedValue(undefined);
  vi.mocked(api.signOut).mockResolvedValue(undefined);
  vi.mocked(api.reconcileCommand).mockImplementation(async () => { receipt = null; return { confirmed: true, result: copy(model.snapshot.run) }; });
  vi.mocked(api.retryPendingCommand).mockImplementation(async () => { receipt = null; return copy(model.snapshot.run); });
  vi.mocked(api.abandonPendingCommand).mockImplementation(async () => { receipt = null; return { status: 'abandoned', result: {} }; });
  Object.defineProperty(HTMLDialogElement.prototype, 'showModal', { configurable: true, value: function () { this.setAttribute('open', ''); } });
  Object.defineProperty(HTMLDialogElement.prototype, 'close', { configurable: true, value: function () { this.removeAttribute('open'); this.dispatchEvent(new Event('close')); } });
  Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText: vi.fn().mockResolvedValue(undefined) } });
});

async function mount(run = true) {
  if (!run) model.meeting.runs = [];
  sessionStorage.setItem(key, JSON.stringify({ meetingId: model.meeting.id, runId: run ? model.snapshot.run.id : null }));
  const view = render(<LiveWorkspace />);
  await screen.findByText(/^(Local service|Workspace) connected$/);
  if (run && model.snapshot.report) await screen.findByRole('button', { name: 'Export JSON' });
  else if (!run) await screen.findByText(`Saved revision ${model.meeting.draft_revision}`);
  else await screen.findByRole('tab', { name: 'Overview' });
  return view;
}
async function click(name: string | RegExp) { fireEvent.click(screen.getByRole('button', { name })); await act(async () => {}); }
function edit(label: string | RegExp, value: string) { fireEvent.change(screen.getByLabelText(label, { exact: typeof label === 'string' }), { target: { value } }); }
async function prepare() { await click('Review preflight'); await screen.findByRole('heading', { name: 'Check the recipients, input and allowance.' }); }


// Audit probes assert reproduced current behavior, not repaired behavior.
test('session audit: failed history page hides Stop despite a valid running snapshot', async () => {
  model.snapshot.run.status = 'running';
  model.snapshot.report = null;
  model.snapshot.events = Array.from({ length:100 },(_, index)=>({...model.snapshot.events[0],sequence:index+1,run_id:model.snapshot.run.id}));
  model.snapshot.event_cutoff = 101;
  model.snapshot.next_event_cursor = 100;
  readOverride = path => path.includes('/events?') ? Promise.reject(new Error('Synthetic event page outage')) : undefined;
  sessionStorage.setItem(key,JSON.stringify({meetingId:model.meeting.id,runId:model.snapshot.run.id}));
  render(<LiveWorkspace />);
  await screen.findByText(/Connection interrupted/);
  expect(api.readApi).toHaveBeenCalledWith(`/api/runs/${model.snapshot.run.id}/snapshot`);
  expect(screen.queryByRole('button',{name:'Stop review'})).not.toBeInTheDocument();
  expect(api.sendCommand).not.toHaveBeenCalled();
});

test('session audit: failed history page suppresses an available completed report', async () => {
  model.snapshot.events = Array.from({ length:100 },(_,index)=>({...model.snapshot.events[0],sequence:index+1,run_id:model.snapshot.run.id}));
  model.snapshot.event_cutoff = 101;
  model.snapshot.next_event_cursor = 100;
  readOverride = path => path.includes('/events?') ? Promise.reject(new Error('Synthetic event page outage')) : undefined;
  sessionStorage.setItem(key,JSON.stringify({meetingId:model.meeting.id,runId:model.snapshot.run.id}));
  render(<LiveWorkspace />);
  await screen.findByText(/Connection interrupted/);
  expect(screen.queryByRole('button',{name:'Export JSON'})).not.toBeInTheDocument();
});

test('session audit: a transient assessment read failure hides the attestation feature without recovery', async () => {
  model.snapshot.report.simulated=false;
  readOverride=path=>path.endsWith('/assessments')?Promise.reject(new Error('Synthetic assessment outage')):undefined;
  await mount();
  await waitFor(()=>expect(api.readApi).toHaveBeenCalledWith(`/api/runs/${model.snapshot.run.id}/assessments`));
  fireEvent.click(screen.getByRole('tab',{name:'Evidence'}));
  expect(screen.queryByText('Record your evidence check')).not.toBeInTheDocument();
  expect(screen.queryByText(/assessment outage/)).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole('tab',{name:'Overview'}));
  fireEvent.click(screen.getByRole('tab',{name:'Evidence'}));
  expect(vi.mocked(api.readApi).mock.calls.filter(([path])=>path.endsWith('/assessments'))).toHaveLength(1);
});
