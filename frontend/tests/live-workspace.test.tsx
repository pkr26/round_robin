import { beforeEach, expect, test, vi } from 'vitest';
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import LiveWorkspace from '../app/live-workspace';
import * as api from '../app/api-client';
import fixture from './fixtures/runtime-snapshot.json';
import { validateDraft } from '../app/contracts';

vi.mock('../app/api-client', async original => ({
  ...await original<typeof import('../app/api-client')>(),
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

test('completed discussion reads later event pages through its snapshot cutoff exactly once', async () => {
  const template = model.snapshot.events[0];
  const events = Array.from({ length: 250 }, (_, index) => ({ ...template, run_id: model.snapshot.run.id, sequence: index + 1, event_type: 'content.committed', payload: { message: `Saved activity ${index + 1}` } }));
  model.snapshot.events = events.slice(0, 100);
  model.snapshot.event_cutoff = 250;
  model.snapshot.next_event_cursor = 100;
  readOverride = path => path.includes('/events?after=100') ? { items: [...events.slice(99), { ...events[249], sequence: 251, payload: { message: 'Beyond snapshot cutoff' } }], next_cursor: 251, event_cutoff: 251 } : undefined;
  await mount();
  fireEvent.click(screen.getByRole('tab', { name: 'Activity' }));
  expect(await screen.findByText('Saved activity 250')).toBeInTheDocument();
  expect(screen.getAllByText('Saved activity 100')).toHaveLength(1);
  expect(screen.queryByText('Beyond snapshot cutoff')).not.toBeInTheDocument();
  expect(api.sendCommand).not.toHaveBeenCalled();
});

test('late event pages are fenced when the workspace unmounts', async () => {
  const template = model.snapshot.events[0];
  model.snapshot.events = Array.from({ length: 100 }, (_, index) => ({ ...template, run_id: model.snapshot.run.id, sequence: index + 1 }));
  model.snapshot.event_cutoff = 101;
  model.snapshot.next_event_cursor = 100;
  const page = deferred<any>();
  readOverride = path => path.includes('/events?') ? page.promise : undefined;
  sessionStorage.setItem(key, JSON.stringify({ meetingId: model.meeting.id, runId: model.snapshot.run.id }));
  const view = render(<LiveWorkspace />);
  await waitFor(() => expect(api.readApi).toHaveBeenCalledWith(`/api/runs/${model.snapshot.run.id}/events?after=100&limit=200`));
  // Unmount while a private history read is unresolved; its completion must be fenced.
  view.unmount();
  await act(async () => page.resolve({ items: [{ ...template, sequence: 101, payload: { message: 'Late private activity' } }], next_cursor: 101, event_cutoff: 101 }));
  expect(screen.queryByText('Late private activity')).not.toBeInTheDocument();
});

test('scoped attestation submits exact saved target and remains separate from inference', async () => {
  model.snapshot.report.simulated = false;
  const target = { target_type: 'option_costs', option_id: 'option_a', requirement_id: null, target_hash: 'a'.repeat(64), label: 'Option A complete cost scope', reference_ids: ['evidence_quote_a'] };
  readOverride = path => path.endsWith('/assessments') ? { targets: [target] } : undefined;
  await mount();
  fireEvent.click(screen.getByRole('tab', { name: 'Evidence' }));
  await screen.findByText('Record your evidence check');
  edit('Scope of your check', '0');
  edit('What you checked and its limits', 'I checked the current supplier quote and all applicable charges.');
  edit('Valid through', '2027-01-01');
  await click('Save my scoped attestation');
  expect(api.sendCommand).toHaveBeenCalledWith(expect.stringContaining('assessment:'), `/api/runs/${model.snapshot.run.id}/assessments`, expect.objectContaining({
    target_type: target.target_type, option_id: target.option_id, target_hash: target.target_hash,
    reference_ids: target.reference_ids, result: 'supported', valid_until: '2027-01-01',
    expected_run_version: model.snapshot.run.version,
  }));
});

test('unavailable and revoked initialization show recoverable state without submitting actions', async () => {
  vi.mocked(api.refreshSession).mockRejectedValueOnce(new api.ApiError('Session revoked', 403));
  render(<LiveWorkspace />);
  await screen.findByText('Sign in to continue.');
  expect(api.sendCommand).not.toHaveBeenCalled();
  await click('Reconnect to local service');
  await screen.findByText('Local service connected');
});

test.each([new Error('Connection failed'), 'unknown failure'])('initial transport failure retains a retry path: %s', async error => {
  vi.mocked(api.refreshSession).mockRejectedValueOnce(error);
  render(<LiveWorkspace />);
  await screen.findByRole('button', { name: 'Reconnect to local service' });
  expect(screen.getByText(error instanceof Error ? error.message : 'The local service is unavailable.')).toBeInTheDocument();
});

test('retained selection falls back to scoped read, malformed selection uses server history', async () => {
  sessionStorage.setItem(key, JSON.stringify({ meetingId: model.meeting.id }));
  readOverride = path => path === '/api/meetings' ? { items: [], next_cursor: null } : undefined;
  const view = render(<LiveWorkspace />);
  await screen.findByRole('button', { name: 'Export JSON' });
  expect(api.readApi).toHaveBeenCalledWith(`/api/meetings/${model.meeting.id}`);
  view.unmount(); sessionStorage.setItem(key, '{'); readOverride = undefined;
  render(<LiveWorkspace />); await screen.findByText('Local service connected');
  await click(new RegExp(model.meeting.title));
  await screen.findByRole('button', { name: 'Export JSON' });
});

test('saved comparison paging deduplicates rows and server history selection is optional storage', async () => {
  const second = { ...copy(model.meeting), id: 'meeting_second', title: 'Another saved comparison', runs: [] };
  readOverride = path => path === '/api/meetings' ? { items: [copy(model.meeting)], next_cursor: 'a/b' } : path.includes('?cursor=') ? { items: [copy(model.meeting), second], next_cursor: null } : undefined;
  await mount(); await click('Load more comparisons');
  expect(within(screen.getByRole('navigation', { name: 'Saved comparisons' })).getAllByRole('button')).toHaveLength(2);
  vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => { throw new Error('optional storage denied'); });
  await click(/Another saved comparison/);
  expect(screen.getByLabelText('Comparison title')).toHaveValue(model.meeting.draft.title);
});

test('report modes preserve unknown cost, readiness and explicit research limitations', async () => {
  const report = model.snapshot.report;
  report.simulated = false; model.snapshot.run.execution_mode = 'live'; report.usage.actual_cost = null; report.usage.status = 'unknown';
  report.readiness = null; report.main_reasons = []; report.dissent = ['A reviewer disagrees']; report.change_summary = ['The budget changed'];
  report.research_summary = { status: 'partial', reason: null }; report.comparison[0].constraint_results = [];
  report.comparison[1].total = '1234.560000'; report.comparison[1].currency = 'EUR';
  report.comparison[2].known_subtotal = null;
  await mount();
  expect(screen.getByText('Readiness unavailable')).toBeInTheDocument();
  expect(screen.getByText(/Unresolved charges remain counted/)).toBeInTheDocument();
  expect(screen.getByText('No assessment supplied')).toBeInTheDocument();
  expect(screen.getByText(/Retrieved passages still require/)).toBeInTheDocument();
  expect(screen.getByText('A reviewer disagrees')).toBeInTheDocument();
});

test('unavailable saved inputs leave the partial report and exposure visible without creating a new brief', async () => {
  model.snapshot.brief = null;
  model.snapshot.run.status = 'failed';
  model.snapshot.report.readiness = null;
  model.snapshot.report.comparison = [];
  model.snapshot.report.usage.actual_cost = null;
  model.snapshot.report.usage.status = 'unknown';
  model.snapshot.report.usage.unknown_operation_ids = ['operation_unknown'];
  await mount();
  expect(screen.getByRole('heading', { name: 'Saved review inputs are unavailable.' })).toBeInTheDocument();
  expect(screen.getByText(/A new review or retry does not make them free/)).toBeInTheDocument();
  expect(screen.getByRole('button', { name: 'Create a linked follow-up' })).toBeDisabled();
  fireEvent.change(screen.getByLabelText('Chosen approach'), { target: { value: 'defer' } });
  expect(screen.getByRole('button', { name: 'Save my choice' })).toBeDisabled();
  expect(api.sendCommand).not.toHaveBeenCalled();
});

test('an existing report stays visible but cannot be used for actions when its inputs are unavailable', async () => {
  model.snapshot.brief = null;
  model.snapshot.run.status = 'completed';
  await mount();
  expect(screen.getByText(/Any report shown below is a saved record and does not validate those inputs/)).toBeInTheDocument();
  expect(screen.getByRole('button', { name: 'Create a linked follow-up' })).toBeDisabled();
  fireEvent.change(screen.getByLabelText('Chosen approach'), { target: { value: model.snapshot.report.comparison[0].option_id } });
  expect(screen.getByRole('button', { name: 'Save my choice' })).toBeDisabled();
  expect(api.sendCommand).not.toHaveBeenCalled();
});

test('unavailable stored evidence is disclosed instead of presented as an empty record', async () => {
  model.snapshot.brief = null;
  model.snapshot.evidence = null;
  model.snapshot.run.status = 'cancelled';
  model.snapshot.report.readiness = null;
  model.snapshot.report.comparison = [];
  await mount();
  expect(screen.getByRole('heading', { name: 'Stored evidence is unavailable in this report.' })).toBeInTheDocument();
  fireEvent.click(screen.getByRole('tab', { name: 'Evidence' }));
  expect(screen.getByText(/Stored evidence is unavailable in this report because it could not be validated/)).toBeInTheDocument();
  expect(screen.queryByText('No evidence passages have been committed yet.')).not.toBeInTheDocument();
  expect(screen.getByRole('button', { name: 'Export JSON' })).toBeEnabled();
});

test.each(['cancel_requested', 'cancelled', 'failed'])('ledger integrity loss does not show or export a current report when %s', async status => {
  model.snapshot.run.status = status;
  model.snapshot.run.execution_mode = 'live';
  model.snapshot.run.remaining_limits = { ...model.snapshot.run.remaining_limits, requests: null, requests_unavailable_reason: 'ledger_integrity_failed' };
  // Simulate an older report surviving in the received snapshot; it must not
  // be mistaken for the current state after the integrity fence.
  model.snapshot.report.recommendation = { option_id: 'option_a', summary: 'Stale recommendation' };
  sessionStorage.setItem(key, JSON.stringify({ meetingId: model.meeting.id, runId: model.snapshot.run.id }));
  render(<LiveWorkspace />);
  await screen.findByRole('heading', { name: 'The current report is unavailable.' });
  expect(screen.getByText(/remaining request count are unavailable/)).toBeInTheDocument();
  expect(screen.getByText(/Already submitted work may still incur charges/)).toBeInTheDocument();
  expect(screen.queryByText('Stale recommendation')).not.toBeInTheDocument();
  expect(screen.queryByText('The saved report is being prepared.')).not.toBeInTheDocument();
  expect(screen.queryByRole('button', { name: 'Export JSON' })).not.toBeInTheDocument();
  expect(api.sendCommand).not.toHaveBeenCalled();
});

test('a ledger integrity fence explains a missing report rather than promising preparation', async () => {
  model.snapshot.run.status = 'cancelled';
  model.snapshot.run.remaining_limits = { ...model.snapshot.run.remaining_limits, requests: null, requests_unavailable_reason: 'ledger_integrity_failed' };
  model.snapshot.report = null;
  sessionStorage.setItem(key, JSON.stringify({ meetingId: model.meeting.id, runId: model.snapshot.run.id }));
  render(<LiveWorkspace />);
  await screen.findByRole('heading', { name: 'The current report is unavailable.' });
  expect(screen.getByText(/This review is labeled simulated/)).toBeInTheDocument();
  expect(screen.queryByText('The saved report is being prepared.')).not.toBeInTheDocument();
  expect(screen.queryByRole('button', { name: 'Export JSON' })).not.toBeInTheDocument();
});

test('combined accounting and evidence failure does not imply a report was created', async () => {
  model.snapshot.run.status = 'cancelled';
  model.snapshot.run.remaining_limits = { ...model.snapshot.run.remaining_limits, requests: null, requests_unavailable_reason: 'ledger_integrity_failed' };
  model.snapshot.report = null;
  model.snapshot.evidence = null;
  sessionStorage.setItem(key, JSON.stringify({ meetingId: model.meeting.id, runId: model.snapshot.run.id }));
  render(<LiveWorkspace />);
  await screen.findByRole('heading', { name: 'The current report is unavailable.' });
  expect(screen.getByRole('heading', { name: 'Stored evidence is unavailable.' })).toBeInTheDocument();
  expect(screen.queryByText(/when the report was created/)).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole('tab', { name: 'Evidence' }));
  expect(screen.getByText('Stored evidence could not be validated. The original records are preserved for review.')).toBeInTheDocument();
});

test('a newer ledger-integrity snapshot replaces the old current report on refresh', async () => {
  model.snapshot.run.status = 'running';
  model.snapshot.report.recommendation = { option_id: 'option_a', summary: 'Previously saved recommendation' };
  await mount();
  expect(screen.getByText('Previously saved recommendation')).toBeInTheDocument();
  model.snapshot.run.status = 'cancelled';
  model.snapshot.run.version += 1;
  model.snapshot.run.remaining_limits = { ...model.snapshot.run.remaining_limits, requests: null, requests_unavailable_reason: 'ledger_integrity_failed' };
  model.snapshot.report = null;
  await screen.findByRole('heading', { name: 'The current report is unavailable.' }, { timeout: 3000 });
  expect(screen.queryByText('Previously saved recommendation')).not.toBeInTheDocument();
  expect(screen.queryByRole('button', { name: 'Export JSON' })).not.toBeInTheDocument();
});

test.each(['ready_to_choose', 'conditional', 'no_feasible_option', 'insufficient_evidence', 'future_state'])('readiness %s is displayed without dropping saved reasons', async readiness => {
  model.snapshot.report.readiness = readiness;
  model.snapshot.report.recommendation = { option_id: 'option_a', summary: 'Synthetic preferred approach' };
  model.snapshot.report.usage.actual_cost = '0.125000'; model.snapshot.run.execution_mode = 'live';
  model.snapshot.report.research_summary = { status: 'complete', reason: 'Saved retrieval reason' };
  await mount(); expect(screen.getByRole('heading', { name: 'Synthetic preferred approach' })).toBeInTheDocument();
  expect(screen.getByText(/Saved retrieval reason/)).toBeInTheDocument();
});

test.each(['accepted', 'modified', 'deferred'])('choice and later outcome are distinct immutable actions: %s', async choice => {
  model.snapshot.report.recommendation = { option_id: 'option_a', summary: 'Synthetic recommendation' };
  await mount();
  edit('Chosen approach', choice === 'deferred' ? 'defer' : choice === 'accepted' ? 'option_a' : 'option_b');
  edit('Why you chose it', 'My reason'); await click('Save my choice');
  await screen.findByText('Your choice is saved separately from the recommendation.');
  expect(api.sendCommand).toHaveBeenLastCalledWith(expect.stringContaining('choice:'), expect.stringContaining('/outcomes'), expect.objectContaining({ choice, observed_outcome: '' }));
  edit('What happened afterwards?', 'Observed actual delivery'); await click('Save outcome');
  await screen.findByText('Outcome saved. The original report and its assumptions are preserved.');
  expect(api.sendCommand).toHaveBeenLastCalledWith(expect.stringContaining('outcome:'), expect.stringContaining('/outcomes'), expect.objectContaining({ choice, observed_outcome: 'Observed actual delivery' }));
});

test('exact evidence, null calculations, source copy failure and focus return stay inspectable', async () => {
  model.snapshot.report.next_evidence = [{ id: 'request_one', question: 'Check the price', verification_method: 'Ask supplier', request_text: 'Please confirm the full price.' }];
  await mount(); await click(/Inspect Option A.*calculation/);
  expect(await screen.findByRole('dialog')).toHaveTextContent('Known subtotal:');
  await click('Close');
  fireEvent.click(screen.getByRole('tab', { name: 'Evidence' })); fireEvent.click(screen.getAllByRole('button', { name: 'Open exact passage & provenance' })[0]); await act(async () => {});
  expect(screen.getByRole('dialog')).toHaveTextContent('Passage SHA-256');
  fireEvent.click(screen.getByRole('dialog')); await act(async () => {});
  fireEvent.click(screen.getByRole('tab', { name: 'Overview' })); await click('Open copyable request'); await click('Copy request');
  await screen.findByText('Request copied.');
  vi.mocked(navigator.clipboard.writeText).mockRejectedValueOnce(new Error('clipboard denied')); await click('Copy request');
  await screen.findByText('Select the request text to copy it.');
});

test('resolved calculation does not fabricate unknown components and exports bind saved version', async () => {
  model.snapshot.report.calculations[0].output = '2500'; model.snapshot.report.calculations[0].known_subtotal = null; model.snapshot.report.calculations[0].missing_components = [];
  await mount(); await click(/Inspect Option A.*calculation/);
  expect(screen.getByRole('dialog')).toHaveTextContent('2500');
  expect(screen.queryByText(/Known subtotal:/)).not.toBeInTheDocument(); await click('Close');
  for (const name of ['Export JSON', 'Export Markdown', 'Export PDF']) await click(name);
  expect(api.downloadSavedExport).toHaveBeenCalledTimes(3);
  expect(api.downloadSavedExport).toHaveBeenLastCalledWith(model.snapshot.run.id, model.snapshot.report.id, 1, 'pdf');
});

test('saved event fallbacks and keyboard tabs retain honest empty evidence', async () => {
  model.snapshot.events = [
    { sequence: 1, timestamp: '2026-11-16T12:00:00Z', event_type: 'stage.started', payload: { stage: 'audit' } },
    { sequence: 2, payload: { reason: 'limited' } }, { sequence: 3, payload: { status: 'unknown' } },
    { sequence: 4, payload: {} }, { payload: null },
  ]; model.snapshot.evidence = []; model.snapshot.report.coverage[0].provider = 'future_provider';
  await mount();
  fireEvent.keyDown(screen.getByRole('tab', { name: 'Overview' }), { key: 'ArrowRight' });
  expect(screen.getByText('No evidence passages have been committed yet.')).toBeInTheDocument();
  fireEvent.keyDown(screen.getByRole('tab', { name: 'Evidence' }), { key: 'ArrowLeft' });
  fireEvent.keyDown(screen.getByRole('tab', { name: 'Overview' }), { key: 'Tab' });
  fireEvent.click(screen.getByRole('tab', { name: 'Activity' }));
  expect(screen.getByText('Committed to the saved review.')).toBeInTheDocument();
  expect(screen.getByText('future_provider')).toBeInTheDocument();
});

test('linked draft preserves parent while explicit save and a new preflight authorize child work', async () => {
  await mount(); await click('Create a linked follow-up');
  edit('Comparison title', 'Updated facts'); await click('Save linked follow-up draft');
  await screen.findByText(/Follow-up draft saved/);
  expect(api.sendCommand).toHaveBeenCalledWith(expect.stringContaining('followup:'), expect.stringContaining('/follow-ups'), expect.objectContaining({ parent_run_id: model.snapshot.run.id }));
  expect(vi.mocked(api.sendCommand).mock.calls.some(call => call[1].endsWith('/runs'))).toBe(false);
});

test('access revocation erases visible saved private state and editor selections', async () => {
  await mount(); await click(/Inspect Option A.*calculation/);
  act(() => accessChanged());
  expect(screen.getByText('Sign in to continue.')).toBeInTheDocument();
  expect(screen.queryByRole('button', { name: 'Export JSON' })).not.toBeInTheDocument();
  expect(screen.getByLabelText('Comparison title')).toHaveValue('');
  expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
});

test.each([false, true])('uncertain command uses retained UUID for reconciliation/retry: %s', async retry => {
  receipt = { intent: retry ? 'cancel:run_saved' : 'start:saved', path: '/api/meetings/saved/runs' };
  await mount(); await click(retry ? 'Retry the same action' : 'Reconcile pending action');
  await screen.findByText('The original action has been reconciled with saved server state.');
  expect(retry ? api.retryPendingCommand : api.reconcileCommand).toHaveBeenCalledOnce();
  expect(api.sendCommand).not.toHaveBeenCalled();
});

test('unconfirmed reconciliation retains the action and refuses invented success', async () => {
  receipt = { intent: 'draft:saved', path: '/api/meetings/saved/draft' };
  vi.mocked(api.reconcileCommand).mockResolvedValue({ confirmed: false });
  await mount(false); await click('Reconcile pending action');
  await screen.findByText(/action is not yet confirmed/);
  expect(screen.getByRole('button', { name: 'Save draft' })).toBeDisabled();
});

test.each(['reconcile', 'abandon'])('committed action with unavailable saved receipt is explained after %s', async mode => {
  receipt = { intent: 'followup:saved', path: '/api/meetings/saved/follow-ups' };
  const result = { receipt_unavailable: true, message: 'server explanation' };
  if (mode === 'reconcile') vi.mocked(api.reconcileCommand).mockImplementation(async () => { receipt = null; return { confirmed: true, result }; });
  else vi.mocked(api.abandonPendingCommand).mockImplementation(async () => { receipt = null; return { status: 'committed', result }; });
  await mount();
  vi.mocked(api.readApi).mockClear();
  await click(mode === 'reconcile' ? 'Reconcile pending action' : 'Abandon unconfirmed action');
  await screen.findByText('The original action committed, but its saved receipt cannot be displayed. The workspace has been refreshed.');
  expect(api.readApi).toHaveBeenCalledWith('/api/meetings');
  expect(api.sendCommand).not.toHaveBeenCalled();
  expect(screen.queryByRole('button', { name: 'Reconcile pending action' })).not.toBeInTheDocument();
});

test.each(['reconcile', 'abandon'])('committed receipt remains explained when workspace refresh fails after %s', async mode => {
  receipt = { intent: 'followup:saved', path: '/api/meetings/saved/follow-ups' };
  const result = { receipt_unavailable: true };
  if (mode === 'reconcile') vi.mocked(api.reconcileCommand).mockImplementation(async () => { receipt = null; return { confirmed: true, result }; });
  else vi.mocked(api.abandonPendingCommand).mockImplementation(async () => { receipt = null; return { status: 'committed', result }; });
  await mount();
  readOverride = path => path === '/api/meetings' ? Promise.reject(new Error('Workspace refresh failed')) : undefined;
  await click(mode === 'reconcile' ? 'Reconcile pending action' : 'Abandon unconfirmed action');
  await screen.findByText('The original action committed, but its saved receipt cannot be displayed. Refresh the workspace to view current state.');
  expect(screen.getByText('Workspace refresh failed')).toBeInTheDocument();
  expect(screen.queryByRole('button', { name: 'Reconcile pending action' })).not.toBeInTheDocument();
  expect(api.sendCommand).not.toHaveBeenCalled();
});

test('preflight requires renewed consent, preserves live blockers and expiry', async () => {
  readOverride = path => path === '/health' ? { mode: 'live', paid_access_configured: true } : undefined;
  await mount(false); edit('Review execution', 'live');
  quote.mode = 'live'; quote.configuration = { models: { openai: 'synthetic-model' } }; quote.recipients.push('other_provider');
  quote.approved_public_terms = [{ id: 'term_one', text: 'approved public phrase' }]; quote.warnings = ['Retain this warning']; quote.blockers = ['Funding locked'];
  await prepare(); fireEvent.click(screen.getByRole('checkbox', { name: /I understand/ }));
  expect(screen.getByRole('button', { name: 'Start live review' })).toBeDisabled();
  expect(screen.getByText(/is currently blocked/)).toBeInTheDocument();
  await click('Return to draft'); quote.blockers = []; quote.paid_access_enabled = true; quote.expires_at = '2000-01-01T00:00:00Z';
  await prepare(); expect(screen.getByText(/This preflight expired/)).toBeInTheDocument();
  expect(screen.getByRole('checkbox', { name: /I understand/ })).toBeDisabled();
});

test('parent replacement and selected immutable reuse require refreshed preflight authority', async () => {
  quote.parent_run_id = model.snapshot.run.id; quote.parent_replacement = { run_id: model.snapshot.run.id, version: 6, status: 'running' };
  readOverride = path => path.endsWith('/claims') ? { items: [{ id: 'claim_selected', version: 2, sha256: 'a'.repeat(64), proposition: 'Parent proposition', evidence_status: 'not_checked', verified_at: '2026-11-16T12:00:00Z' }] } : undefined;
  await mount(false); await prepare();
  const checkbox = await screen.findByRole('checkbox', { name: /Parent proposition/ });
  expect(checkbox).not.toBeChecked(); fireEvent.click(checkbox);
  expect(screen.getByRole('button', { name: 'Stop parent and start follow-up' })).toBeDisabled();
  await click('Refresh preflight with selected parent items');
  expect(api.sendCommand).toHaveBeenLastCalledWith(expect.any(String), expect.stringContaining('/preflight'), expect.objectContaining({ selected_parent_items: [{ id: 'claim_selected', kind: 'claim', version: 2, sha256: 'a'.repeat(64) }] }));
  expect(screen.getByText(/1 saved parent items/)).toBeInTheDocument();
  fireEvent.click(screen.getByRole('checkbox', { name: /I understand/ })); await click('Stop parent and start follow-up');
  expect(api.sendCommand).toHaveBeenCalledWith(expect.stringContaining('start:'), expect.stringContaining('/runs'), expect.objectContaining({ replace_parent_run_version: 6 }));
});

test('new draft preserves exact costs, unknowns, bounded alternatives and explicit public phrases', async () => {
  await mount(false); await click(/New comparison/);
  edit('Comparison title', 'A new comparison'); edit('The decision you need to make', 'Which option fits?'); edit('What a good result must achieve', 'Make a useful comparison');
  edit('Budget (USD)', '1000'); edit('Budget (USD)', ''); edit('Budget (USD)', '1200');
  edit('Deadline', '2027-01-01'); edit('Deadline', ''); edit('Deadline', '2027-02-01');
  edit('Common cost horizon (months)', ''); edit('Common cost horizon (months)', '12'); edit('Location or site context', 'A private location');
  edit(/Hard requirements/, 'Keep the business open\n\nAvoid downtime'); edit(/Preferences/, 'Lower maintenance\n\nSimpler training');
  const first = screen.getByRole('group', { name: 'Candidate 1' });
  fireEvent.change(within(first).getByLabelText('Approach name'), { target: { value: 'Preferred label' } });
  fireEvent.change(within(first).getByLabelText('upfront cost (USD)'), { target: { value: '30.001' } });
  fireEvent.change(within(first).getByLabelText('upfront cost (USD)'), { target: { value: '' } });
  await click(/Add candidate option/); await click(/Add candidate option/);
  expect(screen.getByRole('button', { name: /Add candidate option/ })).toBeDisabled();
  fireEvent.click(screen.getAllByRole('button', { name: 'Remove this candidate' })[0]);
  edit('Selected supporting text', 'Private supporting facts'); edit(/Approved public research terms/, 'approved phrase\n\nsecond approved phrase');
  await click('Save draft'); await screen.findByText(/Comparison saved/);
  expect(api.sendCommand).toHaveBeenCalledWith('create-comparison', '/api/meetings', expect.objectContaining({ brief: expect.objectContaining({ budget: '1200', deadline: '2027-02-01', options: expect.any(Array), background: 'Private supporting facts', public_research_terms: [{ id: 'public_term_0', text: 'approved phrase' }, { id: 'public_term_1', text: 'second approved phrase' }] }) }));
});

test.each([
  ['Hard requirements', Array.from({ length: 21 }, (_, i) => `Requirement ${i}`).join('\n'), 'manual requirements'],
  ['Hard requirements', 'x'.repeat(501), 'Keep each requirement within 500'],
  ['Preferences', Array.from({ length: 11 }, (_, i) => `Preference ${i}`).join('\n'), 'at most 10 preferences'],
  ['Preferences', 'x'.repeat(501), 'Keep each preference within 500'],
  ['Approved public research terms', Array.from({ length: 9 }, (_, i) => `term ${i}`).join('\n'), 'at most eight'],
  ['Approved public research terms', 'x'.repeat(121), 'at most eight'],
])('invalid %s is retained visibly and cannot silently truncate input', async (label, value, reason) => {
  await mount(false); edit(new RegExp(label), value);
  const input = screen.getByLabelText(new RegExp(label)) as HTMLTextAreaElement;
  expect(input.value).toBe(value); expect(input.validationMessage).toContain(reason);
  expect(api.sendCommand).not.toHaveBeenCalled();
});

test('explicit draft save uses its current revision and resets the new comparison selection', async () => {
  await mount(false); edit('Comparison title', 'Edited saved facts'); await click('Save draft');
  await screen.findByText('Draft saved on the local service. No adviser was called.');
  expect(api.sendCommand).toHaveBeenCalledWith(expect.stringContaining('draft:'), expect.stringContaining('/draft'), expect.objectContaining({ expected_revision: 1 }), 'PATCH');
  await click(/New comparison/); expect(sessionStorage.getItem(key)).toBeNull();
});

test.each(['ready', 'temporary'])('upload %s attaches only ready private files and selection is explicit', async status => {
  uploads = [{ id: 'upload_existing', filename: 'saved.txt', status: 'ready', byte_size: 12, sha256: 'b'.repeat(64), recipients: [] }, { id: 'upload_pending', filename: 'pending.txt', status: 'temporary', byte_size: 0 }];
  vi.mocked(api.uploadTextFile).mockResolvedValue({ id: 'upload_new', filename: 'new.txt', status, byte_size: 6, sha256: null, recipients: [] });
  await mount(false);
  const input = screen.getByLabelText('Upload selected text or Markdown');
  fireEvent.change(input, { target: { files: [] } });
  fireEvent.change(input, { target: { files: [new File(['sample'], 'new.txt', { type: 'text/plain' })] } });
  await screen.findByText(status === 'ready' ? /Text uploaded and validated/ : /upload is not ready/);
  expect(screen.getByRole('checkbox', { name: /pending.txt/ })).toBeDisabled();
  const selected = screen.getByRole('checkbox', { name: /saved.txt/ }); fireEvent.click(selected); fireEvent.click(selected);
  expect(selected).not.toBeChecked();
  expect(screen.getByRole('checkbox', { name: /new.txt/ })).toHaveProperty('checked', status === 'ready');
});

test('autosave snapshots valid edits and preserves edits typed while the save is in flight', async () => {
  await mount(false); vi.useFakeTimers();
  const saving = deferred<any>(); vi.mocked(api.sendCommand).mockReturnValueOnce(saving.promise);
  edit('Comparison title', 'First edit'); await act(async () => { await vi.advanceTimersByTimeAsync(701); });
  expect(screen.getByText('Saving changes…')).toBeInTheDocument();
  edit('Comparison title', 'Second edit');
  await act(async () => saving.resolve({ brief: { ...model.meeting.draft, title: 'First edit' }, revision: 2 }));
  expect(screen.getByLabelText('Comparison title')).toHaveValue('Second edit');
  await act(async () => { await vi.advanceTimersByTimeAsync(701); });
  expect(screen.getByText('Saved revision 2')).toBeInTheDocument();
  expect(screen.getByLabelText('Comparison title')).toHaveValue('Second edit');
});

test.each([new Error('Autosave interrupted'), 'unknown'])('failed autosave retains edits and editing clears its error state: %s', async failure => {
  await mount(false); vi.useFakeTimers(); vi.mocked(api.sendCommand).mockRejectedValueOnce(failure);
  edit('Comparison title', 'Retained edit'); await act(async () => { await vi.advanceTimersByTimeAsync(701); });
  expect(screen.getByText('Save not confirmed; edits retained in this tab')).toBeInTheDocument();
  expect(screen.getByLabelText('Comparison title')).toHaveValue('Retained edit');
  edit('Comparison title', 'Retry a new edit'); await act(async () => { await vi.advanceTimersByTimeAsync(701); });
  expect(screen.getByText('Saved revision 2')).toBeInTheDocument();
});

test('late autosave cannot repaint a different selected meeting', async () => {
  const second = { ...copy(model.meeting), id: 'meeting_other', title: 'Other private draft', runs: [] };
  readOverride = path => path === '/api/meetings' ? { items: [copy(model.meeting), second], next_cursor: null } : undefined;
  await mount(false); vi.useFakeTimers(); const saving = deferred<any>(); vi.mocked(api.sendCommand).mockReturnValueOnce(saving.promise);
  edit('Comparison title', 'First owner draft'); await act(async () => { await vi.advanceTimersByTimeAsync(701); });
  await click(/Other private draft/);
  await act(async () => saving.resolve({ brief: { ...model.meeting.draft, title: 'First owner draft' }, revision: 2 }));
  expect(screen.getByRole('heading', { level: 1 })).toHaveTextContent('Other private draft');
  expect(screen.getByLabelText('Comparison title')).not.toHaveValue('First owner draft');
});

test('new pending authority appearing before autosave prevents an implicit command replay', async () => {
  await mount(false); vi.useFakeTimers(); edit('Comparison title', 'Wait for explicit reconciliation');
  receipt = { intent: 'start:other', path: '/api/meetings/other/runs' };
  await act(async () => { await vi.advanceTimersByTimeAsync(701); });
  expect(api.sendCommand).not.toHaveBeenCalled();
});

test('running review cancellation acknowledges saved state without erasing the report', async () => {
  model.snapshot.run.status = 'running'; await mount();
  await click('Stop review'); await screen.findByText(/Stop acknowledged/);
  expect(api.sendCommand).toHaveBeenCalledWith(expect.stringContaining('cancel:'), expect.stringContaining('/cancel'), { expected_run_version: 6 });
  expect(screen.getByRole('button', { name: 'Export JSON' })).toBeInTheDocument();
});

test('clarification handles partial question formats and resumes the same run', async () => {
  model.snapshot.run.status = 'awaiting_input'; model.snapshot.questions = ['Confirm the price', { question: 'Confirm installation' }, { text: 'Confirm timing' }, {}, null];
  await mount(); await click('Answer and confirm brief');
  expect(screen.getByLabelText(/Approved public research terms/)).toBeDisabled();
  edit('Comparison title', 'Clarified facts'); await click('Confirm updated brief');
  await screen.findByText(/brief is confirmed/);
  expect(api.sendCommand).toHaveBeenCalledWith(expect.stringContaining('clarification:'), expect.stringContaining('/clarifications'), expect.objectContaining({ expected_run_version: 6 }));
});

test.each(['synthesis', 'audit'])('eligible %s recovery keeps original review identity', async stage => {
  model.snapshot.retry_eligible = true; model.snapshot.retryable_stages = [stage];
  await mount(); await click(`Retry ${stage}`); await screen.findByText(/Recovery requested within the original/);
  expect(api.sendCommand).toHaveBeenCalledWith(expect.stringContaining('retry:'), expect.stringContaining('/retries'), { expected_run_version: 6, stage });
});

test.each(['running', 'completed'])('a %s run without a saved report cannot invent a recommendation', async status => {
  model.snapshot.report = null; model.snapshot.run.status = status; await mount();
  expect(screen.queryByRole('button', { name: 'Export JSON' })).not.toBeInTheDocument();
  await click('Inspect committed activity'); expect(screen.getByRole('tab', { name: 'Activity' })).toHaveAttribute('aria-selected', 'true');
});

test.each([401, 403, 503])('snapshot HTTP%s becomes a visible reconnect/access state', async status => {
  model.snapshot.report = null;
  readOverride = path => path.includes('/snapshot') ? Promise.reject(new api.ApiError('Cannot read private state', status)) : undefined;
  await mount(); await screen.findByText(/Connection interrupted/);
  if (status < 500) expect(screen.getByText('Sign in to continue.')).toBeInTheDocument();
  expect(api.sendCommand).not.toHaveBeenCalled();
});

test('out-of-order polls cannot downgrade a newer committed snapshot', async () => {
  model.snapshot.run.status = 'running'; model.snapshot.report.recommendation = { option_id: 'option_a', summary: 'Newest committed answer' };
  vi.useFakeTimers({ shouldAdvanceTime: true }); await mount();
  model.snapshot.run.version--; model.snapshot.report.recommendation.summary = 'Obsolete answer';
  await act(async () => { await vi.advanceTimersByTimeAsync(701); });
  expect(vi.mocked(api.readApi).mock.calls.filter(([path]) => path.includes('/snapshot')).length).toBeGreaterThan(1);
  expect(screen.getByRole('heading', { name: 'Newest committed answer' })).toBeInTheDocument();
  model.snapshot.run.version += 2; model.snapshot.event_cutoff = 0;
  await act(async () => { await vi.advanceTimersByTimeAsync(701); });
  expect(screen.queryByRole('heading', { name: 'Obsolete answer' })).not.toBeInTheDocument();
});

test.each([new api.ApiError('Revoked during action', 401), new api.ApiError('Forbidden during action', 403), new Error('Action failed'), 'unknown'])('action failure never claims success: %s', async failure => {
  await mount(); vi.mocked(api.downloadSavedExport).mockRejectedValueOnce(failure);
  await click('Export JSON');
  expect(screen.getByText(failure instanceof Error ? failure.message : 'The action could not be confirmed.')).toBeInTheDocument();
});

test.each([new Error('Receipt storage unavailable'), 'unknown'])('pending receipt read failure is shown after a command: %s', async failure => {
  await mount(); vi.mocked(api.pendingCommand).mockImplementation(() => { throw failure; });
  await click('Export JSON');
  expect(screen.getByText(failure instanceof Error ? failure.message : 'Pending action unavailable')).toBeInTheDocument();
});

test('invited profile shows sign-out and keeps failed logout explicit', async () => {
  vi.mocked(api.refreshSession).mockResolvedValue({ owner_id: 'owner_test', profile: 'invited_hosted', csrf_token: 'csrf' });
  vi.mocked(api.signOut).mockRejectedValueOnce(new Error('Sign-out not confirmed'));
  await mount(); expect(screen.getByText('Saved in your invited Round Robin workspace.')).toBeInTheDocument();
  await click('Sign out'); expect(screen.getByText('Sign-out not confirmed')).toBeInTheDocument();
});

test('history sorts current versions, keeps linked lineage and can return between draft and saved review', async () => {
  model.meeting.runs.push({ ...copy(model.meeting.runs[0]), id: 'run_older', created_at: '2026-01-01T00:00:00Z', parent_run_id: 'run_parent' });
  await mount(); await click('Edit draft'); expect(screen.getByLabelText('Comparison title')).toBeInTheDocument();
  await click('View saved review'); expect(screen.getByRole('button', { name: 'Export JSON' })).toBeInTheDocument();
  const history = screen.getByRole('heading', { name: 'Review history' }).parentElement!;
  fireEvent.click(within(history).getByRole('button', { name: /linked follow-up/ })); await act(async () => {});
  expect(JSON.parse(sessionStorage.getItem(key)!)).toMatchObject({ runId: 'run_older' });
});

test('normal simulated Start never adds parent replacement authority', async () => {
  await mount(false); await prepare(); fireEvent.click(screen.getByRole('checkbox', { name: /I understand/ }));
  await click('Start simulated review');
  const body = vi.mocked(api.sendCommand).mock.calls.find(([, path]) => path.endsWith('/runs'))![2];
  expect(body).not.toHaveProperty('replace_parent_run_version');
});

test('bounded parent selection pages deduplicate and preserve exact versions, with no automatic import', async () => {
  quote.parent_run_id = model.snapshot.run.id; delete quote.reuse_manifest;
  const evidence = Array.from({ length: 25 }, (_, index) => ({ ...copy(model.snapshot.evidence[0]), id: `parent_evidence_${index}`, source_title: `Evidence ${index}` }));
  readOverride = path => path.endsWith('/sources') ? { items: evidence.slice(0, 24), next_cursor: 'cursor/saved' } : path.includes('/sources?') ? { items: [evidence[0], evidence[24]], next_cursor: null } : undefined;
  await mount(false); await prepare(); await click('Load more parent evidence');
  const boxes = screen.getAllByRole('checkbox', { name: /^Evidence · Evidence/ });
  expect(boxes).toHaveLength(25);
  for (const box of boxes.slice(0, 24)) fireEvent.click(box);
  expect(boxes[24]).toBeDisabled(); expect(boxes[0]).not.toBeDisabled();
  fireEvent.click(boxes[0]); expect(boxes[24]).not.toBeDisabled();
  await click('Refresh preflight with selected parent items');
  const selected = (vi.mocked(api.sendCommand).mock.calls.at(-1)![2] as any).selected_parent_items;
  expect(selected).toHaveLength(23); expect(selected.every((item: any) => item.version === 1 && item.kind === 'evidence')).toBe(true);
});

test('unreadable parent support produces an error without selecting or fabricating items', async () => {
  quote.parent_run_id = model.snapshot.run.id;
  readOverride = path => path.endsWith('/sources') ? Promise.reject(new Error('Not accessible')) : undefined;
  await mount(false); await prepare(); await screen.findByText(/parent evidence and claims could not be read/);
  expect(screen.queryAllByRole('checkbox', { name: /^Evidence ·/ })).toHaveLength(0);
});

test.each(['resolve', 'reject'])('late parent read %s cannot repaint a different mounted scope', async settlement => {
  quote.parent_run_id = model.snapshot.run.id;
  const oldParent = quote.parent_run_id;
  const waiting = deferred<any>(); readOverride = path => path === `/api/runs/${oldParent}/sources` ? waiting.promise : path.endsWith('/sources') ? { items: [], next_cursor: null } : undefined;
  await mount(false); await prepare(); await click('Return to draft');
  quote.parent_run_id = 'run_different_parent'; await prepare();
  await act(async () => { if (settlement === 'resolve') waiting.resolve({ items: [{ ...model.snapshot.evidence[0], source_title: 'Stale private parent sentinel' }], next_cursor: null }); else waiting.reject(new Error('Access changed')); });
  expect(screen.getByRole('heading', { name: 'Choose earlier evidence and claims to reuse' })).toBeInTheDocument();
  expect(screen.queryByRole('checkbox', { name: /Stale private parent sentinel/ })).not.toBeInTheDocument();
  expect(screen.queryByText(/parent evidence and claims could not be read/)).not.toBeInTheDocument();
});

test.each(['resolve', 'reject'])('late snapshot %s cannot replace another mounted saved review', async settlement => {
  const oldSnapshot = copy(model.snapshot); oldSnapshot.report.recommendation = { option_id: 'option_a', summary: 'Stale private answer sentinel' };
  const newSnapshot = copy(model.snapshot); newSnapshot.run.id = 'run_different'; newSnapshot.report.run_id = 'run_different'; newSnapshot.report.recommendation = { option_id: 'option_a', summary: 'Current saved answer' };
  model.meeting.runs.push({ ...copy(model.meeting.runs[0]), id: 'run_different', created_at: '2026-01-01T00:00:00Z' });
  const waiting = deferred<any>(); readOverride = path => path === `/api/runs/${oldSnapshot.run.id}/snapshot` ? waiting.promise : path === '/api/runs/run_different/snapshot' ? newSnapshot : undefined;
  sessionStorage.setItem(key, JSON.stringify({ meetingId: model.meeting.id, runId: model.snapshot.run.id }));
  render(<LiveWorkspace />); await screen.findByRole('tab', { name: 'Overview' });
  const history = screen.getByRole('heading', { name: 'Review history' }).parentElement!;
  fireEvent.click(within(history).getAllByRole('button')[1]);
  await screen.findByRole('heading', { name: 'Current saved answer' });
  await act(async () => { if (settlement === 'resolve') waiting.resolve(oldSnapshot); else waiting.reject(new Error('Access changed')); });
  expect(screen.getByRole('heading', { name: 'Current saved answer' })).toBeInTheDocument();
  expect(screen.queryByRole('heading', { name: 'Stale private answer sentinel' })).not.toBeInTheDocument();
  expect(screen.queryByText(/Connection interrupted/)).not.toBeInTheDocument();
});

test.each(['resolve', 'reject'])('late upload/outcome reads %s cannot repopulate another mounted meeting', async settlement => {
  const waiting = deferred<any>(); const oldMeeting = model.meeting.id;
  const other = { ...copy(model.meeting), id: 'meeting_other', title: 'Other current meeting', runs: [] };
  readOverride = path => path === '/api/meetings' ? { items: [copy(model.meeting), other], next_cursor: null }
    : path.startsWith(`/api/meetings/${oldMeeting}/`) && /\/(uploads|outcomes)$/.test(path) ? waiting.promise : undefined;
  await mount(false); await click(/Other current meeting/);
  await act(async () => { if (settlement === 'resolve') waiting.resolve({ items: [{ id: 'upload_old', filename: 'stale-private-file.txt', status: 'ready', byte_size: 6, payload: { run_id: model.snapshot.run.id } }] }); else waiting.reject(new Error('No access')); });
  expect(screen.getByRole('heading', { level: 1, name: 'Other current meeting' })).toBeInTheDocument();
  expect(screen.queryByRole('checkbox', { name: /stale-private-file/ })).not.toBeInTheDocument();
});

test('missing retained meeting and failed post-report refresh remain read failures without implicit starts', async () => {
  sessionStorage.setItem(key, JSON.stringify({ meetingId: 'meeting_missing' }));
  readOverride = path => path === '/api/meetings/meeting_missing' ? Promise.reject(new Error('Gone')) : undefined;
  const view = render(<LiveWorkspace />); await screen.findByText('Local service connected');
  expect(screen.queryByRole('button', { name: 'Export JSON' })).not.toBeInTheDocument(); view.unmount();
  let listings = 0; readOverride = path => path === '/api/meetings' && ++listings > 1 ? Promise.reject(new Error('Temporary listing failure')) : undefined;
  await mount(); expect(api.sendCommand).not.toHaveBeenCalled();
});

test('public terms survive both follow-up and same-run clarification', async () => {
  model.snapshot.brief.public_research_terms = [{ id: 'term_one', text: 'approved public phrase' }]; model.snapshot.run.status = 'awaiting_input';
  await mount(); await click('Answer and confirm brief');
  expect(screen.getByLabelText(/Approved public research terms/)).toHaveValue('approved public phrase');
  await click('Confirm updated brief'); await click('Create a linked follow-up');
  expect(screen.getByLabelText(/Approved public research terms/)).toHaveValue('approved public phrase');
});

test.each(['meeting_id', 'id', 'current', 'absent'])('reconciliation resolves only a server-scoped meeting: %s', async kind => {
  receipt = { intent: 'draft:saved', path: '/api/meetings/saved/draft' };
  const result = kind === 'meeting_id' ? { meeting_id: model.meeting.id } : kind === 'id' ? { id: model.meeting.id } : undefined;
  vi.mocked(api.reconcileCommand).mockImplementation(async () => { receipt = null; if (kind === 'absent') readOverride = path => path === '/api/meetings' ? { items: [], next_cursor: null } : undefined; return { confirmed: true, result }; });
  if (kind === 'absent') { render(<LiveWorkspace />); await screen.findByText('Action not confirmed'); }
  else await mount(false);
  await click('Reconcile pending action'); await screen.findByText('The original action has been reconciled with saved server state.');
});

test('late autosave rejection after changing scope cannot replace the new draft with an error', async () => {
  await mount(false); vi.useFakeTimers(); const saving = deferred<any>(); vi.mocked(api.sendCommand).mockReturnValueOnce(saving.promise);
  edit('Comparison title', 'A pending save'); await act(async () => { await vi.advanceTimersByTimeAsync(701); });
  await click(/New comparison/); await act(async () => saving.reject(new Error('Old draft failed')));
  expect(screen.queryByText('Old draft failed')).not.toBeInTheDocument();
  expect(screen.getByLabelText('Comparison title')).toHaveValue('');
});

test.each([false,true])('unsaved navigation with invalid=%s retains edits until explicit discard',async invalid=>{
 const second={...copy(model.meeting),id:'meeting_second',title:'Other comparison',runs:[]};
 readOverride=path=>path==='/api/meetings'?{items:[copy(model.meeting),second],next_cursor:null}:undefined;
 await mount(false);vi.spyOn(window,'confirm').mockReturnValue(false);edit('Comparison title',invalid?'':'Unsaved local title');
 await click(/Other comparison/);expect(screen.getByLabelText('Comparison title')).toHaveValue(invalid?'':'Unsaved local title');expect(api.sendCommand).not.toHaveBeenCalled();
 await click(/New comparison/);expect(screen.getByLabelText('Comparison title')).toHaveValue(invalid?'':'Unsaved local title');
 const leaving=new Event('beforeunload',{cancelable:true});window.dispatchEvent(leaving);expect(leaving.defaultPrevented).toBe(true);
 vi.spyOn(window,'confirm').mockReturnValue(true);await click(/Other comparison/);expect(screen.getByRole('heading',{level:1})).toHaveTextContent('Other comparison');
 const clean=new Event('beforeunload',{cancelable:true});window.dispatchEvent(clean);expect(clean.defaultPrevented).toBe(false);
});

test('older review pages deduplicate and remain available after refreshing saved meeting metadata',async()=>{
 const current=copy(model.snapshot.run);model.meeting.runs=Array.from({length:20},(_,index)=>({...current,id:`run_history_${index}`,created_at:new Date(Date.UTC(2026,0,31-index)).toISOString()}));model.meeting.next_run_cursor='run_history_19';
 const oldFirst={...current,id:'run_history_20',created_at:'2025-12-01T00:00:00Z'};const oldSecond={...current,id:'run_history_21',created_at:'2025-11-01T00:00:00Z'};
 readOverride=path=>path.endsWith('/runs?cursor=run_history_19')?{items:[model.meeting.runs[19],oldFirst],next_cursor:'run_history_20'}:path.endsWith('/runs?cursor=run_history_20')?{items:[oldFirst,oldSecond],next_cursor:null}:undefined;
 await mount();await click('Load earlier reviews');await click('Load earlier reviews');
 let history=screen.getByRole('heading',{name:'Review history'}).parentElement!;expect(within(history).getAllByRole('button')).toHaveLength(22);expect(screen.queryByRole('button',{name:'Load earlier reviews'})).not.toBeInTheDocument();
 await click('Edit draft');await click('Save draft');await screen.findByText('Draft saved on the local service. No adviser was called.');
 history=screen.getByRole('heading',{name:'Review history'}).parentElement!;expect(within(history).getAllByRole('button')).toHaveLength(22);
 const older=within(history).getAllByRole('button').at(-1)!;fireEvent.click(older);await waitFor(()=>expect(api.readApi).toHaveBeenCalledWith('/api/runs/run_history_21/snapshot'));
});

test('lost private inputs can be safely abandoned before entering a replacement', async () => {
  receipt = { id: 'original', intent: 'draft:saved', path: '/api/meetings/saved/draft' };
  await mount(false);
  expect(screen.getByRole('button', { name: 'Save draft' })).toBeDisabled();
  await click('Abandon unconfirmed action');
  await screen.findByText(/original action was safely abandoned/);
  expect(api.abandonPendingCommand).toHaveBeenCalledOnce();
  expect(api.sendCommand).not.toHaveBeenCalled();
  expect(screen.getByRole('button', { name: 'Save draft' })).toBeEnabled();
});

test('public research validity follows the selected draft after an invalid edit', async () => {
  const second = { ...copy(model.meeting), id: 'meeting_clean', title: 'Valid second draft', runs: [] };
  second.draft.public_research_terms = [];
  readOverride = path => path === '/api/meetings' ? { items: [copy(model.meeting), second], next_cursor: null } : undefined;
  await mount(false);
  edit(/Approved public research terms/, Array.from({ length: 9 }, (_, i) => `term ${i}`).join('\n'));
  expect(screen.getByLabelText(/Approved public research terms/)).toBeInvalid();
  await click(/Valid second draft/);
  const input = screen.getByLabelText(/Approved public research terms/) as HTMLTextAreaElement;
  expect(input).toHaveValue(''); expect(input).toBeValid(); expect(input.form?.checkValidity()).toBe(true);
});

test('saved currencies label editable prices and new candidate components without conversion', async () => {
  model.meeting.draft.currency = 'EUR';
  model.meeting.draft.options.forEach((item: any) => item.cost_components.forEach((cost: any) => { cost.currency = 'EUR'; }));
  await mount(false);
  expect(screen.getByLabelText('Budget (EUR)')).toHaveValue(model.meeting.draft.budget);
  expect(screen.queryByLabelText('Budget (USD)')).not.toBeInTheDocument();
  const first = screen.getByRole('group', { name: 'Candidate 1' });
  fireEvent.change(within(first).getByLabelText('Upfront purchase (EUR)'), { target: { value: '123.456' } });
  await click(/Add candidate option/); await click('Save draft');
  const saved = vi.mocked(api.sendCommand).mock.calls.find(call => call[1].endsWith('/draft'))![2].brief as api.DraftBrief;
  expect(saved.currency).toBe('EUR'); expect(saved.options[0].cost_components[0].amount).toBe('123.456');
  expect(saved.options.flatMap(item => item.cost_components).every(cost => cost.currency === 'EUR')).toBe(true);
  expect(validateDraft(saved)).toBe(true);
});

test('editing manual requirements preserves numeric constraints and their stable identities', async () => {
  const numeric = { id: 'minimum_project_spend', description: 'Spend at least 1200 on the selected approach', origin: 'user_supplied', metric: 'total_cost', operator: 'gte', threshold: '1200' };
  model.meeting.draft.hard_requirements.push(numeric);
  const before = copy(model.meeting.draft.hard_requirements);
  await mount(false);
  expect(screen.getByRole('heading', { name: 'Saved numeric requirements' })).toBeVisible();
  const manual = screen.getByLabelText(/Hard requirements —/) as HTMLTextAreaElement;
  expect(manual.value).not.toContain(numeric.description);
  edit(/Hard requirements —/, `${manual.value}\nKeep the workshop open`); await click('Save draft');
  const saved = vi.mocked(api.sendCommand).mock.calls.find(call => call[1].endsWith('/draft'))![2].brief as api.DraftBrief;
  expect(saved.hard_requirements).toEqual(expect.arrayContaining(before));
  expect(saved.hard_requirements.find(item => item.id === numeric.id)).toEqual(numeric);
  expect(saved.hard_requirements.at(-1)).toMatchObject({ description: 'Keep the workshop open', metric: 'manual', operator: 'manual', threshold: null });
  expect(new Set(saved.hard_requirements.map(item => item.id)).size).toBe(saved.hard_requirements.length);
  expect(validateDraft(saved)).toBe(true);
});

test.each(['not_applicable', 'excluded'])('a %s cost needs an explicit inclusion choice before its new price can be saved', async status => {
  model.meeting.draft.options[0].cost_components[0].status = status;
  model.meeting.draft.options[0].cost_components[0].amount = '0';
  await mount(false);
  const first = screen.getByRole('group', { name: 'Candidate 1' });
  const amount = within(first).getByLabelText('Upfront purchase (USD)');
  const treatment = within(first).getByLabelText('Upfront purchase treatment');
  expect(amount).toBeDisabled(); expect(treatment).toHaveValue(status);
  fireEvent.change(treatment, { target: { value: 'included' } });
  expect(amount).toBeEnabled(); fireEvent.change(amount, { target: { value: '9000' } });
  await click('Save draft');
  const saved = vi.mocked(api.sendCommand).mock.calls.find(call => call[1].endsWith('/draft'))![2].brief as api.DraftBrief;
  expect(saved.options[0].cost_components[0]).toMatchObject({ amount: '9000', status: 'included', unknown_reason: null });
  expect(validateDraft(saved)).toBe(true);
});

test.each(['follow-up', 'clarification'])('late autosave cannot overwrite a frozen %s editor', async mode => {
  const reviewed = copy(model.snapshot.brief); reviewed.title = 'Frozen reviewed brief'; model.snapshot.brief = reviewed;
  if (mode === 'clarification') model.snapshot.run.status = 'awaiting_input';
  sessionStorage.setItem(key, JSON.stringify({ meetingId: model.meeting.id, runId: null }));
  render(<LiveWorkspace />); await screen.findByDisplayValue(model.meeting.draft.title);
  vi.useFakeTimers(); const saving = deferred<any>(); vi.mocked(api.sendCommand).mockReturnValueOnce(saving.promise);
  edit('Comparison title', 'Edited working draft'); await act(async () => { await vi.advanceTimersByTimeAsync(701); });
  expect(api.sendCommand).toHaveBeenCalledOnce();
  fireEvent.click(document.querySelector('.live-history button')!); await act(async () => {});
  await click(mode === 'follow-up' ? 'Create a linked follow-up' : 'Answer and confirm brief');
  expect(screen.getByLabelText('Comparison title')).toHaveValue('Frozen reviewed brief');
  await act(async () => saving.resolve({ meeting_id: model.meeting.id, revision: 2, brief: { ...model.meeting.draft, title: 'Edited working draft', revision: 2 } }));
  expect(screen.getByLabelText('Comparison title')).toHaveValue('Frozen reviewed brief');
  if (mode === 'follow-up') expect(screen.getByText('Unsaved changes')).toBeVisible();
  await click(mode === 'follow-up' ? 'Save linked follow-up draft' : 'Confirm updated brief');
  expect(vi.mocked(api.sendCommand).mock.calls.at(-1)?.[2]).toMatchObject({ brief: { title: 'Frozen reviewed brief' } });
  if (mode === 'follow-up') expect(vi.mocked(api.sendCommand).mock.calls.at(-1)?.[2]).toMatchObject({ parent_run_id: model.snapshot.run.id, expected_draft_revision: 2 });
});

test('review-history changes reset unsaved choice and outcome text and require a current option', async () => {
  const second = copy(model.snapshot); second.run.id = 'run_other_history'; second.run.created_at = '2026-11-15T12:00:00Z'; second.report.run_id = second.run.id; second.report.id = 'report_other_history';
  second.report.comparison.find((item: any) => item.option_id === 'option_a').option_id = 'option_replacement';
  second.brief.options.find((item: any) => item.id === 'option_a').id = 'option_replacement';
  model.meeting.runs.push(second.run);
  readOverride = path => path === `/api/runs/${second.run.id}/snapshot` ? second : undefined;
  await mount(); edit('Chosen approach', 'option_a'); edit('Why you chose it', 'Only for the first review'); edit('What happened afterwards?', 'First review outcome');
  fireEvent.click(document.querySelectorAll('.live-history button')[1]); await act(async () => {});
  await waitFor(() => expect(screen.getByLabelText('Chosen approach').querySelector('option[value="option_replacement"]')).not.toBeNull());
  expect(screen.getByLabelText('Chosen approach')).toHaveValue(''); expect(screen.getByLabelText('Why you chose it')).toHaveValue(''); expect(screen.getByLabelText('What happened afterwards?')).toHaveValue('');
  expect(screen.getByRole('button', { name: 'Save my choice' })).toBeDisabled(); expect(screen.getByRole('button', { name: 'Save outcome' })).toBeDisabled();
  edit('Chosen approach', 'option_replacement'); await click('Save my choice');
  expect(vi.mocked(api.sendCommand).mock.calls.at(-1)?.[2]).toMatchObject({ run_id: second.run.id, reason: expect.not.stringContaining('first review'), observed_outcome: '' });
});

test.each([['JPY', '1000.000000', '1,000'], ['KWD', '1.005000', '1.005'], ['CLF', '1.2', '1.2000'], ['USD', '9007199254740993.01', '9,007,199,254,740,993.01']])('report money preserves exact %s minor units', async (currency, total, exact) => {
  model.snapshot.report.comparison[0].currency = currency; model.snapshot.report.comparison[0].total = total;
  await mount();
  const row = screen.getByRole('rowheader', { name: new RegExp(model.snapshot.report.comparison[0].label) }).closest('tr')!;
  const rendered = within(row).getAllByRole('cell')[0].textContent!;
  expect(rendered).toContain(exact);
  if (currency === 'JPY') expect(rendered).not.toContain('.');
});

test('a terminal report can explicitly discard its remaining retry without deleting history', async () => {
  model.snapshot.retry_eligible = true; model.snapshot.retryable_stages = ['synthesis'];
  vi.mocked(api.sendCommand).mockImplementation(async (_intent, path) => {
    if (path.endsWith('/cancel')) { model.snapshot.retry_eligible = false; model.snapshot.retryable_stages = []; model.snapshot.run.version++; }
    return copy(model.snapshot.run) as never;
  });
  await mount(); await click('Discard remaining retry');
  await screen.findByText(/Unused retry allowance released/);
  expect(api.sendCommand).toHaveBeenCalledWith(expect.stringContaining('cancel:'), expect.stringContaining('/cancel'), { expected_run_version: 6 });
  expect(screen.getByRole('button', { name: 'Export JSON' })).toBeVisible();
  expect(screen.queryByRole('button', { name: 'Retry synthesis' })).not.toBeInTheDocument();
});

test.each(['cancel_requested', 'cancelled', 'failed'])('saved-state integrity loss hides a stale current report when %s', async status => {
  model.snapshot.run.status = status;
  model.snapshot.run.execution_mode = 'live';
  model.snapshot.run.report_unavailable_reason = 'saved_state_integrity_failed';
  model.snapshot.report.recommendation = { option_id: 'option_a', summary: 'Stale private recommendation' };
  sessionStorage.setItem(key, JSON.stringify({ meetingId: model.meeting.id, runId: model.snapshot.run.id }));
  render(<LiveWorkspace />);
  await screen.findByRole('heading', { name: 'The current report is unavailable.' });
  expect(screen.getByText(/Saved review state could not be validated/)).toBeInTheDocument();
  expect(screen.getByText(/Already submitted work may still incur charges/)).toBeInTheDocument();
  expect(screen.queryByText(/Saved usage could not be validated/)).not.toBeInTheDocument();
  expect(screen.queryByText('Stale private recommendation')).not.toBeInTheDocument();
  expect(screen.queryByText('The saved report is being prepared.')).not.toBeInTheDocument();
  expect(screen.queryByRole('button', { name: 'Export JSON' })).not.toBeInTheDocument();
  expect(api.sendCommand).not.toHaveBeenCalled();
});

test('saved-state fence explains evidence unavailable without claiming a report was created', async () => {
  model.snapshot.run.status = 'cancelled';
  model.snapshot.run.report_unavailable_reason = 'saved_state_integrity_failed';
  model.snapshot.report = null;
  model.snapshot.evidence = null;
  sessionStorage.setItem(key, JSON.stringify({ meetingId: model.meeting.id, runId: model.snapshot.run.id }));
  render(<LiveWorkspace />);
  await screen.findByRole('heading', { name: 'The current report is unavailable.' });
  expect(screen.getByText(/This review is labeled simulated/)).toBeInTheDocument();
  expect(screen.getByRole('heading', { name: 'Stored evidence is unavailable.' })).toBeInTheDocument();
  expect(screen.queryByText(/when the report was created/)).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole('tab', { name: 'Evidence' }));
  expect(screen.getByText('Stored evidence could not be validated. The original records are preserved for review.')).toBeInTheDocument();
});

test('a newer saved-state-integrity snapshot replaces the old current report on refresh', async () => {
  model.snapshot.run.status = 'running';
  model.snapshot.report.recommendation = { option_id: 'option_a', summary: 'Previously saved recommendation' };
  await mount();
  expect(screen.getByText('Previously saved recommendation')).toBeInTheDocument();
  model.snapshot.run.status = 'failed';
  model.snapshot.run.version += 1;
  model.snapshot.run.report_unavailable_reason = 'saved_state_integrity_failed';
  model.snapshot.report = null;
  await screen.findByRole('heading', { name: 'The current report is unavailable.' }, { timeout: 3000 });
  expect(screen.queryByText('Previously saved recommendation')).not.toBeInTheDocument();
  expect(screen.queryByRole('button', { name: 'Export JSON' })).not.toBeInTheDocument();
});

test('combined ledger and saved-state failures preserve the accounting warning', async () => {
  model.snapshot.run.status = 'cancelled';
  model.snapshot.run.execution_mode = 'live';
  model.snapshot.run.report_unavailable_reason = 'saved_state_integrity_failed';
  model.snapshot.run.remaining_limits = { ...model.snapshot.run.remaining_limits, requests: null, requests_unavailable_reason: 'ledger_integrity_failed' };
  model.snapshot.report = null;
  model.snapshot.evidence = null;
  sessionStorage.setItem(key, JSON.stringify({ meetingId: model.meeting.id, runId: model.snapshot.run.id }));
  render(<LiveWorkspace />);
  await screen.findByRole('heading', { name: 'The current report is unavailable.' });
  expect(screen.getByText(/Saved usage could not be validated/)).toBeInTheDocument();
  expect(screen.getByText(/Already submitted work may still incur charges/)).toBeInTheDocument();
  expect(screen.getByText(/Saved review state could not be validated/)).toBeInTheDocument();
});

test('a newly unavailable report removes prior assessment actions', async () => {
  model.snapshot.report.simulated = false;
  const target = { target_type: 'option_costs', option_id: 'option_a', requirement_id: null, target_hash: 'a'.repeat(64), label: 'Saved option scope', reference_ids: ['evidence_quote_a'] };
  readOverride = path => path.endsWith('/assessments') ? { targets: [target] } : undefined;
  await mount();
  fireEvent.click(screen.getByRole('tab', { name: 'Evidence' }));
  await screen.findByText('Record your evidence check');
  model.snapshot.run.status = 'failed';
  model.snapshot.run.version += 1;
  model.snapshot.run.report_unavailable_reason = 'saved_state_integrity_failed';
  model.snapshot.report = null;
  const history = screen.getByRole('heading', { name: 'Review history' }).closest('section')!;
  fireEvent.click(within(history).getAllByRole('button')[0]);
  await screen.findByRole('heading', { name: 'The current report is unavailable.' });
  fireEvent.click(screen.getByRole('tab', { name: 'Evidence' }));
  expect(screen.queryByText('Record your evidence check')).not.toBeInTheDocument();
  expect(api.sendCommand).not.toHaveBeenCalled();
});

test('saved-state warning stays visible when an active review fails while Activity is selected', async () => {
  model.snapshot.run.status = 'running';
  model.snapshot.run.execution_mode = 'live';
  model.snapshot.report.recommendation = { option_id: 'option_a', summary: 'Previously saved recommendation' };
  await mount();
  fireEvent.click(screen.getByRole('tab', { name: 'Activity' }));
  expect(screen.getByRole('heading', { name: 'The saved review, as it happened.' })).toBeInTheDocument();
  model.snapshot.run.status = 'failed';
  model.snapshot.run.version += 1;
  model.snapshot.run.report_unavailable_reason = 'saved_state_integrity_failed';
  model.snapshot.report = null;
  await screen.findByRole('heading', { name: 'The current report is unavailable.' }, { timeout: 3000 });
  expect(screen.getByRole('tab', { name: 'Activity' })).toHaveAttribute('aria-selected', 'true');
  expect(screen.getByText(/Saved review state could not be validated/)).toBeInTheDocument();
  expect(screen.getByText(/Already submitted work may still incur charges/)).toBeInTheDocument();
  expect(screen.queryByRole('button', { name: 'Export JSON' })).not.toBeInTheDocument();
});

test('accounting warning stays visible when an active review fails while Activity is selected', async () => {
  model.snapshot.run.status = 'running';
  model.snapshot.run.execution_mode = 'live';
  await mount();
  fireEvent.click(screen.getByRole('tab', { name: 'Activity' }));
  model.snapshot.run.status = 'failed';
  model.snapshot.run.version += 1;
  model.snapshot.run.remaining_limits = { ...model.snapshot.run.remaining_limits, requests: null, requests_unavailable_reason: 'ledger_integrity_failed' };
  model.snapshot.report = null;
  await screen.findByRole('heading', { name: 'The current report is unavailable.' }, { timeout: 3000 });
  expect(screen.getByRole('tab', { name: 'Activity' })).toHaveAttribute('aria-selected', 'true');
  expect(screen.getByText(/Saved usage could not be validated/)).toBeInTheDocument();
  expect(screen.getByText(/Already submitted work may still incur charges/)).toBeInTheDocument();
  expect(screen.queryByRole('button', { name: 'Export JSON' })).not.toBeInTheDocument();
});

test.each(['saved-state', 'ledger'])('%s fence closes an open report inspection and exposes its warning', async kind => {
  model.snapshot.run.status = 'running';
  await mount();
  await click(/Inspect Option A.*calculation/);
  expect(screen.getByRole('dialog')).toHaveTextContent('Known subtotal:');
  model.snapshot.run.status = 'failed';
  model.snapshot.run.version += 1;
  if (kind === 'saved-state') model.snapshot.run.report_unavailable_reason = 'saved_state_integrity_failed';
  else model.snapshot.run.remaining_limits = { ...model.snapshot.run.remaining_limits, requests: null, requests_unavailable_reason: 'ledger_integrity_failed' };
  model.snapshot.report = null;
  await screen.findByRole('heading', { name: 'The current report is unavailable.' }, { timeout: 3000 });
  await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
});

test.each(['saved-state', 'ledger'])('%s fence ignores an in-flight report inspection', async kind => {
  model.snapshot.run.status = 'running';
  const calculation = copy(model.snapshot.report.calculations[0]);
  const pendingRead = deferred<any>();
  readOverride = path => path.includes('/calculations/') ? pendingRead.promise : undefined;
  await mount();
  fireEvent.click(screen.getByRole('button', { name: /Inspect Option A.*calculation/ }));
  await waitFor(() => expect(api.readApi).toHaveBeenCalledWith(expect.stringContaining('/calculations/')));
  model.snapshot.run.status = 'failed';
  model.snapshot.run.version += 1;
  if (kind === 'saved-state') model.snapshot.run.report_unavailable_reason = 'saved_state_integrity_failed';
  else model.snapshot.run.remaining_limits = { ...model.snapshot.run.remaining_limits, requests: null, requests_unavailable_reason: 'ledger_integrity_failed' };
  model.snapshot.report = null;
  await screen.findByRole('heading', { name: 'The current report is unavailable.' }, { timeout: 3000 });
  await act(async () => { pendingRead.resolve(calculation); await pendingRead.promise; });
  expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
});

test.each(['saved-state', 'ledger'])('%s fence still allows a fresh validated source inspection', async kind => {
  model.snapshot.run.status = 'failed';
  if (kind === 'saved-state') model.snapshot.run.report_unavailable_reason = 'saved_state_integrity_failed';
  else model.snapshot.run.remaining_limits = { ...model.snapshot.run.remaining_limits, requests: null, requests_unavailable_reason: 'ledger_integrity_failed' };
  model.snapshot.report = null;
  sessionStorage.setItem(key, JSON.stringify({ meetingId: model.meeting.id, runId: model.snapshot.run.id }));
  render(<LiveWorkspace />);
  await screen.findByRole('heading', { name: 'The current report is unavailable.' });
  fireEvent.click(screen.getByRole('tab', { name: 'Evidence' }));
  fireEvent.click(screen.getAllByRole('button', { name: 'Open exact passage & provenance' })[0]);
  expect(await screen.findByRole('dialog')).toHaveTextContent('Passage SHA-256');
});

test.each(['saved-state', 'ledger'])('%s fence ignores an earlier in-flight source inspection', async kind => {
  model.snapshot.run.status = 'running';
  const passage = copy(model.snapshot.evidence[0]);
  const pendingRead = deferred<any>();
  readOverride = path => path.includes('/sources/') ? pendingRead.promise : undefined;
  await mount();
  fireEvent.click(screen.getByRole('tab', { name: 'Evidence' }));
  fireEvent.click(screen.getAllByRole('button', { name: 'Open exact passage & provenance' })[0]);
  await waitFor(() => expect(api.readApi).toHaveBeenCalledWith(expect.stringContaining('/sources/')));
  model.snapshot.run.status = 'failed';
  model.snapshot.run.version += 1;
  if (kind === 'saved-state') model.snapshot.run.report_unavailable_reason = 'saved_state_integrity_failed';
  else model.snapshot.run.remaining_limits = { ...model.snapshot.run.remaining_limits, requests: null, requests_unavailable_reason: 'ledger_integrity_failed' };
  model.snapshot.report = null;
  await screen.findByRole('heading', { name: 'The current report is unavailable.' }, { timeout: 3000 });
  await act(async () => { pendingRead.resolve(passage); await pendingRead.promise; });
  expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
});
