import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { beforeEach, expect, test, vi } from 'vitest';
import type { DependencyList, EffectCallback } from 'react';
import DesignWorkspace from '../app/design/workspace';

// Pause only the selected native hydration effect to model an edit between
// committing the editable scope and starting its passive network effect.
const nativeEffect=vi.hoisted(()=>({defer:false,start:undefined as (()=>void)|undefined}));
vi.mock('react',async original=>{
 const react=await original<typeof import('react')>();
 return {...react,useEffect(effect:EffectCallback,deps?:DependencyList){
  return react.useEffect(()=>{
   if(nativeEffect.defer&&deps?.[0]==='design_one'&&deps?.[1]==='version_1'){
    let cleanup:ReturnType<EffectCallback>;
    nativeEffect.start=()=>{cleanup=effect();};
    return ()=>{if(typeof cleanup==='function')cleanup();};
   }
   return effect();
  },deps);
 }};
});
const api=vi.hoisted(()=>({ readApi:vi.fn(), readDesignSource:vi.fn(), sendCommand:vi.fn(), refreshSession:vi.fn(), pendingCommand:vi.fn(), reconcileCommand:vi.fn(), abandonPendingCommand:vi.fn(), retryPendingCommand:vi.fn(), uploadDesignImage:vi.fn(), downloadDesignSource:vi.fn(), onAccessChange:vi.fn() }));
vi.mock('../app/api-client',()=>({ ...api,API_URL:'http://127.0.0.1:8000',ApiError:class extends Error {} }));
const copy=<T,>(value:T):T=>structuredClone(value);
const cap={enabled:true,paid_access_enabled:true,native_diagram_enabled:true,native_interface_enabled:true,maximum_usd:'2',supported_sizes:['1024x1024','1536x1024'],limitations:['Synthetic fixture capability limits']};
function version(number=1,source='upload') { return { id:`version_${number}`,version:number,parent_version_id:number>1?`version_${number-1}`:null,canonical_hash:'a'.repeat(64),width:100,height:100,source_kind:source,provenance:{},coverage:{complete:false,generator_provenance:'unknown',limitations:['Unknown original provenance']},reviews:[] as any[] }; }
function design() { return { id:'design_one',meeting_id:'meeting_one',mode:'improve',status:'active',revision:1,brief:{purpose:'Synthetic visual purpose',audience:'Test audience',domain:'raster_concept',locked_requirements:[] as string[],required_text:[] as string[],width:100,height:100},recipients:['openai','anthropic','google'],versions:[] as ReturnType<typeof version>[],pending_operations:[] as any[],operation_failures:[] as any[],selection_recorded:false,selected_version_id:null as string|null,state:{submission_count:0,request_count:0,version_count:0,maximum_usd:'2'} }; }
let saved:ReturnType<typeof design>[];
let access:()=>void;
let capabilities:typeof cap;
let meetingItems:{id:string,title:string}[];
beforeEach(()=>{
  vi.resetAllMocks();nativeEffect.defer=false;nativeEffect.start=undefined;saved=[];capabilities=copy(cap);meetingItems=[{id:'meeting_one',title:'Synthetic comparison'}];
  api.refreshSession.mockResolvedValue({owner_id:'owner_test'});api.pendingCommand.mockReturnValue(null);
  api.onAccessChange.mockImplementation(callback=>{access=callback;return ()=>{};});
  api.readDesignSource.mockResolvedValue('<p>Saved native source</p>');
  api.readApi.mockImplementation(async path=>{
    if(path==='/api/visual/capabilities')return capabilities;
    if(path==='/api/meetings')return {items:meetingItems};
    if(path.endsWith('/design-sessions'))return {items:saved};
    return copy(saved.find(item=>path.endsWith(item.id))!);
  });
  api.sendCommand.mockImplementation(async(_intent,path,body)=>{
    if(path.endsWith('/design-sessions')){const next={...design(),mode:body.mode,brief:body.brief,recipients:body.recipients};saved=[next];return copy(next);}
    const current=saved[0];
    if(path.endsWith('/preflight'))return {id:'quote_one',revision:current.revision,action:body.action,mode:current.mode,expires_at:new Date(Date.now()+600000).toISOString(),recipients:['anthropic','google'],models:{anthropic:'test-model'},limits:{maximum_usd:'2'},blockers:[]};
    if(path.endsWith('/selection')){current.selected_version_id=body.version_id;current.selection_recorded=true;}
    if(path.endsWith('/cancel'))current.status='cancelled';
    if(path.includes('/native-'))current.versions.push(version(current.versions.length+1,path.endsWith('diagram')?'native_diagram':'native_interface'));
    return copy(current);
  });
  api.uploadDesignImage.mockImplementation(async()=>{saved[0].versions=[version()];saved[0].state.version_count=1;return {...copy(saved[0]),upload_status:'ready'};});
  api.reconcileCommand.mockResolvedValue({confirmed:true});api.retryPendingCommand.mockResolvedValue(undefined);api.downloadDesignSource.mockResolvedValue(undefined);
});
async function boot(value?:ReturnType<typeof design>){if(value)saved=[value];render(<DesignWorkspace/>);await screen.findByRole('heading',{name:'Your visual briefs'});if(value){fireEvent.click(await screen.findByRole('button',{name:`${value.brief.purpose.slice(0,80)} · ${value.versions.length} versions`}));await screen.findByRole('heading',{name:value.brief.purpose});}}
function button(name:string|RegExp){return screen.getByRole('button',{name});}

test('closed capability gate and empty meeting history do not create work',async()=>{
 capabilities.enabled=false;render(<DesignWorkspace/>);await screen.findByRole('heading',{name:'Visual work is disabled in this deployment.'});expect(api.sendCommand).not.toHaveBeenCalled();
});

test('empty meeting history is explicit',async()=>{meetingItems=[];await boot();expect(screen.getByText('Save a comparison in the core workspace first.')).toBeVisible();});

test('initial authentication and receipt errors are visible without retaining content',async()=>{
 api.refreshSession.mockRejectedValueOnce('not an Error');render(<DesignWorkspace/>);await screen.findByRole('alert');expect(screen.getByRole('alert')).toHaveTextContent('could not be confirmed');
});

test('frozen form records explicit recipients, requirements, dimensions and allowance',async()=>{
 await boot();fireEvent.change(screen.getByLabelText('Mode'),{target:{value:'improve'}});fireEvent.change(screen.getByLabelText('Output domain'),{target:{value:'native_interface'}});fireEvent.change(screen.getByLabelText('Purpose'),{target:{value:'New private purpose'}});fireEvent.change(screen.getByLabelText('Audience'),{target:{value:'Reviewer'}});
 fireEvent.change(screen.getByLabelText('Maximum provider allowance (USD)'),{target:{value:'1'}});fireEvent.change(screen.getByLabelText('Width (pixels)'),{target:{value:'500'}});fireEvent.change(screen.getByLabelText('Height (pixels)'),{target:{value:'300'}});fireEvent.change(screen.getByLabelText(/Protected requirements/),{target:{value:'Keep labels\n\nStay legible'}});fireEvent.change(screen.getByLabelText(/Required literal text/),{target:{value:'Hello\n'}});
 fireEvent.click(screen.getByLabelText('OpenAI'));fireEvent.click(screen.getByLabelText('OpenAI'));fireEvent.click(button('Save frozen visual brief'));await screen.findByRole('heading',{name:'New private purpose'});
 expect(api.sendCommand.mock.calls[0][2]).toMatchObject({mode:'improve',brief:{domain:'native_interface',width:500,height:300,locked_requirements:['Keep labels','Stay legible'],required_text:['Hello']}});
 expect((screen.getByLabelText('Native HTML source') as HTMLTextAreaElement).value).toContain('Interface draft');
 fireEvent.click(button('New visual brief'));await screen.findByRole('heading',{name:'Freeze the purpose and scope.'});
 fireEvent.change(screen.getByLabelText('Output domain'),{target:{value:'structured_diagram'}});
});

test('meeting switch clears selected design and scopes history to requested meeting',async()=>{
 meetingItems.push({id:'meeting_two',title:'Second synthetic comparison'});await boot(design());fireEvent.change(screen.getByLabelText('Saved comparison'),{target:{value:'meeting_two'}});await waitFor(()=>expect(api.readApi).toHaveBeenCalledWith('/api/meetings/meeting_two/design-sessions'));expect(screen.queryByRole('heading',{name:'Synthetic visual purpose'})).not.toBeInTheDocument();
});

test.each(['ready','processing'])('upload acknowledgement %s is shown truthfully',async status=>{
 await boot(design());api.uploadDesignImage.mockResolvedValueOnce({...design(),versions:[version()],upload_status:status});
 fireEvent.change(screen.getByLabelText('Upload original PNG or JPEG'),{target:{files:[new File(['synthetic'],'original.png',{type:'image/png'})]}});
 await screen.findByText(status==='ready'?'Original V1 saved. Metadata was removed. No model review has been claimed.':'The image upload is still unconfirmed; no usable candidate has been claimed.');
 expect(api.uploadDesignImage).toHaveBeenCalledTimes(1);
});

test('empty file selection makes no upload request',async()=>{await boot(design());fireEvent.change(screen.getByLabelText('Upload original PNG or JPEG'),{target:{files:[]}});expect(api.uploadDesignImage).not.toHaveBeenCalled();});

test.each(['generate','review','edit'])('explicit %s preflight queues the displayed exact work',async action=>{
 const current=design();if(action!=='generate'){current.versions=[version()];current.versions[0].coverage.complete=action==='edit';}
 await boot(current);fireEvent.click(button(action==='generate'?'Prepare original generation':action==='review'?'Review visual preflight':'Prepare next-version edit'));
 await screen.findByRole('heading',{name:'Confirm the exact visual work.'});expect(button(`Start bounded visual ${action}`)).toBeDisabled();
 if(action!=='review')fireEvent.change(screen.getByLabelText('Specific generation or edit intent'),{target:{value:'Only change background'}});
 fireEvent.click(screen.getByLabelText('I approve this exact input scope, action and original allowance.'));fireEvent.click(button(`Start bounded visual ${action}`));await screen.findByText(/Visual work accepted/);
 const sent=api.sendCommand.mock.calls.at(-1)!;expect(sent[1]).toContain(action==='review'?'/reviews':'/improvements');expect(sent[2].preflight_id).toBe('quote_one');
 if(action==='edit')expect(sent[2].target_version_id).toBe('version_1');
});

test('unfunded preflight blockers and expired quotes cannot be confirmed',async()=>{
 const current=design();await boot(current);api.sendCommand.mockResolvedValueOnce({id:'quote_one',revision:1,action:'generate',mode:'improve',expires_at:new Date(Date.now()-1000).toISOString(),recipients:['unknown_provider'],models:{},limits:{maximum_usd:0},blockers:['No funding']});
 fireEvent.click(button('Prepare original generation'));await screen.findByText('No funding');fireEvent.click(screen.getByLabelText('I approve this exact input scope, action and original allowance.'));expect(button('Start bounded visual generate')).toBeDisabled();
});

test('selection and decline remain distinct from independent review',async()=>{
 const current=design();current.versions=[version()];await boot(current);fireEvent.click(button('Keep V1 with incomplete review'));await screen.findByText(/Your chosen version is saved/);expect(screen.getByText(/Selected V1/)).toBeVisible();fireEvent.click(button('Decline all candidates'));await screen.findByText(/Declined all candidates/);expect(button('Decline all candidates')).toBeDisabled();
});

test('immutable version comparison shows exact critics, regression context and native provenance',async()=>{
 const current=design();current.brief.locked_requirements=['Keep readable'];current.recipients=['openai','unfamiliar'];current.versions=[version(1,'native_diagram'),version(2,'native_interface')];current.versions[0].coverage.complete=true;current.versions[0].coverage.generator_provenance='known';current.versions[0].provenance={renderer_version:'fixture-renderer',checks:[{status:'passed'}]};current.versions[0].reviews=[{provider:'unfamiliar',findings:[{id:'finding_a',description:'Needs contrast',category:'observed_defect',severity:'material',disposition:'persistent',view_index:0,region:{x:.1,y:.2,width:.3,height:.4},previous_finding_id:'finding_previous'},{id:'finding_b',description:'Preference only',category:'preference',severity:'minor',disposition:'pending',view_index:1}],limitations:['Synthetic reviewer limitation']}];
 await boot(current);fireEvent.click(button('Inspect V1'));expect(screen.getByText('Needs contrast')).toBeVisible();expect(screen.getByText(/Rechecks prior finding/)).toBeVisible();fireEvent.click(screen.getByLabelText('Show a linear comparison'));expect(screen.getAllByRole('img')).toHaveLength(2);
 fireEvent.click(screen.getAllByRole('button',{name:'Download editable source'})[0]);await waitFor(()=>expect(api.downloadDesignSource).toHaveBeenCalledWith('version_1',1,'svg'));
 fireEvent.click(screen.getAllByRole('button',{name:'Download editable source'})[1]);await waitFor(()=>expect(api.downloadDesignSource).toHaveBeenCalledWith('version_2',2,'html'));
 fireEvent.click(screen.getByLabelText('Show a linear comparison'));expect(screen.getAllByRole('img')).toHaveLength(1);
});

test.each(['structured_diagram','native_interface'])('native %s uses exact source and action plan',async domain=>{
 const current=design();current.brief.domain=domain;await boot(current);
 const source=domain==='structured_diagram'?'{"nodes":[]}':'<p>Updated private source</p>';
 fireEvent.change(screen.getByLabelText(domain==='structured_diagram'?'Structured diagram JSON':'Native HTML source'),{target:{value:source}});
 if(domain==='native_interface')fireEvent.change(screen.getByLabelText('Declared browser actions and assertions (JSON)'),{target:{value:'[{"action":"click","selector":"#x"}]'}});
 fireEvent.click(button('Render a saved native candidate'));await screen.findByText(/Native source and its exact rendered pixels/);expect(api.sendCommand.mock.calls.at(-1)?.[2]).toMatchObject(domain==='structured_diagram'?{diagram:{nodes:[]}}:{source,actions:[{action:'click',selector:'#x'}]});
});

test.each([false,true])('native source cannot overwrite an edit with deferred effect=%s and failure remains explicit',async deferEffect=>{
 nativeEffect.defer=deferEffect;
 const current=design();current.brief.domain='native_interface';current.versions=[version(1,'native_interface')];let resolve!:(value:string)=>void;api.readDesignSource.mockReturnValueOnce(new Promise<string>(done=>{resolve=done;}));await boot(current);
 fireEvent.click(screen.getByText('Render the exact native source',{exact:true}));
 const input=screen.getByLabelText('Native HTML source');
 await waitFor(()=>{expect(input).toBeEnabled();expect(input).toBeVisible();if(deferEffect)expect(nativeEffect.start).toBeTypeOf('function');else expect(api.readDesignSource).toHaveBeenCalledWith('version_1','html');});
 if(deferEffect)expect(api.readDesignSource).not.toHaveBeenCalled();
 fireEvent.change(input,{target:{value:'Local edit stays'}});expect(input).toHaveValue('Local edit stays');
 if(deferEffect)act(()=>nativeEffect.start!());
 await act(async()=>{resolve('Late private content');});expect(input).toHaveValue('Local edit stays');
 nativeEffect.defer=false;
 api.readDesignSource.mockRejectedValueOnce(new Error('source gone'));fireEvent.click(button('New visual brief'));fireEvent.click(button(/Synthetic visual purpose ·/));await screen.findByText(/saved native source could not be read/);
});

test('malformed native JSON is reported without sending a renderer command',async()=>{
 const current=design();current.brief.domain='structured_diagram';await boot(current);fireEvent.change(screen.getByLabelText('Structured diagram JSON'),{target:{value:'{bad'}});fireEvent.click(button('Render a saved native candidate'));await screen.findByRole('alert');expect(api.sendCommand).not.toHaveBeenCalled();
});

test('cancel retains immutable candidates and surfaces bounded failed work',async()=>{
 const current=design();current.versions=[version()];current.operation_failures=[{id:'operation_failed',kind:'generation',error:'worker_unknown'}];await boot(current);expect(screen.getByText(/worker unknown/)).toBeVisible();fireEvent.click(button('Stop visual work'));await screen.findByText(/Visual cancellation acknowledged/);expect(button('Stop visual work')).toBeDisabled();expect(screen.getByRole('img')).toBeVisible();
});

test.each(['unknown','confirmed','retry','missing_retry'])('pending receipt %s requires same-action recovery',async mode=>{
 const current=design();api.pendingCommand.mockReturnValue({id:'command_pending',intent:'designimage:design_one:hash'});api.reconcileCommand.mockResolvedValue(mode==='unknown'?{confirmed:false}:{confirmed:true,result:current});api.retryPendingCommand.mockResolvedValue(mode==='missing_retry'?undefined:current);
 await boot();await screen.findByText('Visual action not confirmed');fireEvent.click(button(mode.includes('retry')?'Retry the same action':'Reconcile original action'));
 if(mode==='unknown')await screen.findByText(/not yet confirmed/);else if(mode!=='missing_retry')await screen.findByRole('heading',{name:'Synthetic visual purpose'});
 expect(mode.includes('retry')?api.retryPendingCommand:api.reconcileCommand).toHaveBeenCalledTimes(1);
});

test('corrupt receipt and rejected commands stay visible',async()=>{
 await boot(design());api.pendingCommand.mockImplementation(()=>{throw new Error('unreadable')});api.sendCommand.mockRejectedValueOnce(new Error('Synthetic rejection'));fireEvent.click(button('Stop visual work'));await screen.findByText('An earlier action is unconfirmed. Reconcile its receipt before continuing.');
});

test('access change clears private brief, native source and history',async()=>{
 const current=design();current.brief.domain='native_interface';current.versions=[version(1,'native_interface')];await boot(current);await waitFor(()=>expect(screen.getByLabelText('Native HTML source')).toHaveValue('<p>Saved native source</p>'));act(()=>access());expect(screen.queryByRole('heading',{name:'Synthetic visual purpose'})).not.toBeInTheDocument();expect(screen.queryByDisplayValue('<p>Saved native source</p>')).not.toBeInTheDocument();expect(screen.getByRole('alert')).toHaveTextContent('Account access changed');
});

test('unfunded capabilities explain the local boundary',async()=>{capabilities.paid_access_enabled=false;await boot();expect(screen.getByLabelText('Maximum provider allowance (USD)')).toBeDisabled();expect(screen.getByText(/No funded image\/vision configuration/)).toBeVisible();});

test('pending work polling never regresses revision, replays work or loses the last saved candidate',async()=>{
 vi.useFakeTimers({shouldAdvanceTime:true});const current=design();current.revision=5;current.versions=[version()];current.pending_operations=[{id:'job_one',kind:'review',status:'running'}];await boot(current);
 const stale={...copy(current),revision:4,brief:{...current.brief,purpose:'Stale private purpose'}};api.readApi.mockResolvedValueOnce(stale);await act(async()=>{await vi.advanceTimersByTimeAsync(1200);});expect(screen.getByRole('heading',{name:'Synthetic visual purpose'})).toBeVisible();expect(screen.queryByText('Stale private purpose')).not.toBeInTheDocument();
 api.readApi.mockRejectedValueOnce(new Error('connection lost'));await act(async()=>{await vi.advanceTimersByTimeAsync(1200);});expect(screen.getByText(/Connection interrupted/)).toBeVisible();expect(screen.getByRole('img')).toBeVisible();
 api.readApi.mockResolvedValueOnce({...copy(current),revision:6,pending_operations:[],versions:[version(),version(2)]});await act(async()=>{await vi.advanceTimersByTimeAsync(1200);});expect(button('Inspect V2')).toBeVisible();expect(api.sendCommand).not.toHaveBeenCalled();
 const calls=api.readApi.mock.calls.length;await act(async()=>{await vi.advanceTimersByTimeAsync(2400);});expect(api.readApi).toHaveBeenCalledTimes(calls);
});

test.each(['resolve','reject'])('pending poll %s after leaving the brief cannot restore private state',async outcome=>{
 vi.useFakeTimers({shouldAdvanceTime:true});const current=design();current.versions=[version()];current.pending_operations=[{id:'job_one',kind:'review',status:'running'}];await boot(current);
 let resolve!:(value:unknown)=>void;let reject!:(value:unknown)=>void;api.readApi.mockReturnValueOnce(new Promise((yes,no)=>{resolve=yes;reject=no;}));await act(async()=>{await vi.advanceTimersByTimeAsync(1200);});fireEvent.click(button('New visual brief'));
 await act(async()=>{if(outcome==='resolve')resolve({...copy(current),revision:7});else reject(new Error('late failure'));});expect(screen.queryByRole('heading',{name:'Synthetic visual purpose'})).not.toBeInTheDocument();expect(screen.queryByText(/Connection interrupted/)).not.toBeInTheDocument();
});

test.each(['resolve','reject'])('saved source %s after closing does not restore source or display a stale error',async outcome=>{
 const current=design();current.brief.domain='native_interface';current.versions=[version(1,'native_interface')];let resolve!:(value:string)=>void;let reject!:(value:unknown)=>void;api.readDesignSource.mockReturnValueOnce(new Promise<string>((yes,no)=>{resolve=yes;reject=no;}));await boot(current);fireEvent.click(button('New visual brief'));
 await act(async()=>{if(outcome==='resolve')resolve('late source');else reject(new Error('late failure'));});expect(screen.queryByRole('alert')).not.toBeInTheDocument();expect(screen.queryByDisplayValue('late source')).not.toBeInTheDocument();
});

test('saved diagram source is parsed as data and displayed with readable formatting',async()=>{
 const current=design();current.brief.domain='structured_diagram';current.versions=[version(1,'native_diagram')];api.readDesignSource.mockResolvedValueOnce('{"nodes":[]}');await boot(current);await waitFor(()=>expect(screen.getByLabelText('Structured diagram JSON')).toHaveValue('{\n  "nodes": []\n}'));expect(api.readDesignSource).toHaveBeenCalledWith('version_1','json');
});

test('generation without an extra edit intent uses the frozen purpose and an empty recipient list is explicit',async()=>{
 const current=design();current.recipients=[];await boot(current);expect(screen.getByText(/Approved recipients: None/)).toBeVisible();fireEvent.click(button('Prepare original generation'));await screen.findByRole('heading',{name:'Confirm the exact visual work.'});fireEvent.click(screen.getByLabelText('I approve this exact input scope, action and original allowance.'));fireEvent.click(button('Start bounded visual generate'));await screen.findByText(/Visual work accepted/);expect(api.sendCommand.mock.calls.at(-1)?.[2]).toMatchObject({intent:current.brief.purpose,target_version_id:null});
});

test('confirmed non-design receipt does not pretend an unrelated result is a candidate',async()=>{
 api.pendingCommand.mockReturnValue({id:'command_pending',intent:'other'});api.reconcileCommand.mockResolvedValue({confirmed:true,result:{unrelated:true}});await boot();fireEvent.click(button('Reconcile original action'));await waitFor(()=>expect(api.reconcileCommand).toHaveBeenCalled());expect(screen.queryByRole('heading',{name:'Synthetic visual purpose'})).not.toBeInTheDocument();
});

test('new raster generation uses configured dimensions and rejects unsupported frozen form sizes',async()=>{
 capabilities.supported_sizes=['1536x1024'];await boot();
 expect(screen.getByLabelText('Width (pixels)')).toHaveValue(1536);expect(screen.getByLabelText('Height (pixels)')).toHaveValue(1024);
 fireEvent.change(screen.getByLabelText('Mode'),{target:{value:'improve'}});
 expect(screen.getByLabelText('Generation size')).toHaveValue('1536x1024');
 fireEvent.change(screen.getByLabelText('Purpose'),{target:{value:'Respect the configured endpoint'}});fireEvent.change(screen.getByLabelText('Audience'),{target:{value:'Reviewer'}});
 fireEvent.change(screen.getByLabelText('Width (pixels)'),{target:{value:'1024'}});fireEvent.click(button('Save frozen visual brief'));
 await screen.findByText('Choose a configured generation size before freezing this Improve brief.');expect(api.sendCommand).not.toHaveBeenCalled();
 fireEvent.change(screen.getByLabelText('Generation size'),{target:{value:'1536x1024'}});fireEvent.click(button('Save frozen visual brief'));await screen.findByRole('heading',{name:'Respect the configured endpoint'});
 expect(api.sendCommand.mock.calls[0][2].brief).toMatchObject({width:1536,height:1024});
});

test('visual comparison paging preserves prior options and can reopen a design outside first page',async()=>{
 meetingItems=[{id:'meeting_one',title:'First comparison'}];const other={...design(),id:'design_two',meeting_id:'meeting_two',brief:{...design().brief,purpose:'Paged visual brief'}};
 const initial=api.readApi.getMockImplementation()!;
 api.readApi.mockImplementation(async path=>path==='/api/meetings'?{items:meetingItems,next_cursor:'next/page'}:path.includes('?cursor=')?{items:[...meetingItems,{id:'meeting_two',title:'Second comparison'}],next_cursor:null}:path==='/api/meetings/meeting_two/design-sessions'?{items:[other]}:path==='/api/design-sessions/design_two'?copy(other):initial(path));
 await boot();fireEvent.click(button('Load more saved comparisons'));await screen.findByRole('option',{name:'Second comparison'});
 expect(within(screen.getByLabelText('Saved comparison')).getAllByRole('option')).toHaveLength(2);expect(api.readApi).toHaveBeenCalledWith('/api/meetings?cursor=next%2Fpage');
 fireEvent.change(screen.getByLabelText('Saved comparison'),{target:{value:'meeting_two'}});fireEvent.click(await screen.findByRole('button',{name:'Paged visual brief · 0 versions'}));await screen.findByRole('heading',{name:'Paged visual brief'});
});

test('pending image after reload permits owner-scoped reopening and exact file reselection',async()=>{
 const current=design();saved=[current];let receipt:any={id:'command_pending',intent:'designimage:design_one:hash',path:'/api/design-sessions/design_one/images',encoding:'binary',ownerId:'owner_test'};
 api.pendingCommand.mockImplementation(()=>receipt);api.reconcileCommand.mockResolvedValue({confirmed:false});
 api.uploadDesignImage.mockImplementationOnce(async()=>{receipt=null;return {...copy(current),versions:[version()],upload_status:'ready'};});
 await boot();fireEvent.click(button('Reconcile original action'));await screen.findByText(/not yet confirmed/);
 expect(button('Synthetic visual purpose · 0 versions')).toBeEnabled();fireEvent.click(button('Open original upload to reselect image'));
 await screen.findByLabelText('Upload original PNG or JPEG');const file=new File(['exact original bytes'],'original.png',{type:'image/png'});
 fireEvent.change(screen.getByLabelText('Upload original PNG or JPEG'),{target:{files:[file]}});
 await screen.findByText('Original V1 saved. Metadata was removed. No model review has been claimed.');expect(api.uploadDesignImage).toHaveBeenCalledWith('design_one',1,file);expect(screen.queryByText('Visual action not confirmed')).not.toBeInTheDocument();
});

test('pending image can reopen its matching design when its meeting is outside loaded options',async()=>{
 const other={...design(),id:'design_other',meeting_id:'meeting_other'};api.pendingCommand.mockReturnValue({id:'pending',intent:'designimage:design_other:hash',path:'/api/design-sessions/design_other/images',encoding:'binary'});
 const initial=api.readApi.getMockImplementation()!;
 api.readApi.mockImplementation(async path=>path==='/api/design-sessions/design_other'?other:path==='/api/meetings/meeting_other'?{id:'meeting_other',title:'Previously unlisted comparison'}:path==='/api/meetings/meeting_other/design-sessions'?{items:[other]}:initial(path));
 await boot();fireEvent.click(button('Open original upload to reselect image'));await screen.findByLabelText('Upload original PNG or JPEG');
 expect(screen.getByLabelText('Saved comparison')).toHaveValue('meeting_other');expect(screen.getByRole('option',{name:'Previously unlisted comparison'})).toBeInTheDocument();
});

test.each(['generate','edit'])('long frozen purpose keeps every requirement while sending bounded default %s intent',async action=>{
 const current=design();current.brief.purpose='Long purpose '.repeat(250).trim();if(action==='edit'){current.versions=[version()];current.versions[0].coverage.complete=true;}
 await boot(current);fireEvent.click(button(action==='generate'?'Prepare original generation':'Prepare next-version edit'));await screen.findByRole('heading',{name:'Confirm the exact visual work.'});
 fireEvent.click(screen.getByLabelText('I approve this exact input scope, action and original allowance.'));fireEvent.click(button(`Start bounded visual ${action}`));await screen.findByText(/Visual work accepted/);
 const body=api.sendCommand.mock.calls.at(-1)![2];expect(body.intent.length).toBeLessThanOrEqual(2000);expect(body.intent).toContain('complete frozen visual brief');expect(screen.getByRole('heading',{name:current.brief.purpose})).toBeInTheDocument();
});

test.each([false, true])('native action plan survives rendering another revision with persisted plan=%s', async persisted => {
 const current=design();current.brief.domain='native_interface';await boot(current);
 const plan=[{action:'click',selector:'#submit',assertion:{selector:'#result',text:'Saved'}}];
 api.sendCommand.mockImplementation(async(_intent,path,body)=>{
  const next=version(current.versions.length+1,'native_interface');
  if(persisted)next.provenance={interaction_plan:body.actions};
  current.versions.push(next);current.revision++;saved=[current];return copy(current);
 });
 fireEvent.change(screen.getByLabelText('Native HTML source'),{target:{value:'<button id="submit">Save</button><p id="result">Waiting</p>'}});
 fireEvent.change(screen.getByLabelText('Declared browser actions and assertions (JSON)'),{target:{value:JSON.stringify(plan)}});
 fireEvent.click(button('Render a saved native candidate'));await screen.findByText(/Native source and its exact rendered pixels/);
 expect(JSON.parse((screen.getByLabelText('Declared browser actions and assertions (JSON)') as HTMLTextAreaElement).value)).toEqual(plan);
 await waitFor(()=>expect(screen.getByLabelText('Native HTML source')).toHaveValue('<p>Saved native source</p>'));
 fireEvent.change(screen.getByLabelText('Native HTML source'),{target:{value:'<p>Second revision still needs the same functional check</p>'}});
 fireEvent.click(button('Render a saved native candidate'));await waitFor(()=>expect(api.sendCommand).toHaveBeenCalledTimes(2));
 expect(api.sendCommand.mock.calls[1][2].actions).toEqual(plan);
});

test('reopening native interface restores its private version-bound interaction plan', async()=>{
 const current=design();current.brief.domain='native_interface';const prior=version(1,'native_interface');
 const plan=[{action:'click',selector:'#submit',assertion:{selector:'#result',text:'Saved'}}];prior.provenance={interaction_plan:plan};current.versions=[prior];
 await boot(current);
 expect(JSON.parse((screen.getByLabelText('Declared browser actions and assertions (JSON)') as HTMLTextAreaElement).value)).toEqual(plan);
 fireEvent.click(button('New visual brief'));fireEvent.click(button(/Synthetic visual purpose · 1 versions/));await screen.findByLabelText('Declared browser actions and assertions (JSON)');
 expect(JSON.parse((screen.getByLabelText('Declared browser actions and assertions (JSON)') as HTMLTextAreaElement).value)).toEqual(plan);
});

test.each([[100,100],[1024,1024],[1536,1024]])('diagram starter matches frozen %sx%s dimensions',async(width,height)=>{
 const current=design();current.brief.domain='structured_diagram';current.brief.width=width;current.brief.height=height;await boot(current);
 const source=JSON.parse((screen.getByLabelText('Structured diagram JSON') as HTMLTextAreaElement).value);
 expect(source).toMatchObject({width,height});
 for(const node of source.nodes){expect(node.x).toBeGreaterThanOrEqual(0);expect(node.y).toBeGreaterThanOrEqual(0);expect(node.x+node.width).toBeLessThanOrEqual(width);expect(node.y+node.height).toBeLessThanOrEqual(height);}
 fireEvent.click(button('Render a saved native candidate'));await waitFor(()=>expect(api.sendCommand).toHaveBeenCalled());
 expect(api.sendCommand.mock.calls.at(-1)?.[2].diagram).toMatchObject({width,height});
});

test.each(['abandoned','committed'])('visual command abandonment handles %s without replay',async status=>{
 const current=design();api.pendingCommand.mockReturnValue({id:'command_pending',intent:'private_native',path:'/api/design-sessions/design_one/native-interface'});
 api.abandonPendingCommand.mockImplementation(async()=>{api.pendingCommand.mockReturnValue(null);return {status,result:status==='committed'?current:{}};});
 await boot();fireEvent.click(button('Abandon unconfirmed action'));
 await screen.findByText(status==='abandoned'?/original visual action was safely abandoned/:/original action was already saved/);
 expect(api.abandonPendingCommand).toHaveBeenCalledOnce();expect(api.sendCommand).not.toHaveBeenCalled();
 expect(screen.queryByText('Visual action not confirmed')).not.toBeInTheDocument();
});

test('expired visual sessions cannot upload or queue another review',async()=>{
 const current=design();current.status='expired';await boot(current);
 expect(screen.queryByLabelText('Upload original PNG or JPEG')).not.toBeInTheDocument();
 expect(button('Prepare original generation')).toBeDisabled();
});
