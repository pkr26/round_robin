import '@testing-library/jest-dom/vitest';
import { afterEach, beforeEach, vi } from 'vitest';
import { cleanup } from '@testing-library/react';
import { webcrypto } from 'node:crypto';

Object.defineProperty(globalThis, 'crypto', { value: webcrypto, configurable: true });
beforeEach(() => {
  sessionStorage.clear();
  localStorage.clear();
  vi.stubGlobal('fetch', vi.fn(() => Promise.reject(new Error('Unexpected network request in offline component tests'))));
});
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

// Same raw-source Istanbul counters as the production-browser audit build.
// Persist each worker's final map. The collecting command must itself pass;
// failed-suite artifacts are never accepted as final verification evidence.
import { afterAll } from 'vitest';
import { mkdirSync, writeFileSync } from 'node:fs';
import { createHash } from 'node:crypto';
import path from 'node:path';
afterAll(() => {
  if (process.env.ROUND_ROBIN_SOURCE_COVERAGE !== '1') return;
  const directory = path.resolve('../.local/exhaustive-audit/frontend-source/unit');
  mkdirSync(directory, { recursive: true });
  const coverage = (globalThis as typeof globalThis & { __coverage__?: unknown }).__coverage__;
  if (!coverage) throw new Error('Source coverage was requested but no counters were instrumented');
  const serialized = JSON.stringify(coverage);
  writeFileSync(path.join(directory, `${process.pid}-${createHash('sha256').update(serialized).digest('hex').slice(0, 16)}.json`), serialized);
});
