// Independently inventory source locations rather than trusting capture totals.
import { readFileSync } from 'node:fs';
import { createRequire } from 'node:module';
const require = createRequire(import.meta.url);
const { createInstrumenter } = require('istanbul-lib-instrument');
const { createCoverageMap } = require('istanbul-lib-coverage');
const files = JSON.parse(readFileSync(0, 'utf8'));
const result = {};
for (const filename of files) {
  const instrumenter = createInstrumenter({esModules: true, compact: false, parserPlugins: ['typescript', 'jsx'], preserveComments: true});
  instrumenter.instrumentSync(readFileSync(filename, 'utf8'), filename);
  const map = createCoverageMap({});
  map.addFileCoverage(instrumenter.lastFileCoverage());
  const summary = map.fileCoverageFor(filename).toSummary().toJSON();
  result[filename] = {lines: summary.lines.total, branches: summary.branches.total};
}
process.stdout.write(JSON.stringify(result));
