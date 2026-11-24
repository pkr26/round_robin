import { createRequire } from 'node:module';
import { readdir, readFile, mkdir, writeFile } from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
const require = createRequire(import.meta.url);
const { createCoverageMap } = require('istanbul-lib-coverage');
const { createContext } = require('istanbul-lib-report');
const reports = require('istanbul-reports');
const { instrument } = require('./frontend_instrumentation.cjs');
const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const output = path.join(root, '.local/exhaustive-audit/frontend-source');
async function files(directory) {
  const result=[];
  for(const item of await readdir(directory,{withFileTypes:true})) {
    const name=path.join(directory,item.name);
    if(item.isDirectory())result.push(...await files(name)); else result.push(name);
  }
  return result;
}
const sourceFiles=(await files(path.join(root,'frontend/app'))).filter(name=>/\.tsx?$/.test(name));
const expected=new Map();
const coverage=createCoverageMap({});
for(const filename of sourceFiles){
  const {coverage:value}=instrument(await readFile(filename,'utf8'),filename);
  expected.set(filename,value);coverage.addFileCoverage(value);
}
let accepted=0;
for(const kind of ['unit','browser']) {
  const directory=path.join(output,kind);
  let paths=[];try{paths=(await files(directory)).filter(name=>name.endsWith('.json'));}catch(error){if(error.code!=='ENOENT')throw error;}
  for(const filename of paths){
    const data=JSON.parse(await readFile(filename,'utf8'));
    const captures=kind==='browser'?(data.status==='passed'?data.captures:[]):[data];
    for(const capture of captures){
      for(const [source,value] of Object.entries(capture)){
        const original=expected.get(source);
        if(!original||value.sourceSha256!==original.sourceSha256)throw new Error(`Stale or out-of-scope source capture: ${source}`);
        for(const field of ['statementMap','fnMap','branchMap'])if(JSON.stringify(original[field])!==JSON.stringify(value[field]))throw new Error(`Incompatible source instrumentation for ${source}`);
        coverage.addFileCoverage(value);
      }
      accepted++;
    }
  }
}
if(!accepted)throw new Error('No fresh source-instrumented test captures');
const context=createContext({dir:output,coverageMap:coverage});
for(const name of ['json','json-summary','html','text'])reports.create(name).execute(context);
const summary={kind:'istanbul_source',all_app_files_included:true,source_hashes_verified:true,accepted_captures:accepted,metrics:coverage.getCoverageSummary().toJSON(),files:Object.fromEntries(coverage.files().sort().map(filename=>[path.relative(root,filename),{sha256:expected.get(filename).sourceSha256,...coverage.fileCoverageFor(filename).toSummary().toJSON()}]))};
await writeFile(path.join(output,'summary.json'),JSON.stringify(summary,null,2)+'\n');
console.log(JSON.stringify(summary.metrics,null,2));
if(process.argv.includes('--check') && ['lines','branches','functions','statements'].some(name=>summary.metrics[name].pct<=95))process.exitCode=1;
