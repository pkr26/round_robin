const fs = require('node:fs');
const Module = require('node:module');
const { webcrypto } = require('node:crypto');
const root = process.cwd();
const ts = require(root+'/frontend/node_modules/typescript');
const original = fs.readFileSync(root+'/frontend/app/api-client.ts', 'utf8');
const compiled = ts.transpileModule(original, {compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2022,esModuleInterop:true}}).outputText;
function reload(){const module = new Module(root+'/frontend/app/api-client.cjs');module.filename=root+'/frontend/app/api-client.cjs';module.paths=Module._nodeModulePaths(root+'/frontend/app');module._compile(compiled,module.filename);return module.exports;}
const items = new Map();global.sessionStorage={getItem:key=>items.get(key)||null,setItem:(key,value)=>items.set(key,value),removeItem:key=>items.delete(key)};
global.window=new EventTarget();Object.defineProperty(global,'crypto',{value:webcrypto,configurable:true});
let mutations=0;
global.fetch=async(url,options)=>{if(url.endsWith('/auth/session'))return new Response(JSON.stringify({owner_id:'owner_test',profile:'development',csrf_token:null}));if(url.includes('/api/commands/'))return new Response('{}',{status:404});mutations++;if(mutations===1)throw new TypeError('network failure before delivery');return new Response(JSON.stringify({detail:'preflight_expired'}),{status:409});};
(async()=>{const api=reload();try{await api.sendCommand('start:preflight_one','/api/meetings/meeting_one/runs',{preflight_id:'preflight_one',expected_draft_revision:1})}catch{}let retryError;try{await api.retryPendingCommand()}catch(e){retryError=e.message};console.log(JSON.stringify({retryError,reconcile:await api.reconcileCommand(),receiptStillPresent:!!api.pendingCommand(),mutationAttempts:mutations},null,2));})();
