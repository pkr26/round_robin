import assert from "node:assert/strict";
import { test } from "node:test";
import path from "node:path";
import { originalPath, sourceFunctionHits } from "./frontend_coverage.mjs";

test("source-map scope excludes framework, dependencies, CSS and namespace escapes", () => {
  assert.equal(originalPath("webpack://_N_E/./app/design/workspace.tsx"), path.resolve("frontend/app/design/workspace.tsx"));
  for (const source of ["webpack://_N_E/./node_modules/next/app.ts", "webpack://_N_E/./app/../../private.ts", "webpack://_N_E/./app/visual.css", "file:///outside/app/private.ts", "webpack://_N_E/./../contracts/sample.ts"]) assert.equal(originalPath(source), null);
});

test("a generated wrapper range cannot claim execution of unused source functions", () => {
  const source = "function used() { return 1; }\nfunction unused() { return 2; }\n";
  const coverage = { fnMap: {
    wrapper: { loc: { start: { line: 1, column: 0 }, end: { line: 2, column: 31 } } },
    used: { loc: { start: { line: 1, column: 0 }, end: { line: 1, column: 29 } } },
    unused: { loc: { start: { line: 2, column: 0 }, end: { line: 2, column: 31 } } },
  }, f: { wrapper: 4, used: 2, unused: 0 } };
  const functions = sourceFunctionHits("fixture.ts", source, coverage);
  assert.deepEqual(functions.map(({ name, hits }) => ({ name, hits })), [{ name: "used", hits: 2 }, { name: "unused", hits: 0 }]);
});

test("functions without source-map evidence are reported unmapped, not executed", () => {
  const source = "const omitted = () => 1;\n";
  assert.deepEqual(sourceFunctionHits("fixture.ts", source, { fnMap: {}, f: {} }).map(({ name, hits }) => ({ name, hits })), [{ name: "omitted", hits: null }]);
});
