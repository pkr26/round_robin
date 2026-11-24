import { after, test } from 'node:test';
import assert from 'node:assert/strict';
import vm from 'node:vm';
import crypto from 'node:crypto';
import { readFileSync, mkdirSync, writeFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import path from 'node:path';
const require=createRequire(import.meta.url);
const {createInstrumenter}=require('istanbul-lib-instrument');
const {createCoverageMap}=require('istanbul-lib-coverage');
const root=path.resolve(import.meta.dirname,'..');
const filename=path.join(root,'scripts/native-renderer.mjs');
const source=readFileSync(filename,'utf8');
const instrumenter=createInstrumenter({esModules:true,compact:false});
const code=instrumenter.instrumentSync(source,filename);
const map=createCoverageMap({});map.addFileCoverage(instrumenter.lastFileCoverage());
const sha=value=>crypto.createHash('sha256').update(value).digest('hex');
const defaultInput=()=>({schema_version:'1.0',source:'<p>controlled synthetic HTML only</p>',source_sha256:sha('<p>controlled synthetic HTML only</p>'),configuration_hash:'a'.repeat(64),viewport:{width:100,height:100},actions:[],timeout_ms:2000});
class TimeoutError extends Error {}

async function run(options={}){
 const input=Object.hasOwn(options,'input')?options.input:defaultInput();const hooks={},timers=[],output=[];let launches=0,closed=0,exited;
 const fail=()=>Promise.reject(new Error('PRIVATE exception must not escape'));
 const locator={click:async()=>{if(options.expireAction){timers[0]();throw new Error('timeout');}if(options.actionError)throw new Error('PRIVATE');},fill:async()=>{},press:async()=>{},textContent:async()=>options.text??'expected',inputValue:async()=>options.value??'expected',isVisible:async()=>options.visible??true};
 const page={on:(event,callback)=>{hooks[event]=callback;},setDefaultTimeout:()=>{},setDefaultNavigationTimeout:()=>{},setContent:async()=>{
   if(options.events){hooks.dialog?.({dismiss:fail});hooks.download?.({cancel:fail});hooks.popup?.({close:fail});for(let i=0;i<110;i++)hooks.pageerror?.();}
   for(let i=0;i<(options.network??0);i++)await hooks.route({request:()=>({url:()=>`https://private.example/${i}`,resourceType:()=> 'fetch'}),abort:fail});
   if(options.websocket)await hooks.websocket({url:()=> 'wss://private.example',close:()=>{}});
   if(options.throwTimeout)throw new TimeoutError('PRIVATE timeout');
 },locator:()=>locator,screenshot:async()=>options.oversize?Buffer.alloc(8*1024*1024+1):Buffer.from('synthetic PNG bytes')};
 const context={route:async(_,fn)=>{hooks.route=fn;},routeWebSocket:async(_,fn)=>{hooks.websocket=fn;},on:(event,fn)=>{hooks['context_'+event]=fn;},newPage:async()=>{hooks.context_page(page);return page;},close:fail};
 const browser={newContext:async()=>context,version:()=> 'fixture-browser',close:async()=>{closed++;if(options.closeError)throw new Error('PRIVATE close');}};
 const chromium={launch:async()=>{launches++;if(options.expireLaunch){timers[0]();timers[1]();throw new Error('launch timeout');}return browser;}};
 const encoded=options.raw??Buffer.from(JSON.stringify(input));
 const processStub={stdin:(async function*(){yield encoded;})(),stdout:{write:value=>{output.push(value);}},exit:value=>{exited=value;},exitCode:0};
 const scope=vm.createContext({Buffer,process:processStub,console,setTimeout:callback=>{timers.push(callback);return {unref:()=>{}};},clearTimeout:()=>{}});
 const module=new vm.SourceTextModule(code,{context:scope,identifier:filename,initializeImportMeta:meta=>{meta.url=new URL('../scripts/native-renderer.mjs',import.meta.url).href;}});
 await module.link(async specifier=>{
   let names,values;
   if(specifier==='node:crypto'){names=['default'];values=[crypto];}
   else if(specifier==='node:module'){names=['createRequire'];values=[()=>()=>({version:'fixture-playwright'})];}
   else if(specifier==='playwright'){names=['chromium','errors'];values=[chromium,{TimeoutError}];}
   else throw new Error('Unexpected renderer import');
   return new vm.SyntheticModule(names,function(){for(let i=0;i<names.length;i++)this.setExport(names[i],values[i]);},{context:scope});
 });
 await module.evaluate({timeout:1000});
 map.merge(scope.__coverage__);
 assert.equal(output.length,1);assert.ok(!output[0].includes('PRIVATE'));
 return {body:JSON.parse(output[0]),code:processStub.exitCode,launches,closed,exited};
}

test('successful renderer binds pixels, declared actions and all observed results',async()=>{
 const input=defaultInput();input.actions=[{action:'click',selector:'#button',assertion:{selector:'#result',text:'expected',visible:true}},{action:'fill',selector:'#field',value:'expected',assertion:{selector:'#field',value:'expected'}},{action:'press',selector:'#field',key:'Enter'},{action:'click',selector:'#other',assertion:{selector:'#result',text:'different'}}];
 const result=await run({input,events:true,network:2,websocket:true});
 assert.equal(result.code,0);assert.equal(result.body.source_sha256,input.source_sha256);assert.equal(result.body.checks.length,4);assert.equal(result.body.checks[0].assertion,'passed');assert.equal(result.body.checks[3].status,'failed');assert.equal(result.body.page_error_count,100);assert.equal(result.body.network_attempt_count,3);assert.equal(result.closed,1);
 assert.ok(result.body.network_attempts.every(item=>!item.url&&/^[a-f0-9]{64}$/.test(item.url_sha256)));
});

test('action failures stay local and distinguish requested assertions',async()=>{
 const input=defaultInput();input.actions=[{action:'click',selector:'#x'},{action:'click',selector:'#y',assertion:{selector:'#y',visible:false}}];
 const result=await run({input,actionError:true});assert.equal(result.code,0);assert.equal(result.body.checks[0].assertion,'not_requested');assert.equal(result.body.checks[1].assertion,'unavailable');
});

test('watchdog and library timeouts consistently refuse successful receipts',async()=>{
 for(const options of [{throwTimeout:true},{expireLaunch:true},{expireAction:true,input:{...defaultInput(),actions:[{action:'click',selector:'#x'}]}}]){
  const result=await run(options);assert.equal(result.code,124);assert.equal(result.body.error,'render_timeout');
 }
});

test('output, network attempts, parse and input byte limits fail without private diagnostics',async()=>{
 for(const options of [{oversize:true},{network:33},{raw:Buffer.from('invalid JSON')},{raw:Buffer.alloc(160*1024+1)},{closeError:true}]){
  const result=await run(options);assert.equal(result.code,1);assert.equal(result.body.error,'render_failed');
 }
});

const invalidInputs=[null,[], {extra:true}, {schema_version:'2'}, {source:''},{source:' '},{source:3},{source:'x'.repeat(128*1024+1)},{source_sha256:'bad'},{viewport:[]},{viewport:{width:99,height:100}},{viewport:{width:100,height:4001}},{viewport:{width:1.2,height:100}},{viewport:{width:100,height:100,extra:true}},{actions:null},{actions:Array(21).fill({})},{timeout_ms:249},{timeout_ms:20001},{timeout_ms:300.5}];
for(const [index,change] of invalidInputs.entries())test(`invalid native input ${index} fails before launching browser`,async()=>{
 const input=change===null||Array.isArray(change)?change:{...defaultInput(),...change};
 const result=await run({input});assert.equal(result.launches,0);assert.equal(result.code,1);
});

const invalidActions=[null,[],{}, {action:'navigate',selector:'#x'},{action:'click',selector:''},{action:'click',selector:3},{action:'click',selector:'x'.repeat(501)},{action:'click',selector:'#x',value:'no'},{action:'fill',selector:'#x'},{action:'fill',selector:'#x',value:3},{action:'fill',selector:'#x',value:'x'.repeat(2001)},{action:'click',selector:'#x',key:'Enter'},{action:'press',selector:'#x',key:'UntrustedKey'},{action:'click',selector:'#x',assertion:{}},{action:'click',selector:'#x',assertion:{selector:'',visible:true}},{action:'click',selector:'#x',assertion:{selector:3,visible:true}},{action:'click',selector:'#x',assertion:{selector:'x'.repeat(501),visible:true}},{action:'click',selector:'#x',assertion:{selector:'#x',text:1}},{action:'click',selector:'#x',assertion:{selector:'#x',text:'x'.repeat(2001)}},{action:'click',selector:'#x',assertion:{selector:'#x',value:1}},{action:'click',selector:'#x',assertion:{selector:'#x',value:'x'.repeat(2001)}},{action:'click',selector:'#x',assertion:{selector:'#x',visible:'yes'}}];
for(const [index,action] of invalidActions.entries())test(`invalid native action ${index} fails before launching browser`,async()=>{
 const result=await run({input:{...defaultInput(),actions:[action]}});assert.equal(result.launches,0);assert.equal(result.code,1);
});

after(()=>{
 const directory=path.join(root,'.local/exhaustive-audit/native-source');mkdirSync(directory,{recursive:true});
 const metrics=map.fileCoverageFor(filename).toSummary().toJSON();
 writeFileSync(path.join(directory,'coverage-final.json'),JSON.stringify(map.toJSON(),null,2)+'\n');
 writeFileSync(path.join(directory,'summary.json'),JSON.stringify({kind:'istanbul_source',files:{'scripts/native-renderer.mjs':{sha256:sha(source),...metrics}},metrics},null,2)+'\n');
 console.log('Native source coverage:',JSON.stringify(metrics));
});
