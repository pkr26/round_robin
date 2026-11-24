import { beforeEach, expect, test, vi } from 'vitest';
import { webcrypto } from 'node:crypto';
import snapshotFixture from './fixtures/runtime-snapshot.json';
import adaptedHistoricalReport from './fixtures/historical-runtime-report.json';

const key = 'round-robin:pending-local-command:v1';
const identity = { owner_id: 'owner_alpha', profile: 'development', csrf_token: null };
const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } });
let api: typeof import('../app/api-client');
let fetcher: ReturnType<typeof vi.fn>;
beforeEach(async () => {
  vi.resetModules();
  api = await import('../app/api-client');
  fetcher = vi.fn(async (url: string) => String(url).endsWith('/auth/session') ? json(identity) : json({ ok: true }));
  vi.stubGlobal('fetch', fetcher);
  Object.defineProperty(URL, 'createObjectURL', { value: vi.fn(() => 'blob:private'), configurable: true });
  Object.defineProperty(URL, 'revokeObjectURL', { value: vi.fn(), configurable: true });
  vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {});
});

function deferred<T>() { let resolve!: (value: T) => void; const promise = new Promise<T>(done => { resolve = done; }); return { promise, resolve }; }
function file(name: string, bytes: string | Uint8Array, type = '') {
  const data = typeof bytes === 'string' ? new TextEncoder().encode(bytes) : bytes;
  return { name, size: data.byteLength, type, arrayBuffer: async () => data.buffer.slice(data.byteOffset, data.byteOffset + data.byteLength) } as File;
}

test('session bootstrap is shared and cached; forced refresh with CSRF is validated', async () => {
  const wait = deferred<Response>(); fetcher.mockReturnValueOnce(wait.promise);
  const first = api.refreshSession(); const second = api.refreshSession();
  wait.resolve(json({ ...identity, profile: 'invited_hosted', csrf_token: 'csrf-test' }));
  expect(await first).toEqual(await second);
  await api.refreshSession(); expect(fetcher).toHaveBeenCalledTimes(1);
  await api.readApi('/api/test', { method: 'PATCH' });
  expect(fetcher.mock.calls.at(-1)?.[1]).toMatchObject({ credentials: 'include', headers: { 'X-CSRF-Token': 'csrf-test' } });
});

test.each([null, {}, { ...identity, owner_id: '' }, { ...identity, profile: 'evil' }, { ...identity, csrf_token: 3 }])('reject malformed session contract %j', async (value) => {
  fetcher.mockResolvedValueOnce(json(value));
  await expect(api.refreshSession()).rejects.toThrow('unsupported contract');
});

test('session transport, forbidden response, and concurrent refresh fail safely', async () => {
  fetcher.mockRejectedValueOnce(new Error('secret upstream error'));
  await expect(api.refreshSession()).rejects.toThrow('unavailable');
  const listener = vi.fn(); const stop = api.onAccessChange(listener);
  fetcher.mockResolvedValueOnce(json({}, 403));
  await expect(api.refreshSession()).rejects.toMatchObject({ status: 403 }); expect(listener).toHaveBeenCalledTimes(1);
  stop();
  const wait = deferred<Response>(); fetcher.mockReturnValueOnce(wait.promise);
  const stale = api.refreshSession(true); await api.refreshSession(true);
  wait.resolve(json(identity)); await expect(stale).rejects.toThrow('access changed');
});

test('account change clears private inputs and emits access boundary', async () => {
  await api.refreshSession();
  const listener = vi.fn(); api.onAccessChange(listener);
  fetcher.mockResolvedValueOnce(json({ ...identity, owner_id: 'owner_beta' }));
  await expect(api.refreshSession(true)).rejects.toThrow('signed-in account changed');
  expect(listener).toHaveBeenCalledTimes(1);
  expect((await api.refreshSession()).owner_id).toBe('owner_beta');
});

test('logout accepts configured acknowledgements and fences access', async () => {
  fetcher.mockResolvedValueOnce(json({ ...identity, csrf_token: 'csrf' })).mockResolvedValueOnce(new Response(null, { status: 204 }));
  await api.signOut();
  expect(fetcher.mock.calls.at(-1)?.[1]).toMatchObject({ redirect: 'manual', headers: { 'X-CSRF-Token': 'csrf' } });
  fetcher.mockResolvedValueOnce(json(identity)).mockResolvedValueOnce(json({}, 500));
  await expect(api.signOut()).rejects.toThrow('not been confirmed');
});

test.each([401, 403, 422, 503])('HTTP %s errors remain typed and redact transport exceptions', async (status) => {
  fetcher.mockResolvedValueOnce(json({ detail: [{ msg: 'invalid field' }, { message: 'another' }, null, 4] }, status));
  await expect(api.readApi('/api/test')).rejects.toMatchObject({ status, uncertain: status >= 500 });
});

test('JSON parse failure and connection failure distinguish mutations', async () => {
  fetcher.mockResolvedValueOnce(new Response('not json'));
  await expect(api.readApi('/api/test')).rejects.toMatchObject({ uncertain: false });
  fetcher.mockRejectedValueOnce(new Error('private error'));
  await expect(api.readApi('/api/test')).rejects.toThrow('unavailable');
  await api.refreshSession();
  fetcher.mockRejectedValueOnce(new Error('private error'));
  await expect(api.readApi('/api/test', { method: 'PATCH' })).rejects.toMatchObject({ uncertain: true });
});

test.each(['/api/runs/run_a/snapshot', '/api/runs/run_a/reports/1', '/api/visual/capabilities', '/api/meetings/meeting_a/design-sessions', '/api/design-sessions/design_a', '/api/design-sessions/design_a/images', '/api/design-sessions/design_a/reviews', '/api/design-sessions/design_a/improvements', '/api/design-sessions/design_a/native-diagram', '/api/design-sessions/design_a/preflight'])('invalid typed response %s fails contract validation', async (path) => {
  await expect(api.readApi(path)).rejects.toThrow('unsupported');
});

test.each(['valid', 'unavailable'])('snapshot contract accepts a %s saved brief', async state => {
  const brief = state === 'valid' ? snapshotFixture.snapshot.brief : null;
  fetcher.mockResolvedValueOnce(json({ ...snapshotFixture.snapshot, brief, report: null, evidence: [], events: [], proposed_changes: [] }));
  await expect(api.readApi('/api/runs/run_a/snapshot')).resolves.toMatchObject({ brief });
});

test('snapshot contract accepts explicitly unavailable stored evidence', async () => {
  fetcher.mockResolvedValueOnce(json({ ...snapshotFixture.snapshot, brief: null, report: null, evidence: null, events: [], proposed_changes: [] }));
  await expect(api.readApi('/api/runs/run_a/snapshot')).resolves.toMatchObject({ evidence: null });
});

test('snapshot contract accepts unavailable ledger accounting without inventing a request count', async () => {
  const snapshot = structuredClone(snapshotFixture.snapshot);
  const run = { ...snapshot.run, status: 'cancelled', readiness: null,
    remaining_limits: { ...snapshot.run.remaining_limits, requests: null, requests_unavailable_reason: 'ledger_integrity_failed' } };
  fetcher.mockResolvedValueOnce(json({ ...snapshot, run, report: null, evidence: null, events: [], proposed_changes: [] }));
  const result = await api.readApi<{ run: { remaining_limits: { requests: number | null; requests_unavailable_reason?: string } }; report: unknown }>('/api/runs/run_a/snapshot');
  expect(result.run.remaining_limits).toMatchObject({ requests: null, requests_unavailable_reason: 'ledger_integrity_failed' });
  expect(result.report).toBeNull();
});

test('snapshot contract continues to accept older request counts without a reason field', async () => {
  const snapshot = structuredClone(snapshotFixture.snapshot);
  fetcher.mockResolvedValueOnce(json({ ...snapshot, report: null, evidence: [], events: [], proposed_changes: [] }));
  await expect(api.readApi('/api/runs/run_a/snapshot')).resolves.toMatchObject({ run: { remaining_limits: snapshot.run.remaining_limits } });
});

test('foreign late read is rejected after logout', async () => {
  await api.refreshSession(); const wait = deferred<Response>(); fetcher.mockReturnValueOnce(wait.promise);
  const read = api.readApi('/api/private');
  fetcher.mockResolvedValueOnce(new Response(null, { status: 204 })); await api.signOut();
  wait.resolve(json({ private: 'old owner' })); await expect(read).rejects.toThrow('access changed');
});

test('confirmed commands freeze payload, attach UUID and clear only their receipt', async () => {
  expect(await api.sendCommand('draft', '/api/meetings', { text: 'private', nested: { z: 2, a: 1 }, array: ['x'] })).toEqual({ ok: true });
  const request = fetcher.mock.calls.at(-1)?.[1] as RequestInit;
  expect(JSON.parse(String(request.body))).toMatchObject({ text: 'private', command_id: expect.any(String) });
  expect(api.pendingCommand()).toBeNull();
});

test('uncertain commands retain noncontent receipts, block changed input and replay the same UUID', async () => {
  fetcher.mockImplementation(async (url: string) => url.endsWith('/auth/session') ? json(identity) : json({ error: 'unconfirmed' }, 503));
  await expect(api.sendCommand('draft', '/api/meetings', { text: 'PRIVATE_SENTINEL' })).rejects.toMatchObject({ uncertain: true });
  const original = api.pendingCommand()!; expect(JSON.stringify(original)).not.toContain('PRIVATE_SENTINEL');
  await expect(api.sendCommand('different', '/api/meetings', {})).rejects.toThrow('Another action');
  await expect(api.sendCommand('draft', '/api/meetings', { text: 'changed' })).rejects.toThrow('inputs differ');
  fetcher.mockImplementation(async (url: string) => url.endsWith('/auth/session') ? json(identity) : json({ accepted: true }));
  expect(await api.retryPendingCommand()).toEqual({ accepted: true });
  expect(JSON.parse(String(fetcher.mock.calls.at(-1)?.[1]?.body)).command_id).toBe(original.id);
  expect(api.pendingCommand()).toBeNull(); expect(await api.retryPendingCommand()).toBeUndefined();
});

test.each(['/api/meetings/m/runs', '/api/meetings/m/preflight', '/api/runs/r/exports', '/api/runs/r/cancel', '/api/runs/r/retries', '/api/design-sessions/d/selection'])('only safe noncontent command fields can survive reload for %s', async path => {
  fetcher.mockImplementation(async (url: string) => url.endsWith('/auth/session') ? json(identity) : json({}, 503));
  await expect(api.sendCommand('safe', path, {})).rejects.toThrow();
  expect(api.pendingCommand()?.safeBody).toEqual({});
  vi.resetModules(); api = await import('../app/api-client');
  fetcher.mockImplementation(async (url: string) => url.endsWith('/auth/session') ? json(identity) : json({ accepted: true }));
  if (path.includes('/design-sessions/')) await expect(api.retryPendingCommand()).rejects.toThrow('unsupported');
  else expect(await api.retryPendingCommand()).toEqual({ accepted: true });
});

test('legacy content receipts are scrubbed; unreadable receipts fail closed', async () => {
  sessionStorage.setItem(key, JSON.stringify({ id: 'legacy', intent: 'draft', path: '/api/meetings', method: 'POST', body: { text: 'PRIVATE' } }));
  expect(api.pendingCommand()?.bodyHash).toBe('legacy_unverified'); expect(sessionStorage.getItem(key)).not.toContain('PRIVATE');
  await expect(api.retryPendingCommand()).rejects.toThrow('inputs differ');
  sessionStorage.setItem(key, '{'); expect(() => api.pendingCommand()).toThrow('cannot be read');
  sessionStorage.setItem(key, JSON.stringify({ id: 'bad', intent: 'draft', path: '/api/meetings' })); expect(() => api.pendingCommand()).toThrow();
});

test('reconciliation clears committed/gone, retains absent/unknown and propagates other failures', async () => {
  expect(await api.reconcileCommand()).toEqual({ confirmed: true });
  const receipt = { id: 'command_a', intent: 'save', path: '/api/meetings', bodyHash: 'hash', method: 'POST', ownerId: identity.owner_id };
  for (const [body, status, confirmed] of [[{}, 404, false], [{ outcome: 'pending' }, 200, false], [{ outcome: 'committed', result: { saved: true } }, 200, true], [{ outcome: 'gone' }, 200, true]] as const) {
    sessionStorage.setItem(key, JSON.stringify(receipt));
    fetcher.mockImplementation(async (url: string) => url.endsWith('/auth/session') ? json(identity) : json(body, status));
    expect((await api.reconcileCommand()).confirmed).toBe(confirmed);
  }
  sessionStorage.setItem(key, JSON.stringify(receipt)); fetcher.mockImplementation(async (url: string) => url.endsWith('/auth/session') ? json(identity) : json({}, 500));
  await expect(api.reconcileCommand()).rejects.toMatchObject({ status: 500 });
});

test.each(['reconcile', 'abandon'])('committed unavailable receipt clears pending UUID via %s without replay', async mode => {
  const pending = { id: 'command_a', intent: 'followup', path: '/api/meetings/meeting_a/follow-ups', bodyHash: 'hash', method: 'POST', ownerId: identity.owner_id };
  const unavailable = { receipt_unavailable: true, message: 'The saved result is unavailable.' };
  sessionStorage.setItem(key, JSON.stringify(pending));
  fetcher.mockImplementation(async (url: string) => String(url).endsWith('/auth/session') ? json(identity) :
    json(mode === 'reconcile' ? { outcome: 'committed', result: unavailable } : { status: 'committed', result: unavailable }));
  const result = mode === 'reconcile' ? await api.reconcileCommand() : await api.abandonPendingCommand();
  expect(result.result).toEqual(unavailable);
  expect(api.pendingCommand()).toBeNull();
  expect(fetcher.mock.calls.filter(([url]) => String(url).includes('/follow-ups'))).toHaveLength(0);
});

test('committed-but-unavailable retry keeps UUID until explicit reconciliation', async () => {
  const unavailable = { receipt_unavailable: true, message: 'The saved result is unavailable.' };
  fetcher.mockImplementation(async (url: string) => String(url).endsWith('/auth/session') ? json(identity) :
    String(url).includes('/follow-ups') ? json({ error: { code: 'committed_receipt_unavailable', command_outcome: 'committed' } }, 409) :
      json({ outcome: 'committed', result: unavailable }));
  await expect(api.sendCommand('followup', '/api/meetings/meeting_a/follow-ups', { private: 'synthetic' }))
    .rejects.toMatchObject({ status: 409, commandOutcome: 'committed' });
  expect(api.pendingCommand()).not.toBeNull();
  expect(await api.reconcileCommand()).toEqual({ confirmed: true, result: unavailable });
  expect(api.pendingCommand()).toBeNull();
  expect(fetcher.mock.calls.filter(([url]) => String(url).includes('/follow-ups'))).toHaveLength(1);
});

test('cross-owner receipt is never replayed or reconciled', async () => {
  sessionStorage.setItem(key, JSON.stringify({ id: 'foreign', intent: 'draft', path: '/api/meetings', bodyHash: 'hash', method: 'POST', ownerId: 'owner_other', safeBody: {} }));
  await expect(api.sendCommand('draft', '/api/meetings', {})).rejects.toThrow('different signed-in account');
  await expect(api.reconcileCommand()).rejects.toThrow('original account');
});

test('a reloaded private command requires reconciliation, and storage failure prevents mutation', async () => {
  sessionStorage.setItem(key, JSON.stringify({ id: 'private', intent: 'draft', path: '/api/meetings', bodyHash: 'hash', method: 'POST' }));
  await expect(api.retryPendingCommand()).rejects.toThrow('only in memory'); sessionStorage.clear();
  vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => { throw new Error('storage denied'); });
  await expect(api.sendCommand('draft', '/api/meetings', {})).rejects.toThrow('session storage');
  expect(fetcher).toHaveBeenCalledTimes(1);
});

test('reentrant send and definitive 4xx preserve unambiguous command semantics', async () => {
  const wait = deferred<Response>(); fetcher.mockReturnValueOnce(wait.promise);
  const first = api.sendCommand('draft', '/api/meetings', {});
  await expect(api.sendCommand('draft', '/api/meetings', {})).rejects.toThrow('already being sent');
  wait.resolve(json(identity)); fetcher.mockResolvedValueOnce(json({ error: { command_outcome: 'not_committed', code: 'validation_error' } }, 422));
  await expect(first).rejects.toMatchObject({ status: 422 }); expect(api.pendingCommand()).toBeNull();
});

test.each(['preflight_expired', 'draft_conflict'])('definitive %s on a retained retry retires the original receipt', async code => {
  fetcher.mockImplementation(async (url: string) => url.endsWith('/auth/session') ? json(identity) : json({}, 503));
  await expect(api.sendCommand('draft', '/api/meetings/m/draft', { brief: 'private' }, 'PATCH')).rejects.toThrow();
  const original = api.pendingCommand()!;
  fetcher.mockImplementation(async (url: string) => url.endsWith('/auth/session') ? json(identity) : json({ error: { code, message: 'Refresh inputs', command_outcome: 'not_committed' } }, 409));
  await expect(api.retryPendingCommand()).rejects.toMatchObject({ status: 409, commandOutcome: 'not_committed' });
  expect(JSON.parse(String(fetcher.mock.calls.at(-1)?.[1]?.body)).command_id).toBe(original.id);
  expect(api.pendingCommand()).toBeNull();
  fetcher.mockImplementation(async (url: string) => url.endsWith('/auth/session') ? json(identity) : json({ accepted: true }));
  await expect(api.sendCommand('new draft', '/api/meetings/m/draft', { brief: 'corrected input' }, 'PATCH')).resolves.toEqual({ accepted: true });
});

test.each([{}, { code: 'unknown', command_outcome: 'unknown' }, { code: 'command_conflict', command_outcome: 'not_committed' }])('retained retry keeps its receipt without proof the original was rejected: %j', async detail => {
  fetcher.mockImplementation(async (url: string) => url.endsWith('/auth/session') ? json(identity) : json({}, 503));
  await expect(api.sendCommand('draft', '/api/meetings', { private: 'input' })).rejects.toThrow();
  const id = api.pendingCommand()!.id;
  fetcher.mockImplementation(async (url: string) => url.endsWith('/auth/session') ? json(identity) : json({ error: detail }, 409));
  await expect(api.retryPendingCommand()).rejects.toThrow();
  expect(api.pendingCommand()?.id).toBe(id);
});

test('a reloaded private action remains fenced until the server confirms abandonment', async () => {
  fetcher.mockImplementation(async (url: string) => {
    if (url.endsWith('/auth/session')) return json(identity);
    throw new Error('request did not reach service');
  });
  await expect(api.sendCommand('draft', '/api/meetings', { private: 'DO_NOT_PERSIST' })).rejects.toThrow();
  const id = api.pendingCommand()!.id;
  expect(sessionStorage.getItem(key)).not.toContain('DO_NOT_PERSIST');
  vi.resetModules(); api = await import('../app/api-client');
  fetcher.mockImplementation(async (url: string) => url.endsWith('/auth/session') ? json(identity) : json({}, 404));
  expect(await api.reconcileCommand()).toEqual({ confirmed: false });
  await expect(api.retryPendingCommand()).rejects.toThrow('only in memory');
  expect(api.pendingCommand()?.id).toBe(id);
  for (const response of [json({}, 503), json({ status: 'unsupported', result: {} })]) {
    fetcher.mockImplementation(async (url: string) => url.endsWith('/auth/session') ? json(identity) : response);
    await expect(api.abandonPendingCommand()).rejects.toThrow();
    expect(api.pendingCommand()?.id).toBe(id);
  }
  fetcher.mockImplementation(async (url: string) => url.endsWith('/auth/session') ? json(identity) : json({ status: 'abandoned', result: {}, command_type: 'abandoned', resource_id: null }));
  await expect(api.abandonPendingCommand()).resolves.toEqual({ status: 'abandoned', result: {} });
  expect(fetcher.mock.calls.at(-1)).toEqual([`${api.API_URL}/api/commands/${id}/abandon`, expect.objectContaining({ method: 'POST' })]);
  expect(fetcher.mock.calls.at(-1)?.[1]?.body).toBeUndefined();
  expect(api.pendingCommand()).toBeNull();
  fetcher.mockImplementation(async (url: string) => url.endsWith('/auth/session') ? json(identity) : json({ accepted: true }));
  await expect(api.sendCommand('replacement', '/api/meetings', { private: 'new input' })).resolves.toEqual({ accepted: true });
});

test.each(['committed', 'gone'])('abandonment recovers %s without replaying or changing its original payload', async status => {
  sessionStorage.setItem(key, JSON.stringify({ id: 'original', intent: 'draft', path: '/api/meetings', bodyHash: 'hash', method: 'POST', ownerId: identity.owner_id }));
  const saved = status === 'committed' ? { id: 'meeting_saved' } : {};
  fetcher.mockImplementation(async (url: string) => url.endsWith('/auth/session') ? json(identity) : json({ status, result: saved }));
  expect(await api.abandonPendingCommand()).toEqual({ status, result: saved });
  expect(api.pendingCommand()).toBeNull();
  expect(fetcher.mock.calls.filter(call => !String(call[0]).endsWith('/auth/session'))).toHaveLength(1);
});

test('abandoned receipt reconciles after a lost abandonment acknowledgement; other owners cannot abandon it', async () => {
  const receipt = { id: 'original', intent: 'draft', path: '/api/meetings', bodyHash: 'hash', method: 'POST', ownerId: identity.owner_id };
  sessionStorage.setItem(key, JSON.stringify({ ...receipt, ownerId: 'owner_other' }));
  await expect(api.abandonPendingCommand()).rejects.toThrow('original account');
  expect(fetcher).toHaveBeenCalledTimes(1);
  sessionStorage.setItem(key, JSON.stringify(receipt));
  fetcher.mockImplementation(async (url: string) => url.endsWith('/auth/session') ? json(identity) : json({ outcome: 'abandoned', result: {} }));
  expect(await api.reconcileCommand()).toEqual({ confirmed: true, result: {} });
  expect(api.pendingCommand()).toBeNull();
});

test.each([file('bad.exe', 'text'), file('empty.txt', ''), { ...file('huge.txt', 'x'), size: 1024 * 1024 + 1 }, file('bad.txt', new Uint8Array([0xff])), file('binary.txt', 'x\0'), file('spaces.md', '  ')])('text uploads reject invalid file %j', async value => {
  await expect(api.uploadTextFile('meeting_a', 1, value)).rejects.toThrow(); expect(fetcher).not.toHaveBeenCalled();
});

test.each(['private🦊.md', 'private.txt'])('text upload canonicalizes newlines and sanitizes %s', async name => {
  await api.uploadTextFile('meeting_a', 1, file(name, 'one\r\ntwo\rthree'));
  expect(fetcher.mock.calls.at(-1)?.[1]).toMatchObject({ body: 'one\ntwo\nthree', headers: { 'X-Filename': name.replace(/[^a-zA-Z0-9_. -]/g, '_'), 'X-Content-Sha256': expect.stringMatching(/^[a-f0-9]{64}$/) } });
});

test.each([file('bad.gif', 'GIF', 'image/gif'), file('empty.png', ''), { ...file('big.png', 'x'), size: 10 * 1024 * 1024 + 1 }, file('unknown.bin', 'x')])('image upload rejects invalid type/size %j', async value => {
  await expect(api.uploadDesignImage('design_a', 1, value)).rejects.toThrow('PNG or JPEG'); expect(fetcher).not.toHaveBeenCalled();
});

test.each(['original.png', 'original.jpg'])('binary upload binds exact bytes for %s', async name => {
  // Contract validation still runs; the server fixture is intentionally invalid.
  await expect(api.uploadDesignImage('design_a', 1, file(name, 'pixels'))).rejects.toThrow('unsupported visual contract');
  expect(fetcher.mock.calls.at(-1)?.[1]).toMatchObject({ headers: { 'X-Content-Sha256': expect.stringMatching(/^[a-f0-9]{64}$/) } });
});

test('binary upload after reload requires exact reselected original bytes', async () => {
  await expect(api.uploadDesignImage('design_a', 1, file('original.png', 'pixels'))).rejects.toThrow();
  const receipt = api.pendingCommand()!; receipt.safeBody = { expected_revision: 1, sha256: (fetcher.mock.calls.at(-1)?.[1]?.headers as Record<string,string>)['X-Content-Sha256'], content_type: 'image/png' };
  sessionStorage.setItem(key, JSON.stringify(receipt)); vi.resetModules(); api = await import('../app/api-client');
  await expect(api.retryPendingCommand()).rejects.toThrow('Reselect');
});

test('native source download enforces bound and does not leak on revoked access', async () => {
  fetcher.mockResolvedValueOnce(json({}, 404)); await expect(api.downloadDesignSource('d', 1, 'svg')).rejects.toMatchObject({ status: 404 });
  fetcher.mockResolvedValueOnce({ ok: true, blob: async () => ({ size: 1024 * 1024 + 1 }) }); await expect(api.downloadDesignSource('d', 1, 'svg')).rejects.toThrow('download bound');
  await api.refreshSession(); const wait = deferred<Blob>(); fetcher.mockResolvedValueOnce({ ok: true, blob: () => wait.promise });
  const download = api.downloadDesignSource('d', 1, 'html'); await Promise.resolve();
  fetcher.mockResolvedValueOnce(new Response(null, { status: 204 })); await api.signOut(); wait.resolve(new Blob(['PRIVATE']));
  await expect(download).rejects.toThrow('access changed'); expect(URL.createObjectURL).not.toHaveBeenCalled();
});

test('completed source download uses local blob, fixed filename and revokes it', async () => {
  vi.useFakeTimers(); fetcher.mockResolvedValueOnce(new Response('<svg/>'));
  await api.downloadDesignSource('d', 2, 'svg'); expect(HTMLAnchorElement.prototype.click).toHaveBeenCalledTimes(1);
  await vi.advanceTimersByTimeAsync(1000); expect(URL.revokeObjectURL).toHaveBeenCalledWith('blob:private');
});

test.each(['pdf_font_unsupported', 'explicit failure', null])('failed export remains distinguishable: %s', async error => {
  fetcher.mockImplementation(async (url: string) => url.endsWith('/auth/session') ? json(identity) : json({ status: 'failed', error }));
  await expect(api.downloadSavedExport('run_a', 'report_a', 1, 'pdf')).rejects.toThrow(error === 'pdf_font_unsupported' ? 'font' : error ?? 'could not be created');
});

test('exports reject changed identity/version and failed download', async () => {
  const artifact = { id: 'artifact_a', status: 'ready', report_id: 'report_a', report_version: 1 };
  fetcher.mockImplementation(async (url: string) => url.endsWith('/auth/session') ? json(identity) : json({ ...artifact, report_version: 2 }));
  await expect(api.downloadSavedExport('run_a', 'report_a', 1, 'json')).rejects.toThrow('does not match');
  fetcher.mockImplementation(async (url: string) => url.endsWith('/auth/session') ? json(identity) : url.endsWith('/download') ? json({ error: { detail: 'gone' } }, 404) : json(artifact));
  await expect(api.downloadSavedExport('run_a', 'report_a', 1, 'json')).rejects.toThrow('gone');
});

test.each(['markdown', 'json'] as const)('export downloads exact saved %s through local blob', async format => {
  vi.useFakeTimers();
  fetcher.mockImplementation(async (url: string) => url.endsWith('/auth/session') ? json(identity) : url.endsWith('/download') ? new Response('PRIVATE') : json({ id: 'artifact_a', status: 'ready', report_id: 'report_a', report_version: 1, download_url: 'https://attacker.example/private' }));
  await api.downloadSavedExport('run_a', 'report_a', 1, format);
  expect(fetcher.mock.calls.at(-1)?.[0]).toBe(`${api.API_URL}/api/exports/artifact_a/download`);
  await vi.advanceTimersByTimeAsync(1000); expect(URL.revokeObjectURL).toHaveBeenCalled();
});

test('command hash yield cannot transfer private inputs across sign-out', async () => {
  const wait = deferred<ArrayBuffer>(); const digest = vi.spyOn(webcrypto.subtle, 'digest').mockReturnValueOnce(wait.promise);
  const command = api.sendCommand('draft', '/api/meetings', { private: 'owner_alpha text' });
  await vi.waitFor(() => expect(digest).toHaveBeenCalled());
  fetcher.mockResolvedValueOnce(new Response(null, { status: 204 })); await api.signOut();
  wait.resolve(new ArrayBuffer(32)); await expect(command).rejects.toThrow('access changed');
  expect(fetcher.mock.calls.filter(([url]) => String(url).includes('/api/meetings'))).toHaveLength(0);
});

test.each([401,403])('revoked access %s clears all clients on ordinary reads and native downloads',async status=>{
 await api.refreshSession();const listener=vi.fn();api.onAccessChange(listener);
 fetcher.mockResolvedValueOnce(json({},status));await expect(api.readApi('/api/private')).rejects.toMatchObject({status});expect(listener).toHaveBeenCalledTimes(1);
 fetcher.mockResolvedValueOnce(json({},status));await expect(api.downloadDesignSource('version_one',1,'html')).rejects.toMatchObject({status});expect(listener).toHaveBeenCalledTimes(2);
 fetcher.mockResolvedValueOnce(json({},status));await expect(api.readDesignSource('version_one','html')).rejects.toMatchObject({status});expect(listener).toHaveBeenCalledTimes(3);
});

test.each(['html','json'] as const)('native %s source is bounded and returned as inert text',async format=>{
 fetcher.mockResolvedValueOnce(new Response('exact private source'));expect(await api.readDesignSource('version_one',format)).toBe('exact private source');
 fetcher.mockResolvedValueOnce({ok:true,blob:async()=>({size:1024*1024+1})});await expect(api.readDesignSource('version_one',format)).rejects.toThrow('byte bound');
});

test('native source read is fenced during both blob and text awaits',async()=>{
 await api.refreshSession();const blobWait=deferred<Blob>();fetcher.mockResolvedValueOnce({ok:true,blob:()=>blobWait.promise});const first=api.readDesignSource('version_one','html');await Promise.resolve();
 fetcher.mockResolvedValueOnce(new Response(null,{status:204}));await api.signOut();blobWait.resolve(new Blob(['private']));await expect(first).rejects.toThrow('access changed');
 await api.refreshSession();const textWait=deferred<string>();fetcher.mockResolvedValueOnce({ok:true,blob:async()=>({size:5,text:()=>textWait.promise})});const second=api.readDesignSource('version_one','json');await Promise.resolve();await Promise.resolve();
 fetcher.mockResolvedValueOnce(new Response(null,{status:204}));await api.signOut();textWait.resolve('private');await expect(second).rejects.toThrow('access changed');
});

test.each([401,403])('export download HTTP%s invalidates access before reporting failure',async status=>{
 const listener=vi.fn();api.onAccessChange(listener);
 fetcher.mockImplementation(async(url:string)=>url.endsWith('/auth/session')?json(identity):url.endsWith('/download')?json({},status):json({id:'artifact_a',status:'ready',report_id:'report_a',report_version:1}));
 await expect(api.downloadSavedExport('run_a','report_a',1,'pdf')).rejects.toMatchObject({status});expect(listener).toHaveBeenCalledTimes(1);
});

test('a revoked in-flight export never becomes a clickable private download',async()=>{
 const wait=deferred<Blob>();let downloading=false;
 fetcher.mockImplementation(async(url:string)=>url.endsWith('/auth/session')?json(identity):url.endsWith('/auth/logout')?new Response(null,{status:204}):url.endsWith('/download')?{ok:true,blob:()=>{downloading=true;return wait.promise;}}:json({id:'artifact_a',status:'ready',report_id:'report_a',report_version:1}));
 const download=api.downloadSavedExport('run_a','report_a',1,'json');await vi.waitFor(()=>expect(downloading).toBe(true));await api.signOut();wait.resolve(new Blob(['private']));await expect(download).rejects.toThrow('access changed');expect(URL.createObjectURL).not.toHaveBeenCalled();
});

test('reselected upload preserves original UUID and revision after reload while refusing changed bytes',async()=>{
 await expect(api.uploadDesignImage('design_a',1,file('original.png','pixels'))).rejects.toThrow('unsupported visual contract');
 const original=api.pendingCommand()!;expect(original.safeBody).toMatchObject({expected_revision:1,content_type:'image/png'});
 expect(sessionStorage.getItem(key)).not.toContain('pixels');vi.resetModules();api=await import('../app/api-client');
 await expect(api.uploadDesignImage('design_a',99,file('original.png','pixels'))).rejects.toThrow('unsupported visual contract');
 expect(fetcher.mock.calls.at(-1)?.[1]).toMatchObject({headers:{'X-Command-Id':original.id,'X-Expected-Revision':'1'}});
 const posts=fetcher.mock.calls.filter(([,options])=>options?.method==='POST').length;
 await expect(api.uploadDesignImage('design_a',99,file('different.png','changed pixels'))).rejects.toThrow('Another action');
 expect(fetcher.mock.calls.filter(([,options])=>options?.method==='POST')).toHaveLength(posts);
});

test.each(['cancel_requested', 'cancelled', 'failed'])('actual snapshot validator accepts saved-state-unavailable for %s without hiding known accounting', async status => {
  const snapshot = structuredClone(snapshotFixture.snapshot);
  const run = { ...snapshot.run, status, readiness: null, report_unavailable_reason: 'saved_state_integrity_failed' };
  fetcher.mockResolvedValueOnce(json({ ...snapshot, run, brief: null, report: null, evidence: [], events: [], proposed_changes: [] }));
  const result = await api.readApi<any>('/api/runs/run_a/snapshot');
  expect(result.run.report_unavailable_reason).toBe('saved_state_integrity_failed');
  expect(result.run.remaining_limits.requests).toEqual(snapshot.run.remaining_limits.requests);
  expect(result.report).toBeNull();
});

test.each([undefined, null])('actual snapshot validator accepts old or null saved-state marker %s', async marker => {
  const snapshot = structuredClone(snapshotFixture.snapshot);
  const run = { ...snapshot.run, ...(marker === undefined ? {} : { report_unavailable_reason: marker }) };
  fetcher.mockResolvedValueOnce(json({ ...snapshot, run, brief: null, report: null, evidence: [], events: [], proposed_changes: [] }));
  await expect(api.readApi('/api/runs/run_a/snapshot')).resolves.toMatchObject({ run });
});

test('actual snapshot validator rejects an unknown saved-state reason', async () => {
  const snapshot = structuredClone(snapshotFixture.snapshot);
  const run = { ...snapshot.run, report_unavailable_reason: 'forged_private_reason' };
  fetcher.mockResolvedValueOnce(json({ ...snapshot, run, brief: null, report: null, evidence: [], events: [], proposed_changes: [] }));
  await expect(api.readApi('/api/runs/run_a/snapshot')).rejects.toThrow('unsupported');
});

test('actual client still accepts an explicit historical report after a saved-state-unavailable snapshot', async () => {
  const snapshot = structuredClone(snapshotFixture.snapshot);
  const run = { ...snapshot.run, status: 'failed', readiness: null, report_unavailable_reason: 'saved_state_integrity_failed' };
  fetcher.mockResolvedValueOnce(json({ ...snapshot, run, brief: null, report: null, evidence: [], events: [], proposed_changes: [] }));
  await expect(api.readApi('/api/runs/run_a/snapshot')).resolves.toMatchObject({ report: null });
  fetcher.mockResolvedValueOnce(json(adaptedHistoricalReport));
  await expect(api.readApi('/api/runs/run_a/reports/1')).resolves.toMatchObject({ id: adaptedHistoricalReport.id });
});
