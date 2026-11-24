import { test as base, expect } from "@playwright/test";
import { mkdir, writeFile } from "node:fs/promises";
import { join } from "node:path";
import { createHash } from "node:crypto";

export { expect };
export type { APIRequestContext, Page } from "@playwright/test";

// Coverage contains only shipped static JavaScript. Supplied source, request
// bodies and browser state never enter this optional audit artifact.
export const test = base.extend<{ frontendCoverage: void }>({
  frontendCoverage: [async ({ page }, use, testInfo) => {
    if (process.env.ROUND_ROBIN_SOURCE_COVERAGE === "1") {
      const captures: Record<string, unknown>[] = [];
      await page.exposeBinding('__roundRobinCapture', (_, value) => { captures.push(value); });
      await page.addInitScript(() => {
        window.addEventListener('pagehide', () => {
          const scope = window as unknown as { __coverage__?: Record<string, unknown>; __roundRobinCapture: (value: unknown) => Promise<void> };
          if (scope.__coverage__) void scope.__roundRobinCapture(scope.__coverage__);
        });
      });
      try { await use(); } finally {
        const current = await page.evaluate(() => (window as unknown as { __coverage__?: Record<string, unknown> }).__coverage__).catch(() => undefined);
        if (current) captures.push(current);
        if (testInfo.status === 'passed') {
          if (!captures.length) throw new Error('Browser audit build did not expose source-instrumented counters');
          const directory = join(process.cwd(), '.local', 'exhaustive-audit', 'frontend-source', 'browser');
          await mkdir(directory, { recursive: true });
          const identity = createHash('sha256').update(`${testInfo.file}:${testInfo.title}:${testInfo.retry}`).digest('hex').slice(0, 24);
          await writeFile(join(directory, `${identity}.json`), JSON.stringify({ test: testInfo.title, status: testInfo.status, captures }));
        }
      }
      return;
    }
    if (process.env.ROUND_ROBIN_E2E_COVERAGE !== "1") { await use(); return; }
    await page.coverage.startJSCoverage({ resetOnNavigation: false, reportAnonymousScripts: false });
    try { await use(); }
    finally {
      const entries = await page.coverage.stopJSCoverage();
      const shipped = entries.filter(entry => {
        try {
          const url = new URL(entry.url);
          return ["127.0.0.1", "localhost"].includes(url.hostname) && url.pathname.startsWith("/_next/static/");
        } catch { return false; }
      });
      const directory = join(process.cwd(), ".local", "deep-audit", "v8");
      await mkdir(directory, { recursive: true });
      const identity = createHash("sha256").update(`${testInfo.file}:${testInfo.title}:${testInfo.retry}`).digest("hex").slice(0, 24);
      await writeFile(join(directory, `${identity}.json`), JSON.stringify({
        test: testInfo.title, file: testInfo.file, retry: testInfo.retry,
        status: testInfo.status, entries: shipped,
      }));
    }
  }, { auto: true }],
});
