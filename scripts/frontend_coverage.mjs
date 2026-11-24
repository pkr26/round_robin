/** Source-map optional Playwright V8 captures onto current handwritten app files.
 * Run after ROUND_ROBIN_AUDIT_SOURCE_MAPS=1 pnpm build and coverage-enabled E2E.
 * Emits Istanbul JSON, LCOV and a summary; no coverage threshold is inferred.
 */
import { createHash } from "node:crypto";
import { createRequire } from "node:module";
import { readFile, readdir, mkdir, writeFile } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";
import v8toIstanbul from "v8-to-istanbul";
import libCoverage from "istanbul-lib-coverage";

const require = createRequire(import.meta.url);
const ts = require("../frontend/node_modules/typescript");
const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const app = path.join(root, "frontend/app");
const build = path.join(root, "frontend/out");
const input = path.join(root, ".local/deep-audit/v8");
const output = path.join(root, ".local/deep-audit/frontend-coverage");
const report = path.join(root, "docs/evidence/deep-audit/frontend-coverage.json");
const sha256 = content => createHash("sha256").update(content).digest("hex");
const relative = value => path.relative(root, value).split(path.sep).join("/");
const isAppSource = value => path.isAbsolute(value) && path.relative(app, value) !== "" && !path.relative(app, value).startsWith("..") && /\.tsx?$/.test(value);

async function filesBelow(directory) {
  const result = [];
  for (const item of await readdir(directory, { withFileTypes: true })) {
    const value = path.join(directory, item.name);
    if (item.isDirectory()) result.push(...await filesBelow(value));
    else if (item.isFile()) result.push(value);
  }
  return result.sort();
}

function originalPath(value) {
  // Only Next's explicit handwritten app source namespace is eligible. All
  // framework, contracts, dependencies, CSS and virtual modules are excluded.
  const match = /^webpack:\/\/[^/]+\/\.\/app\/(.+\.tsx?)$/.exec(value);
  if (!match) return null;
  const candidate = path.resolve(app, match[1]);
  return isAppSource(candidate) ? candidate : null;
}

function astFunctions(filename, source) {
  const tree = ts.createSourceFile(filename, source, ts.ScriptTarget.Latest, true);
  const functions = [];
  function visit(node, parent = "") {
    let scope = parent;
    if (ts.isFunctionLike(node) && node.body) {
      const start = tree.getLineAndCharacterOfPosition(node.getStart(tree));
      const name = node.name?.getText(tree) || (ts.isVariableDeclaration(node.parent) ? node.parent.name.getText(tree) : `anonymous@${start.line + 1}:${start.character + 1}`);
      scope += name;
      functions.push({ name: scope, line: start.line + 1, column: start.character, start: node.getStart(tree), body: node.body.getStart(tree), end: node.getEnd(), hits: null });
      scope += ".";
    }
    ts.forEachChild(node, child => visit(child, scope));
  }
  visit(tree);
  return { tree, functions };
}

function sourceFunctionHits(filename, source, coverage) {
  const { tree, functions } = astFunctions(filename, source);
  for (const [id, mapped] of Object.entries(coverage.fnMap)) {
    const location = mapped.loc;
    if (!location || location.start.line < 1 || location.end.line < 1 || location.start.line > tree.getLineStarts().length || location.end.line > tree.getLineStarts().length) continue;
    const start = tree.getPositionOfLineAndCharacter(location.start.line - 1, location.start.column);
    const end = tree.getPositionOfLineAndCharacter(location.end.line - 1, location.end.column);
    // Generated module wrappers must not count as a hit of every source
    // function. Require a range beginning at its declaration/header and ending
    // inside that same function. Ambiguous/inlined functions remain unmapped.
    const candidates = functions.filter(item => start >= item.start && start <= item.body + 1 && end <= item.end + 1);
    candidates.sort((left, right) => left.end - left.start - (right.end - right.start));
    const candidate = candidates[0];
    if (candidate) candidate.hits = (candidate.hits ?? 0) + coverage.f[id];
  }
  return functions.map(({ start, body, end, ...item }) => item);
}

async function main() {
  const coverage = libCoverage.createCoverageMap({});
  const skipped = [];
  const accepted = [];
  const verifiedSources = new Set();
  const captures = (await filesBelow(input)).filter(filename => filename.endsWith(".json"));
  for (const capture of captures) {
    const record = JSON.parse(await readFile(capture, "utf8"));
    if (record.status !== "passed") { skipped.push({ capture: relative(capture), reason: "test_not_passed" }); continue; }
    for (const entry of record.entries ?? []) {
      let bundle;
      try {
        const url = new URL(entry.url);
        if (!["127.0.0.1", "localhost"].includes(url.hostname) || !url.pathname.startsWith("/_next/static/") || !url.pathname.endsWith(".js")) throw new Error();
        bundle = path.resolve(build, "." + url.pathname);
        if (!bundle.startsWith(build + path.sep)) throw new Error();
      } catch { skipped.push({ capture: relative(capture), reason: "foreign_or_invalid_bundle" }); continue; }
      let built;
      let map;
      try {
        built = await readFile(bundle, "utf8");
        map = JSON.parse(await readFile(bundle + ".map", "utf8"));
      } catch { skipped.push({ bundle: relative(bundle), reason: "bundle_or_audit_source_map_missing" }); continue; }
      if (typeof entry.source !== "string" || sha256(entry.source) !== sha256(built)) { skipped.push({ bundle: relative(bundle), reason: "stale_capture_bundle_checksum" }); continue; }
      if (!Array.isArray(map.sources)) throw new Error(`Source map has no source namespace: ${relative(bundle)}`);
      const paths = map.sources.map(originalPath);
      if (!paths.some(Boolean)) continue;
      if (!Array.isArray(map.sourcesContent) || map.sources.length !== map.sourcesContent.length) throw new Error(`Source map is missing its original app source contents: ${relative(bundle)}`);
      for (const [index, source] of paths.entries()) {
        if (!source) continue;
        const current = await readFile(source, "utf8");
        if (typeof map.sourcesContent[index] !== "string" || sha256(map.sourcesContent[index]) !== sha256(current)) throw new Error(`Source map does not match current ${relative(source)}; rebuild before measuring`);
        verifiedSources.add(source);
      }
      // Preserve all original map indices while excluding non-app paths from
      // conversion. v8-to-istanbul receives absolute source paths explicitly.
      map = { ...map, sourceRoot: "", sources: paths.map((value, index) => value ?? path.join(root, ".local/deep-audit/excluded-map-source", String(index))) };
      const converter = v8toIstanbul(bundle, 0, { source: built, sourceMap: { sourcemap: map } }, value => !isAppSource(value));
      await converter.load();
      converter.applyCoverage(entry.functions);
      coverage.merge(converter.toIstanbul());
      accepted.push({ capture: relative(capture), test: record.test, bundle: relative(bundle), sha256: sha256(built) });
    }
  }
  if (!accepted.length || !coverage.files().length) throw new Error("No current source-mapped app coverage. Build with ROUND_ROBIN_AUDIT_SOURCE_MAPS=1, run E2E with ROUND_ROBIN_E2E_COVERAGE=1, then retry.");
  const perFile = [];
  const normalized = {};
  const lcov = [];
  for (const filename of coverage.files().sort()) {
    if (!isAppSource(filename) || !verifiedSources.has(filename)) throw new Error("Coverage escaped its verified handwritten source scope");
    const value = coverage.fileCoverageFor(filename);
    const source = await readFile(filename, "utf8");
    const functions = sourceFunctionHits(filename, source, value.data);
    perFile.push({ path: relative(filename), sha256: sha256(source), metrics: value.toSummary().toJSON(), source_functions: functions,
      uncovered_mapped_functions: functions.filter(item => item.hits === 0), unmapped_functions: functions.filter(item => item.hits === null),
      uncovered_lines: Object.entries(value.getLineCoverage()).filter(([, hits]) => hits === 0).map(([line]) => Number(line)) });
    normalized[relative(filename)] = { ...value.toJSON(), path: relative(filename) };
    lcov.push("TN:Round Robin source-mapped browser V8", `SF:${relative(filename)}`);
    for (const [index, item] of functions.entries()) {
      if (item.hits === null) continue;
      const name = `${index}:${item.name}`.replace(/[\r\n]/g, " ");
      lcov.push(`FN:${item.line},${name}`, `FNDA:${item.hits},${name}`);
    }
    for (const [line, hits] of Object.entries(value.getLineCoverage())) lcov.push(`DA:${line},${hits}`);
    lcov.push("end_of_record");
  }
  const allFiles = (await filesBelow(app)).filter(filename => /\.tsx?$/.test(filename));
  const unavailable = [];
  for (const filename of allFiles.filter(filename => !coverage.files().includes(filename))) {
    const source = await readFile(filename, "utf8");
    unavailable.push({ path: relative(filename), sha256: sha256(source), reason: "No current browser source mapping/capture; server-only, unimported or removed by compiler. Excluded from measured denominator.", functions: astFunctions(filename, source).functions.map(({ name, line, column }) => ({ name, line, column })) });
  }
  const summary = { kind: "source_mapped_browser_v8", current_source_maps_verified: true, metrics: coverage.getCoverageSummary().toJSON(), files: perFile, unavailable_files: unavailable,
    source_function_totals: { inventoried: perFile.reduce((total, file) => total + file.source_functions.length, 0), mapped: perFile.reduce((total, file) => total + file.source_functions.filter(item => item.hits !== null).length, 0), uncovered_mapped: perFile.reduce((total, file) => total + file.uncovered_mapped_functions.length, 0), unmapped: perFile.reduce((total, file) => total + file.unmapped_functions.length, 0) },
    accepted_captures: accepted, skipped_captures: skipped,
    limitations: ["V8 ranges are remapped through compiler source maps; this is not source-instrumented Istanbul statement or branch measurement.", "Istanbul function metrics describe mapped V8 function ranges. The separate source-function inventory matches declaration/header and end locations conservatively; unmapped functions are not claimed executed or unexecuted.", "Type-only, blank and compiler-eliminated lines may be represented differently by V8 line conversion. Server components without browser maps are listed separately and excluded from measured denominators.", "Only successful test captures whose bundle and original source checksums match this build are merged; this run may be a subset of the full browser suite.", "No 100 percent coverage or correctness claim is implied; inspect uncovered lines/functions and unavailable files."] };
  await mkdir(output, { recursive: true });
  await writeFile(path.join(output, "coverage-final.json"), JSON.stringify(normalized, null, 2) + "\n");
  await writeFile(path.join(output, "summary.json"), JSON.stringify(summary, null, 2) + "\n");
  await writeFile(path.join(output, "lcov.info"), lcov.join("\n") + "\n");
  await mkdir(path.dirname(report), { recursive: true });
  await writeFile(report, JSON.stringify(summary, null, 2) + "\n");
  console.log(JSON.stringify({ report: relative(report), output: relative(output), files: perFile.length, unavailable_files: unavailable.length, metrics: summary.metrics, source_function_totals: summary.source_function_totals, accepted_bundles: accepted.length, skipped_entries: skipped.length }, null, 2));
}

export { originalPath, sourceFunctionHits };

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  try { await main(); }
  catch (error) { console.error(error instanceof Error ? error.message : "Frontend coverage conversion failed"); process.exitCode = 1; }
}
