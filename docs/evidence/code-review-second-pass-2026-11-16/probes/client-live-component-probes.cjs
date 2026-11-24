const fs=require('node:fs'), Module=require('node:module'), assert=require('node:assert/strict');
const root=process.cwd(), ts=require(root+'/frontend/node_modules/typescript');
const {JSDOM}=require(root+'/frontend/node_modules/jsdom');
const dom=new JSDOM('<!doctype html><html><body></body></html>',{url:'http://127.0.0.1:3000/live'});
for(const k of ['window','document','HTMLElement','HTMLInputElement','HTMLTextAreaElement','HTMLDialogElement','Event','sessionStorage'])global[k]=dom.window[k];
Object.defineProperty(global,'navigator',{value:dom.window.navigator,configurable:true});global.IS_REACT_ACT_ENVIRONMENT=true;
const React=require(root+'/frontend/node_modules/react');
const {render,screen,fireEvent,waitFor,cleanup,act}=require(root+'/frontend/node_modules/@testing-library/react');
const Ajv=require(root+'/frontend/node_modules/ajv/dist/2020'), addFormats=require(root+'/frontend/node_modules/ajv-formats');
const ajv=new Ajv({strict:false,allErrors:true});addFormats(ajv);const validateDraft=ajv.compile(JSON.parse(fs.readFileSync(root+'/contracts/brief.schema.json','utf8')));
const fixture=JSON.parse(fs.readFileSync(root+'/frontend/tests/fixtures/runtime-snapshot.json','utf8'));
window.confirm=()=>true;
const results={};
function load(api){const path=root+'/frontend/app/live-workspace.tsx';const m=new Module(path);m.filename=path;m.paths=Module._nodeModulePaths(root+'/frontend/app');const orig=m.require.bind(m);m.require=name=>name==='./api-client'?api:name==='./contracts'?{validateDraft}:orig(name);m._compile(ts.transpileModule(fs.readFileSync(path,'utf8'),{compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2022,jsx:ts.JsxEmit.ReactJSX,esModuleInterop:true}}).outputText,path);return m.exports.default;}
function setup(meeting,snapshots,send){sessionStorage.clear();sessionStorage.setItem('round-robin:current-local-comparison:v1',JSON.stringify({meetingId:meeting.id,runId:null}));const api={API_URL:'http://127.0.0.1:8000',ApiError:class extends Error{},onAccessChange:()=>()=>{},refreshSession:async()=>({owner_id:'owner_test',profile:'development',csrf_token:null}),pendingCommand:()=>null,readApi:async path=>path==='/health'?{mode:'simulated'}:path==='/api/meetings'?{items:[meeting],next_cursor:null}:snapshots[path]??{items:[]},sendCommand:send};render(React.createElement(load(api)));return api;}
async function settle(){await act(async()=>{await new Promise(r=>setTimeout(r,10))});}
(async()=>{
 // A numeric constraint supplied by the valid server contract is rewritten by editing another line.
 let meeting=structuredClone(fixture.meeting);meeting.runs=[];meeting.draft.hard_requirements.push({id:'minimum_project_spend',description:'Spend at least 1200 on the selected approach',origin:'user_supplied',metric:'total_cost',operator:'gte',threshold:'1200'});
 let saved;
 setup(meeting,{},async(intent,path,body)=>{saved=structuredClone(body.brief);return {meeting_id:meeting.id,revision:2,brief:{...saved,revision:2}}});
 await screen.findByDisplayValue(meeting.draft.title);
 const req=screen.getByLabelText(/Hard requirements/);fireEvent.change(req,{target:{value:req.value+'\nKeep the workshop open'}});
 fireEvent.submit(screen.getByRole('button',{name:'Save draft'}).closest('form'));
 await waitFor(()=>assert.ok(saved));
 assert.ok(validateDraft(saved),JSON.stringify(validateDraft.errors));
 results.typed_requirement_loss={before:meeting.draft.hard_requirements.at(-1),after:saved.hard_requirements.find(x=>x.description==='Spend at least 1200 on the selected approach'),all_saved_requirements:saved.hard_requirements,valid_after:validateDraft(saved)};
 fs.writeFileSync('/tmp/rr-audit2-client/typed-constraint-before.json',JSON.stringify(meeting.draft));fs.writeFileSync('/tmp/rr-audit2-client/typed-constraint-after.json',JSON.stringify(saved));cleanup();

 // Autosave begins on the meeting draft; history and follow-up remain navigable during the request.
 meeting=structuredClone(fixture.meeting);const snapshot=structuredClone(fixture.snapshot);snapshot.brief.title='Frozen reviewed brief';
 let autosaveBody,resolveAutosave,followupBody;const deferred=new Promise(resolve=>resolveAutosave=resolve);
 setup(meeting,{[`/api/runs/${snapshot.run.id}/snapshot`]:snapshot},async(intent,path,body)=>{if(path.endsWith('/follow-ups')){followupBody=structuredClone(body);return {...meeting,draft:body.brief,draft_revision:3}}autosaveBody=structuredClone(body);return deferred;});
 await screen.findByDisplayValue(meeting.draft.title);
 fireEvent.change(screen.getByLabelText('Comparison title'),{target:{value:'Edited working draft'}});
 await waitFor(()=>assert.ok(autosaveBody),{timeout:2000});
 const history=[...document.querySelectorAll('.live-history button')][0];assert.equal(history.disabled,false);fireEvent.click(history);
 await screen.findByRole('button',{name:'Create a linked follow-up'});fireEvent.click(screen.getByRole('button',{name:'Create a linked follow-up'}));
 await screen.findByDisplayValue('Frozen reviewed brief');
 const beforeResolve=screen.getByLabelText('Comparison title').value;
 await act(async()=>resolveAutosave({meeting_id:meeting.id,revision:2,brief:{...autosaveBody.brief,revision:2}}));
 await waitFor(()=>assert.equal(screen.getByLabelText('Comparison title').value,'Edited working draft'));
 results.autosave_overwrites_followup={followup_title_before_late_response:beforeResolve,followup_title_after_late_response:screen.getByLabelText('Comparison title').value,save_button:screen.getByRole('button',{name:'Save linked follow-up draft'}).textContent,unsaved_changes_visible:!!screen.queryByText('Unsaved changes'),late_response_title:autosaveBody.brief.title};
 fireEvent.submit(screen.getByRole('button',{name:'Save linked follow-up draft'}).closest('form'));await waitFor(()=>assert.ok(followupBody));results.autosave_overwrites_followup.submitted_followup={parent_run_id:followupBody.parent_run_id,brief_title:followupBody.brief.title,expected_draft_revision:followupBody.expected_draft_revision};cleanup();

 // A selected option absent in another review survives history navigation and enables an invalid choice.
 meeting=structuredClone(fixture.meeting);const first=structuredClone(fixture.snapshot),second=structuredClone(fixture.snapshot);
 second.run.id='run_other_history';second.run.created_at='2026-11-15T12:00:00Z';second.report.run_id=second.run.id;second.report.id='report_other_history';second.report.comparison.find(x=>x.option_id==='option_a').option_id='option_replacement';second.brief.options.find(x=>x.id==='option_a').id='option_replacement';meeting.runs.push(second.run);
 fs.writeFileSync('/tmp/rr-audit2-client/second-history-snapshot.json',JSON.stringify(second));
 let outcome;
 setup(meeting,{[`/api/runs/${first.run.id}/snapshot`]:first,[`/api/runs/${second.run.id}/snapshot`]:second},async(intent,path,body)=>{outcome=structuredClone(body);return {id:'outcome_probe',payload:body,created_at:'2026-11-16T12:00:00Z'}});
 await screen.findByDisplayValue(meeting.draft.title);fireEvent.click(document.querySelectorAll('.live-history button')[0]);
 await screen.findByLabelText('Chosen approach');fireEvent.change(screen.getByLabelText('Chosen approach'),{target:{value:'option_a'}});fireEvent.change(screen.getByLabelText('Why you chose it'),{target:{value:'The first report has the best quote'}});
 fireEvent.click(document.querySelectorAll('.live-history button')[1]);await waitFor(()=>assert.equal(screen.getByLabelText('Chosen approach').querySelector('option[value="option_a"]'),null));
 const visibleChoice=screen.getByLabelText('Chosen approach').value,enabled=!screen.getByRole('button',{name:'Save my choice'}).disabled;
 fireEvent.click(screen.getByRole('button',{name:'Save my choice'}));await waitFor(()=>assert.ok(outcome));
 results.stale_outcome={visible_choice:visibleChoice,save_enabled:enabled,posted_payload:outcome};cleanup();

 // A legitimate not-applicable component is displayed as an ordinary editable price.
 meeting=structuredClone(fixture.meeting);meeting.runs=[];meeting.draft.options[0].cost_components[0].status='not_applicable';meeting.draft.options[0].cost_components[0].amount='0';let costSaved;
 setup(meeting,{},async(intent,path,body)=>{costSaved=structuredClone(body.brief);return {meeting_id:meeting.id,revision:2,brief:{...costSaved,revision:2}}});
 await screen.findByDisplayValue(meeting.draft.title);const amount=screen.getAllByLabelText('Upfront purchase (USD)')[0];fireEvent.change(amount,{target:{value:'9000'}});
 fireEvent.submit(screen.getByRole('button',{name:'Save draft'}).closest('form'));await waitFor(()=>assert.ok(costSaved));
 results.ignored_cost_edit={input_label:'Upfront purchase (USD)',entered_amount:'9000',saved_component:costSaved.options[0].cost_components[0],hidden_status_visible:document.body.textContent.includes('not_applicable'),valid_after:validateDraft(costSaved)};
 fs.writeFileSync('/tmp/rr-audit2-client/ignored-cost-edit.json',JSON.stringify(costSaved));cleanup();
 console.log(JSON.stringify(results,null,2));fs.writeFileSync('/tmp/rr-audit2-client/live-component-results.json',JSON.stringify(results,null,2));
})().catch(error=>{console.error(error);cleanup();process.exitCode=1});
