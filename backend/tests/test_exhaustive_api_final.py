"""Independent final API boundary pass: replay, pagination and admission races."""
import asyncio
from dataclasses import replace
from datetime import timedelta
import json
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.tests.test_api import application,client,command,create,preflight,start
from backend.tests.test_provider_configuration import synthetic_configuration
from round_robin.api import events
from round_robin.database.models import Command,Draft,Event,Meeting,Outbox,Owner,PreflightRecord,Run,Step,Upload,utcnow
from round_robin.database.services import lock_policies


@pytest.mark.parametrize('case',['missing','foreign_owner','foreign_meeting','temporary','missing_hash','missing_recipient'])
def test_preflight_selected_attachment_rejects_each_scope_or_readiness_failure(application,client,case):
    meeting=create(client);other=create(client)
    with application.state.session_factory.begin() as session:
        draft=session.get(Draft,meeting['id']);draft.payload={**draft.payload,'selected_attachment_ids':['upload_selected']}
        if case!='missing':
            session.add(Upload(id='upload_selected',meeting_id=other['id'] if case=='foreign_meeting' else meeting['id'],owner_id='owner_foreign' if case=='foreign_owner' else 'owner_local',filename='private.txt',media_type='text/plain',byte_size=10,status='temporary' if case=='temporary' else 'ready',sha256=None if case=='missing_hash' else '1'*64,recipients=['openai'] if case=='missing_recipient' else ['openai','anthropic','google']))
    payload=command(expected_draft_revision=1)
    result=client.post(f"/api/meetings/{meeting['id']}/preflight",json=payload)
    assert result.status_code==409 and result.json()['error']['code']=='input_scope_invalid'
    with application.state.session_factory() as session:
        assert session.scalar(select(PreflightRecord)) is None
        assert session.get(Command,('owner_local',payload['command_id'])) is None


@pytest.mark.parametrize('mutation',['hash','size'])
def test_start_revalidates_selected_input_bytes_after_preflight_without_consuming_authority(application,client,mutation):
    meeting=create(client)
    with application.state.session_factory.begin() as session:
        draft=session.get(Draft,meeting['id']);draft.payload={**draft.payload,'selected_attachment_ids':['upload_selected']}
        session.add(Upload(id='upload_selected',meeting_id=meeting['id'],owner_id='owner_local',filename='private.txt',media_type='text/plain',byte_size=10,status='ready',sha256='1'*64,recipients=['google','openai','anthropic']))
    quote=preflight(client,meeting)
    assert quote['input_manifest']==[{'id':'upload_selected','sha256':'1'*64,'recipients':['openai','anthropic','google'],'byte_size':10}]
    with application.state.session_factory.begin() as session:
        row=session.get(Upload,'upload_selected')
        if mutation=='hash':row.sha256='2'*64
        else:row.byte_size=11
    payload=command(preflight_id=quote['id'],expected_draft_revision=1)
    result=start(client,meeting,quote,payload)
    assert result.status_code==409 and result.json()['error']['code']=='input_scope_invalid'
    with application.state.session_factory() as session:
        assert session.get(PreflightRecord,quote['id']).consumed_by_run_id is None
        assert session.scalar(select(Run)) is None
        assert session.get(Command,('owner_local',payload['command_id'])) is None


def test_oversized_operator_configuration_does_not_issue_live_authorization(application,client,tmp_path):
    path=tmp_path/'operator.json';path.write_text(' '*131073)
    application.state.settings=replace(application.state.settings,provider_config_file=path,paid_access_enabled=True)
    meeting=create(client)
    result=client.post(f"/api/meetings/{meeting['id']}/preflight",json=command(expected_draft_revision=1,mode='live'))
    assert result.status_code==422 and result.json()['error']['code']=='provider_configuration_invalid'
    with application.state.session_factory() as session:assert session.scalar(select(PreflightRecord)) is None


@pytest.mark.parametrize('bucket_index',[0,1])
def test_live_start_rechecks_policy_lock_set_after_funded_preflight(application,client,tmp_path,bucket_index):
    path=tmp_path/'synthetic-provider-config.json';path.write_text(json.dumps(synthetic_configuration()))
    application.state.settings=replace(application.state.settings,provider_config_file=path,paid_access_enabled=True)
    meeting=create(client)
    with application.state.session_factory.begin() as session:
        for policy in lock_policies(session,'owner_local'):policy.spending_locked=False
    response=client.post(f"/api/meetings/{meeting['id']}/preflight",json=command(expected_draft_revision=1,mode='live'))
    assert response.status_code==200 and response.json()['blockers']==[]
    quote=response.json()
    with application.state.session_factory.begin() as session:
        lock_policies(session,'owner_local')[bucket_index].spending_locked=True
    result=start(client,meeting,quote)
    assert result.status_code==409 and result.json()['error']['code']=='spending_locked'
    with application.state.session_factory() as session:
        assert session.get(PreflightRecord,quote['id']).consumed_by_run_id is None
        assert session.scalar(select(Run)) is None
        assert session.scalar(select(Outbox)) is None


def test_deployment_queue_limit_counts_other_owners_and_rolls_back_start(application,client):
    meeting=create(client);quote=preflight(client,meeting)
    with application.state.session_factory.begin() as session:
        session.add(Owner(id='owner_other'));session.flush()
        session.add(Meeting(id='meeting_other',owner_id='owner_other',title='Other private queue'));session.flush()
        for n in range(20):
            pre=PreflightRecord(id=f'preflight_other_{n}',meeting_id='meeting_other',owner_id='owner_other',draft_revision=1,draft_hash='1'*64,configuration_hash='2'*64,payload={},expires_at=utcnow()+timedelta(minutes=10))
            session.add(pre);session.flush()
            session.add(Run(id=f'run_other_{n}',meeting_id='meeting_other',owner_id='owner_other',preflight_id=pre.id,input_snapshot={},configuration={},status='queued',initial_admission_expires_at=utcnow()+timedelta(minutes=30)))
    payload=command(preflight_id=quote['id'],expected_draft_revision=1)
    result=start(client,meeting,quote,payload)
    assert result.status_code==429 and result.json()['error']['code']=='quota_exceeded'
    assert 'Other private queue' not in result.text
    with application.state.session_factory() as session:
        assert session.get(PreflightRecord,quote['id']).consumed_by_run_id is None
        assert session.scalar(select(Run).where(Run.owner_id=='owner_local')) is None
        assert session.get(Command,('owner_local',payload['command_id'])) is None


def test_messages_are_owner_scoped_bounded_and_cursor_stable(application,client):
    meeting=create(client);run=start(client,meeting).json();foreign=start(client,create(client)).json()
    with application.state.session_factory.begin() as session:
        for n in range(52):
            session.add(Step(id=f'message_{n:03}',run_id=run['id'],stage='proposal',role_slot_id=f'role_{n}',provider='openai',status='succeeded',result_json={'output':{'summary':f'Owned message {n}'}}))
        session.add(Step(id='message_pending',run_id=run['id'],stage='proposal',role_slot_id='pending',provider='google',status='pending'))
        session.add(Step(id='message_foreign',run_id=foreign['id'],stage='proposal',role_slot_id='foreign',provider='google',status='succeeded',result_json={'private':'foreign sentinel'}))
    path=f"/api/runs/{run['id']}/messages"
    first=client.get(path,params={'limit':500}).json()
    assert len(first['items'])==50 and first['next_cursor']=='message_049'
    second=client.get(path,params={'cursor':first['next_cursor'],'limit':50}).json()
    assert [item['id'] for item in second['items']]==['message_050','message_051'] and second['next_cursor'] is None
    minimum=client.get(path,params={'limit':0}).json()
    assert len(minimum['items'])==1 and minimum['next_cursor']=='message_000'
    empty=client.get(path,params={'cursor':'zzzz'}).json()
    assert empty=={'items':[],'next_cursor':None}
    assert 'foreign sentinel' not in str(first)+str(second)
    from round_robin.api.auth import require_owner
    application.dependency_overrides[require_owner]=lambda:'owner_intruder'
    assert client.get(path).status_code==404


@pytest.mark.parametrize('suffix',['report','reports/1'])
def test_pending_report_reads_do_not_materialize_or_invent_versions(application,client,suffix):
    run=start(client,create(client)).json()
    response=client.get(f"/api/runs/{run['id']}/{suffix}")
    assert response.status_code==404 and response.json()['error']['code']=='report_pending'
    assert response.json()['error']['retry_scope']=='read'


def test_manual_retry_replay_is_idempotent_and_retains_original_calls(application,client):
    run=start(client,create(client)).json()
    with application.state.session_factory.begin() as session:
        row=session.get(Run,run['id']);row.status='completed_with_limitations';row.stage='finished';row.active_seconds=150
        session.add(Step(id='retry_synthesis',run_id=row.id,stage='synthesis',role_slot_id='synthesizer',provider='openai',status='failed',error_code='timeout',calls=1,infrastructure_claims=1))
    body=command(expected_run_version=run['version'],stage='synthesis')
    response=client.post(f"/api/runs/{run['id']}/retries",json=body)
    assert response.status_code==200,response.text
    assert client.post(f"/api/runs/{run['id']}/retries",json=body).json()==response.json()
    with application.state.session_factory() as session:
        row=session.get(Run,run['id']);step=session.get(Step,'retry_synthesis')
        assert row.recovery_generation==1 and row.version==2 and row.active_seconds==150
        assert step.calls==step.infrastructure_claims==1 and step.status=='pending' and step.generation==1
        assert len(list(session.scalars(select(Event).where(Event.event_type=='stage.retry_requested'))))==1
        work=list(session.scalars(select(Outbox)))
        assert len(work)==1 and work[0].kind=='advance' and work[0].status=='pending'
        assert work[0].run_id==run['id']


def test_sse_database_disappearance_between_auth_and_batch_ends_without_private_delivery(application,client,monkeypatch):
    run=start(client,create(client)).json()
    original=Session.get
    async def connected():return False
    async def run_in_pool(fn):
        if fn.__name__=='read_batch':
            # A concurrent durable purge may remove the row after authorization.
            with monkeypatch.context() as patch:
                patch.setattr(Session,'get',lambda self,model,identity,*a,**kw:None if model is Run and identity==run['id'] else original(self,model,identity,*a,**kw))
                return fn()
        return fn()
    monkeypatch.setattr(events,'run_in_threadpool',run_in_pool)
    async def exercise():
        response=await events.stream_events(run['id'],SimpleNamespace(app=application,is_disconnected=connected),last_event_id=None,owner='owner_local')
        assert [part async for part in response.body_iterator]==[]
    asyncio.run(exercise())


def test_snapshot_of_unfinished_run_exposes_no_invented_report_or_retry(application,client):
    run=start(client,create(client)).json()
    response=client.get(f"/api/runs/{run['id']}/snapshot")
    assert response.status_code==200,response.text
    snapshot=response.json()
    assert snapshot['report'] is None and snapshot['retry_eligible'] is False
    assert snapshot['retryable_stages']==[] and snapshot['event_cutoff']==1
    assert [item['event_type'] for item in snapshot['events']]==['run.queued']


def test_run_state_remains_inspectable_but_never_certifies_corrupt_saved_report(application,client):
    from round_robin.database.models import ReportVersion
    run=start(client,create(client)).json()
    original={'readiness':'ready','private':'private corrupted report sentinel'}
    with application.state.session_factory.begin() as session:
        session.add(ReportVersion(id='report_corrupt',run_id=run['id'],version=1,event_cutoff=1,content=original))
    response=client.get(f"/api/runs/{run['id']}")
    assert response.status_code==200 and response.json()['readiness'] is None
    assert 'private corrupted report sentinel' not in response.text
    with application.state.session_factory() as session:assert session.get(ReportVersion,'report_corrupt').content==original


@pytest.mark.parametrize('case',['version','stage'])
def test_retry_rejects_stale_version_or_nonexistent_stage_without_mutation(application,client,case):
    run=start(client,create(client)).json()
    with application.state.session_factory.begin() as session:
        row=session.get(Run,run['id']);row.status='completed_with_limitations';row.stage='finished'
        session.add(Step(id='retry_synthesis',run_id=row.id,stage='synthesis',role_slot_id='synthesizer',provider='openai',status='failed',error_code='timeout',calls=1,infrastructure_claims=1))
    body=command(expected_run_version=99 if case=='version' else 1,stage='synthesis' if case=='version' else 'audit')
    response=client.post(f"/api/runs/{run['id']}/retries",json=body)
    assert response.status_code==409 and response.json()['error']['code']=='retry_ineligible'
    with application.state.session_factory() as session:
        row=session.get(Run,run['id']);step=session.get(Step,'retry_synthesis')
        assert row.recovery_generation==0 and row.version==1 and step.status=='failed'
        assert len(list(session.scalars(select(Outbox))))==1
        assert session.get(Command,('owner_local',body['command_id'])) is None
