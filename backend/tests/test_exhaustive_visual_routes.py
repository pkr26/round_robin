"""Fresh HTTP adversaries for visual inputs, immutable preflights and failures."""
from copy import deepcopy
from dataclasses import asdict, replace
from datetime import timedelta
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import select

from backend.tests.test_visual import visual_app, png, upload_original, caps, critique
from backend.tests.test_image_provider import image_configuration
from round_robin.api import visual as routes
from round_robin.database.models import Command, utcnow
from round_robin.database.visual_models import DesignOperation, DesignReview, DesignSession, DesignVersion
from round_robin.visual.iterations import VisualLimit, VisualState
from round_robin.visual.service import commit_review
from round_robin.visual.reviews import PixelView, issue_review_receipt


def command(**values):return {'command_id':str(uuid4()),**values}

def upload(client,design,data=None,**changes):
    data=png() if data is None else data
    headers={'Content-Type':'image/png','Content-Length':str(len(data)),'X-Command-Id':str(uuid4()),'X-Expected-Revision':str(design['revision']),'X-Content-Sha256':hashlib.sha256(data).hexdigest(),**changes}
    return client.post(f"/api/design-sessions/{design['id']}/images",content=data,headers=headers)


@pytest.mark.parametrize('headers,status', [({'Content-Type':'application/octet-stream'},415),({'Content-Length':'not-number'},422),({'Content-Length':'0'},422),({'Content-Length':'10485761'},422),({'X-Content-Sha256':'0'*63},422),({'X-Content-Sha256':'g'*64},422),({'X-Expected-Revision':'99'},409)])
def test_upload_invalid_contracts_fail_before_private_bytes_persist(visual_app,headers,status):
    app,client,_,design=visual_app
    response=upload(client,design,**headers);assert response.status_code==status,response.text
    with app.state.session_factory() as session:
        assert session.scalar(select(DesignVersion)) is None
        assert session.scalar(select(DesignOperation)) is None


@pytest.mark.parametrize('variant', ['incomplete','hash','bytes','quota','oversize'])
def test_failed_uploads_release_reservations_and_resolve_receipt(visual_app,monkeypatch,variant):
    app,client,_,design=visual_app;headers={};data=png()
    if variant=='incomplete':headers={'Content-Length':str(len(data)+1)}
    elif variant=='hash':headers={'X-Content-Sha256':'0'*64}
    elif variant=='bytes':data=b'not actual pixels'
    elif variant=='quota':monkeypatch.setattr(routes,'reserve_asset_storage',lambda *_:(_ for _ in ()).throw(VisualLimit('quota test')))
    elif variant=='oversize':
        monkeypatch.setattr(routes,'MAX_UPLOAD_BYTES',len(data)-1);headers={'Content-Length':'1'}
    response=upload(client,design,data,**headers);assert response.status_code==(429 if variant=='quota' else 422),response.text
    with app.state.session_factory() as session:
        assert session.scalar(select(DesignVersion)) is None
        for operation in session.scalars(select(DesignOperation)):assert operation.payload['status']=='released'


def test_upload_replay_original_exclusion_selection_and_missing_reads(visual_app):
    app,client,_,design=visual_app;identity=str(uuid4());data=png();headers={'X-Command-Id':identity}
    first=upload(client,design,data,**headers);assert first.status_code==201
    assert upload(client,design,data,**headers).json()==first.json()
    assert upload(client,first.json()).status_code==409
    current=first.json();path=f"/api/design-sessions/{design['id']}/selection";body=command(expected_revision=current['revision'],version_id=None)
    response=client.post(path,json=body);assert response.status_code==200
    assert client.post(path,json=body).json()==response.json()
    assert client.post(path,json=command(expected_revision=current['revision'],version_id=None)).status_code==409
    assert client.post(path,json=command(expected_revision=response.json()['revision'],version_id='version_foreign')).status_code==404
    for suffix in ['image','source.svg','source.json','source.html']:
        assert client.get(f'/api/design-versions/version_missing/{suffix}').status_code==404
    version_id=current['versions'][0]['id']
    for suffix in ['source.svg','source.json','source.html']:assert client.get(f'/api/design-versions/{version_id}/{suffix}').status_code==404
    with app.state.session_factory.begin() as session:
        version=session.get(DesignVersion,version_id);Path(version.asset_path).write_bytes(b'corrupt')
    assert client.get(f'/api/design-versions/{version_id}/image').status_code==503


def test_create_replay_funding_and_owner_quota(visual_app,monkeypatch):
    app,client,meeting,design=visual_app;path=f"/api/meetings/{meeting['id']}/design-sessions"
    body=command(mode='review_only',brief={'purpose':'New immutable scope','audience':'Test'})
    response=client.post(path,json=body);assert response.status_code==201
    assert client.post(path,json=body).json()==response.json()
    assert client.post(path,json={**body,'command_id':str(uuid4()),'maximum_usd':'1'}).status_code==409
    with app.state.session_factory.begin() as session:
        original=session.get(DesignSession,design['id'])
        for index in range(28):
            session.add(DesignSession(id=f'design_quota_{index}',meeting_id=meeting['id'],owner_id='owner_local',mode=original.mode,status='active',revision=1,execution_fence=0,deployment_epoch=1,frozen_brief=original.frozen_brief,recipients=[],maximum_usd='0',configuration={},state=original.state))
    assert client.post(path,json={**body,'command_id':str(uuid4())}).status_code==429


@pytest.fixture
def funded(visual_app,image_configuration):
    app,client,meeting,design=visual_app
    app.state.settings=replace(app.state.settings,paid_access_enabled=True)
    with app.state.session_factory.begin() as session:
        row=session.get(DesignSession,design['id']);row.configuration=image_configuration;row.maximum_usd='2';state=VisualState(**row.state);state.maximum_usd='2';row.state=asdict(state)
    return app,client,meeting,client.get(f"/api/design-sessions/{design['id']}").json()


def preflight(client,design,action='review'):
    return client.post(f"/api/design-sessions/{design['id']}/preflight",json=command(expected_revision=design['revision'],action=action))


@pytest.mark.parametrize('change,action,fragment', [('no_image','review','original image'),('review_only','generate','Review-only'),('edit_missing','edit','Editing requires'),('recipients','review','frozen recipients'),('allowance','review','allowance'),('money','review','protected independent reviews'),('configuration','review','configuration changed'),('pending','review','still pending')])
def test_preflight_exposes_each_frozen_input_and_resource_blocker(funded,change,action,fragment):
    app,client,_,design=funded
    with app.state.session_factory.begin() as session:
        row=session.get(DesignSession,design['id'])
        if change=='recipients':row.recipients=[]
        elif change=='allowance':row.maximum_usd='3';row.state={**row.state,'maximum_usd':'3'}
        elif change=='money':row.maximum_usd='0.1';row.state={**row.state,'maximum_usd':'0.1'}
        elif change=='configuration':row.state={**row.state,'paid_admitted':True};row.configuration={**row.configuration,'operator_configuration_hash':'0'*64}
        elif change=='pending':session.add(DesignOperation(id='operation_pending',session_id=row.id,provider='anthropic',payload={'kind':'vision','status':'queued'}))
    response=preflight(client,design,action);assert response.status_code==200,response.text
    assert any(fragment in item for item in response.json()['blockers'])


def test_preflight_replay_revision_and_generate_after_original(funded):
    app,client,_,design=funded
    saved=upload_original(client,design)
    path=f"/api/design-sessions/{design['id']}/preflight";body=command(expected_revision=saved['revision'],action='generate')
    response=client.post(path,json=body);assert response.status_code==200
    assert any('V1 only' in item for item in response.json()['blockers'])
    assert client.post(path,json=body).json()==response.json()
    assert client.post(path,json={**body,'command_id':str(uuid4()),'expected_revision':99}).status_code==409
    with app.state.session_factory.begin() as session:
        row=session.get(DesignSession,design['id']);row.mode='improve';row.state={**row.state,'mode':'improve'}
    assert client.post(f"/api/meetings/{design['meeting_id']}/design-sessions",json=command(mode='improve',brief={'purpose':'test','audience':'test'},maximum_usd='3')).status_code==409


def test_review_admission_queues_once_and_rejects_stale_exact_scope(funded):
    app,client,_,design=funded;saved=upload_original(client,design);quote=preflight(client,saved).json()
    path=f"/api/design-sessions/{design['id']}/reviews";body=command(expected_revision=saved['revision'],preflight_id=quote['id'])
    with app.state.session_factory.begin() as session:
        row=session.get(DesignSession,design['id']);row.preflight={**row.preflight,'brief_hash':'0'*64}
    assert client.post(path,json=body).status_code==409
    quote=preflight(client,saved).json();body=command(expected_revision=saved['revision'],preflight_id=quote['id'])
    response=client.post(path,json=body);assert response.status_code==202,response.text
    assert len(response.json()['accepted_operation_ids'])==2
    assert client.post(path,json=body).json()==response.json()


@pytest.mark.parametrize('state', ['missing','terminal','reviewed'])
def test_review_refuses_no_image_terminal_or_already_reviewed(funded,state):
    app,client,_,design=funded
    saved=upload_original(client,design) if state!='missing' else design
    quote=preflight(client,saved).json()
    with app.state.session_factory.begin() as session:
        row=session.get(DesignSession,design['id']);row.preflight={**row.preflight,'blockers':[]}
        if state=='terminal':row.status='cancelled';row.state={**row.state,'status':'cancelled'}
        elif state=='reviewed':
            version=session.scalar(select(DesignVersion));raw=Path(version.asset_path).read_bytes()
            for provider in ['anthropic','google']:
                receipt=issue_review_receipt(caps(provider),version.id,version.canonical_hash,[PixelView(raw,version.canonical_hash)])
                commit_review(session,row,version,critique(receipt),receipt,expected_fence=0)
            row.revision=saved['revision']
    response=client.post(f"/api/design-sessions/{design['id']}/reviews",json=command(expected_revision=saved['revision'],preflight_id=quote['id']))
    assert response.status_code==409,response.text


@pytest.mark.parametrize('kind', ['diagram','interface'])
@pytest.mark.parametrize('failure', ['gate','scope','limit','required','quota','renderer'])
def test_native_command_gates_and_failure_receipts(visual_app,monkeypatch,kind,failure):
    app,client,meeting,_=visual_app
    env='ROUND_ROBIN_NATIVE_'+kind.upper()+'_ENABLED';monkeypatch.setenv(env,'1' if failure!='gate' else '0');monkeypatch.setenv('ROUND_ROBIN_NATIVE_RENDERER_IMAGE','synthetic')
    domain='structured_diagram' if kind=='diagram' else 'native_interface'
    created=client.post(f"/api/meetings/{meeting['id']}/design-sessions",json=command(mode='improve',brief={'purpose':'test','audience':'test','domain':domain,'width':500,'height':200,'required_text':['Required'] if failure=='required' else []})).json()
    source={'width':500,'height':200,'nodes':[{'id':'node_one','label':'Input','x':20,'y':20}]}
    body=command(expected_revision=99 if failure=='scope' else created['revision'],**({'diagram':source} if kind=='diagram' else {'source':'<p>Input</p>'}))
    if failure=='limit':
        with app.state.session_factory.begin() as session:
            row=session.get(DesignSession,created['id']);row.state={**row.state,'renderer_attempts':6}
    if failure=='quota':monkeypatch.setattr(routes,'reserve_asset_storage',lambda *_:(_ for _ in ()).throw(VisualLimit('quota')))
    if failure=='renderer':monkeypatch.setattr(routes,'render_diagram' if kind=='diagram' else 'render_interface',lambda *a,**k:(_ for _ in ()).throw(ValueError('PRIVATE synthetic error')))
    response=client.post(f"/api/design-sessions/{created['id']}/native-{kind}",json=body)
    expected={'gate':404,'scope':409,'limit':409,'required':422,'quota':429,'renderer':422}[failure]
    assert response.status_code==expected,response.text;assert 'PRIVATE' not in response.text
    if failure=='renderer':
        with app.state.session_factory() as session:
            assert session.get(Command,('owner_local',body['command_id'])).result['render_status']=='failed'
            assert session.get(DesignOperation,'visualrender_'+body['command_id'].replace('-','')).payload['status']=='released'


@pytest.mark.parametrize('kind', ['diagram','interface'])
def test_native_download_rejects_oversized_owned_source(visual_app,monkeypatch,kind):
    app,client,_,design=visual_app;saved=upload_original(client,design);version_id=saved['versions'][0]['id']
    with app.state.session_factory.begin() as session:
        version=session.get(DesignVersion,version_id);root=Path(version.asset_path).parent
        data=b'x'*(1024*1024+1 if kind=='diagram' else 128*1024+1);source=root/('native.svg' if kind=='diagram' else 'native.html');source.write_bytes(data)
        version.source_kind='native_diagram' if kind=='diagram' else 'native_interface'
        version.provenance={**version.provenance,'svg_path':str(source),'svg_hash':hashlib.sha256(data).hexdigest(),'source_path':str(source),'source_hash':hashlib.sha256(data).hexdigest()}
    assert client.get(f"/api/design-versions/{version_id}/source.{'svg' if kind=='diagram' else 'html'}").status_code==503
    if kind=='diagram':assert client.get(f'/api/design-versions/{version_id}/source.json').status_code==503
