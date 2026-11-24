const { createInstrumenter } = require('istanbul-lib-instrument');
const { createHash } = require('node:crypto');
const path = require('node:path');
const root = path.resolve(__dirname, '..');
const app = path.join(root, 'frontend', 'app');
const included = filename => filename.startsWith(app + path.sep) && /\.(ts|tsx)$/.test(filename);
function instrument(source, filename) {
  if (!included(filename)) throw new Error('Frontend instrumentation escaped handwritten app source');
  const tool = createInstrumenter({ esModules: true, compact: false, produceSourceMap: true, parserPlugins: ['typescript', 'jsx'], preserveComments: true });
  const code = tool.instrumentSync(source, filename);
  const coverage = tool.lastFileCoverage();
  const sha256 = createHash('sha256').update(source).digest('hex');
  // Preserve the original source digest in each runtime map. It is checked by
  // the merger; edited files and stale browser captures cannot be combined.
  const tagged = code + `\n;globalThis.__coverage__[${JSON.stringify(filename)}].sourceSha256 = ${JSON.stringify(sha256)};`;
  coverage.sourceSha256 = sha256;
  return { code: tagged, map: tool.lastSourceMap(), coverage };
}
module.exports = { included, instrument };
