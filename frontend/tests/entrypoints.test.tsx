import { expect, test, vi } from 'vitest';
import { renderToStaticMarkup } from 'react-dom/server';
import Page from '../app/page';
import LivePage, { metadata as liveMetadata } from '../app/live/page';
import DesignPage, { metadata as visualMetadata } from '../app/design/page';
import RootLayout, { metadata } from '../app/layout';
import Workspace from '../app/workspace';
import LiveWorkspace from '../app/live-workspace';
import DesignWorkspace from '../app/design/workspace';
import { sample, validateDraft } from '../app/contracts';

test('every route selects the intended workspace and layout declares document language', () => {
  expect(Page().type).toBe(Workspace);
  expect(LivePage().type).toBe(LiveWorkspace);
  expect(DesignPage().type).toBe(DesignWorkspace);
  expect(renderToStaticMarkup(RootLayout({ children: <main>Safe route</main> }))).toContain('<html lang="en"><head></head><body><main>Safe route</main></body></html>');
  expect(String(metadata.description)).toContain('fictional');
  expect(String(liveMetadata.description)).toContain('simulated');
  expect(String(visualMetadata.description)).toContain('immutable');
  expect(validateDraft(sample.scenarios[0].brief)).toBe(true);
  expect(validateDraft({ private: 'malformed' })).toBe(false);
});

test('a corrupt generated sample fails visibly instead of presenting invented data', async () => {
  vi.resetModules();
  vi.doMock('../../contracts/sample.json', () => ({ default: {} }));
  await expect(import('../app/contracts')).rejects.toThrow('does not match the Python contract');
  vi.doUnmock('../../contracts/sample.json');
});
