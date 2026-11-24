import { Blob as NodeBlob } from 'node:buffer';
import { act, fireEvent, render, screen, within } from '@testing-library/react';
import { afterEach, beforeEach, expect, test, vi } from 'vitest';
import Workspace from '../app/workspace';
import { sample, validateDraft } from '../app/contracts';

const STORAGE_KEY = 'round-robin:fictional-walkthrough:v1';
const original = structuredClone(sample);
const button = (name: string | RegExp) => screen.getByRole('button', { name });
const click = (name: string | RegExp) => fireEvent.click(button(name));
const change = (name: string | RegExp, value: string) => fireEvent.change(screen.getByLabelText(name), { target: { value } });
const draft = () => click('Edit a comparison');
const choose = (id: string) => change('Prepared fictional scenarios', id);
const review = () => click(/Review preflight/);
const begin = () => {
  review();
  fireEvent.click(screen.getByRole('checkbox', { name: /I understand/ }));
  click(/Start simulated review/);
};
const advance = async (ms: number) => { await act(async () => { await vi.advanceTimersByTimeAsync(ms); }); };
const finish = async () => { for (let n = 0; n < 5; n++) await advance(700); };
let blobs: Blob[];
beforeEach(() => {
  blobs = [];
  Object.defineProperty(HTMLDialogElement.prototype, 'showModal', { configurable: true, value: function (this: HTMLDialogElement) { this.open = true; } });
  Object.defineProperty(HTMLDialogElement.prototype, 'close', { configurable: true, value: function (this: HTMLDialogElement) { this.open = false; } });
  vi.stubGlobal('Blob', NodeBlob);
  vi.stubGlobal('URL', class extends URL {
    static createObjectURL(blob: Blob) { blobs.push(blob); return `blob:fictional-${blobs.length}`; }
    static revokeObjectURL = vi.fn();
  });
  vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {});
});
afterEach(() => { Object.assign(sample, structuredClone(original)); });

test('tab keyboard navigation, provenance, and calculations retain visible evidence boundaries', () => {
  render(<Workspace />);
  const overview = screen.getByRole('tab', { name: 'Overview' });
  fireEvent.keyDown(overview, { key: 'ArrowLeft' });
  expect(screen.getByRole('tab', { name: 'Activity' })).toHaveFocus();
  fireEvent.keyDown(screen.getByRole('tab', { name: 'Activity' }), { key: 'Home' });
  fireEvent.keyDown(overview, { key: 'End' });
  fireEvent.keyDown(screen.getByRole('tab', { name: 'Activity' }), { key: 'ArrowRight' });
  fireEvent.keyDown(overview, { key: 'x' });
  expect(overview).toHaveFocus();
  click('Evidence library');
  fireEvent.click(screen.getAllByRole('button', { name: /Open exact passage/ })[0]);
  expect(screen.getByRole('dialog')).toHaveTextContent('Passage SHA-256');
  click('Close details');
  fireEvent.click(screen.getAllByRole('button', { name: /Inspect inputs/ })[0]);
  expect(screen.getByRole('dialog')).toHaveTextContent('Inputs and provenance');
  fireEvent(screen.getByRole('dialog'), new Event('close'));
  expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
  click('Round Robin sample home');
  click(/Inspect the conditions/);
  expect(screen.getByRole('dialog')).toHaveTextContent('Readiness is separate from execution status');
  click('Close details');
  click(/Ready within this fictional scope/);
  expect(screen.getByRole('dialog')).toHaveTextContent('Ready within this fictional scope');
  expect(fetch).not.toHaveBeenCalled();
});

test.each([true, false])('quote brief clipboard success=%s exposes a useful result', async (success) => {
  const writeText = vi.fn(success ? async () => {} : async () => { throw new Error('denied'); });
  Object.defineProperty(navigator, 'clipboard', { value: { writeText }, configurable: true });
  render(<Workspace />);
  click('Open quote / site-check brief');
  await act(async () => { click(/Copy brief/); });
  expect(writeText).toHaveBeenCalledWith(sample.scenarios[0].report!.next_evidence[0].request_text);
  expect(screen.getByRole('dialog')).toHaveTextContent(success ? 'Copied to clipboard.' : 'Clipboard unavailable. Select and copy');
});

test('exports retain fictional labeling, exact data, and provenance and release download objects', async () => {
  vi.useFakeTimers();
  render(<Workspace />);
  click('Export JSON');
  click('Export Markdown');
  expect(JSON.parse(await blobs[0].text())).toEqual(sample.scenarios[0].report);
  const markdown = await blobs[1].text();
  expect(markdown).toContain('Fictional sample');
  expect(markdown).toContain(sample.scenarios[0].report!.calculations[0].formula);
  expect(markdown).toContain('SHA256');
  await advance(1000);
  expect(URL.revokeObjectURL).toHaveBeenCalledTimes(2);
  expect(fetch).not.toHaveBeenCalled();
});

test('all editable fields preserve unsaved state, schema validation and custom-report refusal', () => {
  render(<Workspace />); draft();
  change('Comparison title', ''); click('Save draft');
  expect(screen.getByRole('alert')).toHaveTextContent('Please correct the draft');
  change('Comparison title', 'Custom fictional comparison');
  change('What do you need to decide?', 'Choose a different fictional scenario');
  change('Budget (USD)', ''); change('Common cost horizon (months)', '');
  change('Deadline (optional)', '');
  change('A good outcome looks like', 'Preserve every unknown'); change('Location', 'Phoenix');
  review(); expect(screen.getByRole('alert')).toHaveTextContent('Save the current draft');
  change('Budget (USD)', '10.00'); change('Common cost horizon (months)', '12');
  change('Deadline (optional)', '2027-01-01'); click('Save draft');
  expect(screen.getByText('Draft saved for this session. No inference performed.')).toBeVisible();
  review(); expect(screen.getByRole('alert')).toHaveTextContent('no corresponding report');
  choose('base12'); review();
  expect(screen.getByRole('heading', { level: 1 })).toHaveTextContent('Review before you begin');
  click('Back to brief');
  expect(screen.getByRole('heading', { level: 1 })).toHaveTextContent('Start with what matters');
});

test('opted-in persistence saves validated input, scenario changes and explicit removal', () => {
  render(<Workspace />); draft();
  const consent = screen.getByRole('checkbox', { name: /Keep this fictional draft/ });
  fireEvent.click(consent); click('Save draft');
  expect(JSON.parse(localStorage.getItem(STORAGE_KEY)!).draft.revision).toBe(sample.scenarios[0].brief.revision + 1);
  expect(screen.getByText('Fictional draft saved in this browser. No inference performed.')).toBeVisible();
  choose('horizon24');
  expect(JSON.parse(localStorage.getItem(STORAGE_KEY)!).scenarioId).toBe('horizon24');
  fireEvent.click(consent);
  expect(localStorage.getItem(STORAGE_KEY)).toBeNull();
});

test('storage write and erase errors never imply durable save or deletion', () => {
  render(<Workspace />); draft();
  fireEvent.click(screen.getByRole('checkbox', { name: /Keep this fictional draft/ }));
  vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => { throw new Error('full'); });
  click('Save draft');
  expect(screen.getByRole('alert')).toHaveTextContent('will not survive a refresh');
  choose('horizon24');
  expect(screen.getByRole('alert')).toHaveTextContent('scenario is available in this session');
  vi.spyOn(Storage.prototype, 'removeItem').mockImplementation(() => { throw new Error('denied'); });
  fireEvent.click(screen.getByRole('checkbox', { name: /Keep this fictional draft/ }));
  expect(screen.getByRole('alert')).toHaveTextContent('Unable to clear browser storage');
});

test.each(['{', 'null', '[]', '{}', '{"draft":{}}', '{"draft":{},"scenarioId":5}'])('invalid browser record %s cannot replace the trusted sample', (raw) => {
  localStorage.setItem(STORAGE_KEY, raw);
  render(<Workspace />);
  expect(screen.getByRole('heading', { level: 1 })).toHaveTextContent(original.scenarios[0].brief.title);
  expect(screen.queryByText('Reopened the fictional draft saved in this browser.')).not.toBeInTheDocument();
});

test.each(['base12', 'unknown'])('validated saved draft restoration checks scenario membership: %s', (scenarioId) => {
  localStorage.setItem(STORAGE_KEY, JSON.stringify({ draft: sample.scenarios[0].brief, scenarioId }));
  render(<Workspace />);
  if (scenarioId === 'base12') {
    expect(screen.getByText('Reopened the fictional draft saved in this browser.')).toBeVisible();
    expect(screen.getByRole('checkbox', { name: /Keep this fictional draft/ })).toBeChecked();
  } else expect(screen.getByRole('heading', { level: 1 })).toHaveTextContent(original.scenarios[0].brief.title);
});

test('browser read failure presents the trusted original with a visible recovery explanation', () => {
  vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => { throw new Error('disabled'); });
  render(<Workspace />);
  expect(screen.getByRole('alert')).toHaveTextContent('Browser storage is unavailable');
});

test('expired preflight cannot start, including expiry between the timer render and click', () => {
  vi.useFakeTimers(); vi.setSystemTime(new Date('2026-01-01T00:00:00Z'));
  render(<Workspace />); draft(); review();
  expect(button(/Start simulated review/)).toBeDisabled();
  fireEvent.click(screen.getByRole('checkbox', { name: /I understand/ }));
  vi.setSystemTime(new Date('2026-01-01T00:11:00Z'));
  click(/Start simulated review/);
  expect(screen.getByRole('alert')).toHaveTextContent('preflight is expired');
  expect(screen.queryByRole('progressbar')).not.toBeInTheDocument();
});

test('expiry timer visibly disables the action and clamps remaining minutes to zero', async () => {
  vi.useFakeTimers(); render(<Workspace />); draft(); review();
  fireEvent.click(screen.getByRole('checkbox', { name: /I understand/ }));
  await advance(601000);
  expect(button(/Start simulated review/)).toBeDisabled();
  expect(screen.getByText('Expires in 0 minutes')).toBeVisible();
});

test('completed simulations save each selected scenario once and preserve linked originals', async () => {
  vi.useFakeTimers(); render(<Workspace />); draft(); choose('horizon24');
  click(/Open original →/); expect(screen.getByRole('heading', { level: 1 })).toHaveTextContent(sample.scenarios[0].brief.title);
  click(/Explore a changed-input follow-up/); begin(); await finish();
  expect(screen.getByText('Simulated report saved in this session. No provider requests were made.')).toBeVisible();
  fireEvent.click(screen.getByRole('tab', { name: 'Activity' }));
  expect(screen.getByText(/proposal excerpts below belong to the original/)).toBeVisible();
  click(/Open original report/);
  click('Decision overview');
  click('Follow-up: 24 months');
  click('Edit brief'); begin(); await finish();
  expect(screen.getAllByRole('button', { name: 'Follow-up: 24 months' })).toHaveLength(1);
  expect(fetch).not.toHaveBeenCalled();
});

test.each(['base12', 'horizon24'])('cancelled simulation %s waits for acknowledgement and reopens saved cancellation', async (scenarioId) => {
  vi.useFakeTimers(); render(<Workspace />); draft(); choose(scenarioId); begin();
  click('Stop simulation');
  expect(button('Stop not confirmed')).toBeDisabled();
  expect(screen.getByRole('progressbar')).toBeVisible();
  await advance(900);
  expect(screen.getByText(/Simulation cancellation confirmed/)).toBeVisible();
  click('Decision overview');
  expect(screen.getByRole('button', { name: /Readiness unavailable/ })).toBeVisible();
  click(/Readiness unavailable/);
  expect(screen.getByRole('dialog')).toHaveTextContent('Readiness unavailable');
  click('Close details'); click('Export Markdown');
  expect(await blobs[0].text()).toContain('Readiness: unavailable');
});

test.each(['base12', 'missing_installation'])('partial simulation %s keeps incomplete costs and no completed recommendation', async (scenarioId) => {
  vi.useFakeTimers(); render(<Workspace />); draft(); choose(scenarioId); begin();
  click(/Inspect a partial-result example/);
  expect(screen.getByText(/Partial-result fixture/)).toBeVisible();
  click('Decision overview');
  expect(screen.getByRole('button', { name: /Readiness unavailable/ })).toBeVisible();
  click('Export Markdown');
  expect(await blobs[0].text()).toContain('Readiness: unavailable');
});

test('missing installation preserves unknown totals, calculated subtotals, and inspection of missing inputs', async () => {
  vi.useFakeTimers(); render(<Workspace />); draft(); choose('missing_installation'); begin(); await finish();
  expect(screen.getByRole('button', { name: /Conditional decision/ })).toBeVisible();
  const report = sample.scenarios.find(s => s.id === 'missing_installation')!.report!;
  const missing = report.comparison.find(c => c.missing_components.length)!;
  click(`Inspect ${missing.label} cost calculation`);
  expect(screen.getByRole('dialog')).toHaveTextContent('Missing components');
  expect(screen.getByRole('dialog')).toHaveTextContent('subtotal is not an all-in total');
  click('Close details'); click('Evidence library');
  expect(screen.getAllByText(/unknown/).length).toBeGreaterThan(0);
  click('Export Markdown');
  expect(await blobs[0].text()).toContain('unknown');
});

test('nullable report and draft fields retain readable fallbacks rather than invented support', async () => {
  const scenario = sample.scenarios[0];
  const report = scenario.report!;
  report.next_evidence = [];
  report.dissent = [];
  report.recommendation = null;
  report.readiness = null;
  report.readiness_unavailable_reason = null;
  const calculation = report.calculations[0];
  calculation.horizon_months = null;
  calculation.output = null;
  scenario.brief.budget = null;
  scenario.brief.horizon_months = null;
  scenario.brief.deadline = null;
  expect(validateDraft(scenario.brief)).toBe(true);
  render(<Workspace />);
  click('Open quote / site-check brief');
  expect(screen.getByRole('dialog')).toHaveTextContent(report.next_action);
  click('Close details'); click('Evidence library');
  fireEvent.click(screen.getAllByRole('button', { name: /Inspect inputs/ })[0]);
  expect(screen.getByRole('dialog')).toHaveTextContent('Common cost basis');
  expect(screen.getByRole('dialog')).toHaveTextContent('Unknown');
  click('Close details'); click('Export Markdown');
  const markdown = await blobs[0].text();
  expect(markdown).toContain('No recommendation established.');
  expect(markdown).toContain('None recorded in this fixture.');
  click('Draft brief');
  expect(screen.getByLabelText('Budget (USD)')).toHaveValue('');
  expect(screen.getByLabelText('Common cost horizon (months)')).toHaveValue(null);
});
