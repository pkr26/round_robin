import { defineConfig } from 'vitest/config';
import { createRequire } from 'node:module';
const { included, instrument } = createRequire(import.meta.url)('../scripts/frontend_instrumentation.cjs');

export default defineConfig({
  plugins: [{ name: 'round-robin-source-coverage', enforce: 'pre', transform(source, id) { if (process.env.ROUND_ROBIN_SOURCE_COVERAGE === '1' && included(id)) return instrument(source, id); } }],
  oxc: { jsx: { runtime: 'automatic' } },
  test: {
    // Audit counters must be regenerated from the current source. Vitest's
    // persistent transform cache also serves ordinary uninstrumented runs.
    fsModuleCache: process.env.ROUND_ROBIN_SOURCE_COVERAGE !== '1',
    environment: 'jsdom',
    include: ['tests/**/*.test.{ts,tsx}'],
    setupFiles: ['tests/setup.ts'],
    restoreMocks: true,
    coverage: {
      provider: 'istanbul',
      include: ['app/**/*.{ts,tsx}'],
      reportsDirectory: '../.local/exhaustive-audit/frontend',
      reporter: ['text', 'json', 'json-summary', 'html'],
      reportOnFailure: true,
      thresholds: { lines: 95.01, branches: 95.01, functions: 95.01, statements: 95.01 },
    },
  },
});
