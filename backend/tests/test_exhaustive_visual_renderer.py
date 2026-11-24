"""Native-render isolation adversaries exercise real pipes and bound receipts."""
import base64
import hashlib
import json
import sys
from dataclasses import replace
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from backend.tests.test_visual import png
from round_robin.visual.renderers import interface as native
from round_robin.visual.renderers.diagram import Diagram, render_diagram
from round_robin.visual.schemas import Critique, Finding, ImageCapabilities, VisualBrief


@pytest.mark.parametrize('changes', [dict(image=';shell'), dict(browser_timeout_ms=True), dict(browser_timeout_ms=249),dict(browser_timeout_ms=20001),dict(host_timeout_seconds=26),dict(host_timeout_seconds=1),dict(enabled=False),dict(image='')])
def test_native_configuration_fails_closed(changes):
    config=replace(native.NativeRendererConfiguration(True,'test-image'),**changes)
    with pytest.raises(native.IsolatedRendererRequired):config.validate()


@pytest.mark.parametrize('body', [dict(selector='x'),dict(selector='x',text=None,value=None,visible=None)])
def test_assertions_require_a_real_expected_condition(body):
    with pytest.raises(ValidationError):native.DOMAssertion(**body)


@pytest.mark.parametrize('body', [dict(action='fill',selector='x'),dict(action='click',selector='x',value='private'),dict(action='press',selector='x'),dict(action='click',selector='x',key='Enter')])
def test_native_actions_reject_inapplicable_arguments(body):
    with pytest.raises(ValidationError):native.InterfaceAction(**body)


def test_bounded_process_streams_real_pipes_without_echoing_stderr():
    code,out,err=native._bounded_process([sys.executable,'-c','import sys;data=sys.stdin.buffer.read();sys.stdout.buffer.write(data);sys.stderr.write("private diagnostic")'],b'fixed input',timeout=3,stdout_limit=100)
    assert (code,out,err)==(0,b'fixed input',b'private diagnostic')


@pytest.mark.parametrize('stream', ['stdout','stderr'])
def test_bounded_process_kills_excess_output(stream):
    with pytest.raises(native.NativeRendererError,match='bounded output'):
        native._bounded_process([sys.executable,'-c',f'import sys;sys.{stream}.write("x"*10000)'],b'',timeout=3,stdout_limit=1,stderr_limit=1)


def test_bounded_process_kills_timeout_and_refuses_missing_executable():
    with pytest.raises(native.NativeRendererTimeout):
        native._bounded_process([sys.executable,'-c','import time;time.sleep(5)'],b'',timeout=.02,stdout_limit=10)
    with pytest.raises(native.IsolatedRendererRequired):
        native._bounded_process(['/nonexistent-round-robin-audit-command'],b'',timeout=1,stdout_limit=10)


def test_bounded_process_cleans_child_on_writer_thread_launch_failure(monkeypatch):
    monkeypatch.setattr(native.threading.Thread,'start',lambda self: (_ for _ in ()).throw(RuntimeError('synthetic thread limit')))
    with pytest.raises(RuntimeError,match='synthetic thread limit'):
        native._bounded_process([sys.executable,'-c','import time;time.sleep(5)'],b'',timeout=1,stdout_limit=10)


def test_bounded_process_writer_handles_early_child_exit():
    code,out,err=native._bounded_process([sys.executable,'-c','pass'],b'x'*200000,timeout=3,stdout_limit=10)
    assert code==0 and not out and not err


@pytest.fixture
def renderer(monkeypatch):
    state={'change':lambda x:x, 'calls':[],'run_code':0,'inspect_code':0,'inspect_body':b'sha256:'+b'a'*64,'cleanup_fail':False}
    def process(args,content,**kwargs):
        state['calls'].append((args,content,kwargs))
        if args[1]=='image':return state['inspect_code'],state['inspect_body'],b''
        if args[1]=='rm':
            if state['cleanup_fail']:raise native.NativeRendererError('synthetic cleanup')
            return 0,b'',b''
        packet=json.loads(content)
        image=png((packet['viewport']['width'],packet['viewport']['height']))
        checks=[{'index':i,'action':a['action'],'selector_sha256':hashlib.sha256(a['selector'].encode()).hexdigest(),'status':'passed','assertion':'passed' if a['assertion'] else 'not_requested'} for i,a in enumerate(packet['actions'])]
        result={'schema_version':'1.0','renderer_version':native.RENDERER_VERSION,'playwright_version':'test','browser_version':'test','source_sha256':packet['source_sha256'],'configuration_hash':packet['configuration_hash'],'viewport':packet['viewport'],'screenshot_sha256':hashlib.sha256(image).hexdigest(),'screenshot_base64':base64.b64encode(image).decode(),'checks':checks,'network_attempts':[],'network_attempt_count':0,'page_error_count':0,'limitations':[]}
        state['change'](result)
        return state['run_code'],json.dumps(result).encode(),b'ignored private diagnostics'
    monkeypatch.setattr(native,'_bounded_process',process)
    return state


def render(actions=None,**kwargs):
    return native.render_interface('<button id="x">Fixed fixture</button>',(100,100),actions,configuration=native.NativeRendererConfiguration(True,'test-image'),**kwargs)


def test_native_receipt_binds_every_identity_and_declared_action(renderer):
    actions=[{'action':'click','selector':'#x'},{'action':'fill','selector':'#x','value':'ok','assertion':{'selector':'#x','value':'ok'}},{'action':'press','selector':'#x','key':'Enter','assertion':{'selector':'#x','text':'ok','visible':True}}]
    result=render(actions)
    assert result.image_bytes==result.raster.canonical_bytes
    assert len(result.snapshot.checks)==3
    args,source,_=renderer['calls'][1]
    assert '--network=none' in args and '--read-only' in args and '--pull=never' in args
    assert not any('volume' in arg for arg in args)
    assert b'Fixed fixture' in source and not any('Fixed fixture' in arg for arg in args)
    assert renderer['calls'][-1][0][1:3]==['rm','--force']
    renderer['cleanup_fail']=True
    assert render().source.startswith('<button')


@pytest.mark.parametrize('field,value', [
 ('screenshot_sha256','0'*64),('source_sha256','0'*64),('configuration_hash','0'*64),
 ('viewport',{'width':101,'height':100}),('network_attempt_count',1),
 ('screenshot_base64','invalid-base64'),('checks',[]),
])
def test_forged_renderer_receipts_never_publish(renderer,field,value):
    renderer['change']=lambda result:result.update({field:value})
    with pytest.raises(native.NativeRendererError,match='validation'):render([{'action':'click','selector':'#x'}])


@pytest.mark.parametrize('change', [
 {'index':1},{'action':'fill'},{'selector_sha256':'0'*64},{'assertion':'pending'},
 {'assertion':'passed'},
])
def test_renderer_check_cannot_invent_or_change_action(renderer,change):
    renderer['change']=lambda result:result['checks'][0].update(change)
    with pytest.raises(native.NativeRendererError):render([{'action':'click','selector':'#x'}])


@pytest.mark.parametrize('status,assertion,allowed', [('passed','passed',True),('failed','failed',True),('failed','unavailable',True),('passed','failed',False),('passed','unavailable',False),('failed','not_requested',False)])
def test_renderer_assertion_outcomes_are_consistent(renderer,status,assertion,allowed):
    renderer['change']=lambda result:result['checks'][0].update(status=status,assertion=assertion)
    action=[{'action':'click','selector':'#x','assertion':{'selector':'#x','visible':True}}]
    if allowed:assert render(action).checks[0]['status']==status
    else:
        with pytest.raises(native.NativeRendererError):render(action)


@pytest.mark.parametrize('code,error', [(124,native.NativeRendererTimeout),(1,native.NativeRendererError)])
def test_native_failed_execution_is_classified_without_private_stderr(renderer,code,error):
    renderer['run_code']=code
    with pytest.raises(error) as failure:render()
    assert 'private' not in str(failure.value)


def test_native_inspection_and_all_input_resource_bounds(renderer):
    renderer['inspect_body']=b'not an image id'
    with pytest.raises(native.IsolatedRendererRequired):render()
    for source in [None,' ','x'*(128*1024+1)]:
        with pytest.raises(native.NativeRendererError):native.render_interface(source,configuration=native.NativeRendererConfiguration(True,'test-image'))
    for viewport in [100,(100,),[100,100,100]]:
        with pytest.raises(native.NativeRendererError):native.render_interface('x',viewport,configuration=native.NativeRendererConfiguration(True,'test-image'))
    for actions in [{},[{}]*21]:
        with pytest.raises(native.NativeRendererError):render(actions)
    renderer['inspect_body']=b'sha256:'+b'a'*64
    with pytest.raises(native.NativeRendererError,match='160 KiB'):
        native.render_interface('x'*128*1024,(100,100),[{'action':'fill','selector':'x','value':'x'*2000}]*20,configuration=native.NativeRendererConfiguration(True,'test-image'))


def test_diagram_duplicate_labels_bounds_and_escaping(monkeypatch):
    node={'id':'node_one','label':'Small','x':10,'y':10}
    for changes in [{'nodes':[node,node]},{'width':4000,'height':4000},{'nodes':[{**node,'width':60,'label':'x'*100}]}]:
        with pytest.raises(ValidationError):Diagram.model_validate({'width':500,'height':200,'nodes':[node],**changes})
    result=render_diagram(Diagram.model_validate({'width':500,'height':200,'nodes':[node]}))
    assert b'<svg' in result.svg
    from round_robin.visual.renderers import diagram
    monkeypatch.setattr(diagram,'tostring',lambda *args,**kwargs:b'x'*(1024*1024+1))
    with pytest.raises(ValueError,match='SVG exceeds'):render_diagram(Diagram.model_validate({'width':500,'height':200,'nodes':[node]}))
