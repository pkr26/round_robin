"""Independent image-worker accounting, lease and no-send adversarial review."""
from contextlib import contextmanager
from dataclasses import asdict
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from backend.tests.test_image_worker import image_factory, seed_image, FakeImageAdapter
from backend.tests.test_image_provider import image_configuration, pixels
from backend.tests.test_orchestration import seed
from round_robin.database.models import Attempt,BudgetOperation,Meeting,Run,Step,utcnow
from round_robin.database.services import lock_policies
from round_robin.database.visual_models import DesignOperation,DesignSession,DesignVersion
from round_robin.orchestration.budget import operations_exposure
from round_robin.providers.base import ProviderFailure
from round_robin.visual.iterations import VisualState,VisualLimit
from round_robin.worker import image_tasks as worker


def test_image_heartbeat_thread_failure_is_proven_not_sent_and_settles_without_image(image_factory,image_configuration,tmp_path,monkeypatch):
    design_id,operation_id=seed_image(image_factory,image_configuration)
    adapter=FakeImageAdapter('openai',image_configuration,image_factory)
    def unavailable(self):raise RuntimeError('cannot start thread')
    monkeypatch.setattr(worker.threading.Thread,'start',unavailable)
    result=worker.execute_image_operation(operation_id,session_factory=image_factory,provider_registry={'openai':adapter},artifact_directory=tmp_path)
    assert result['status']=='persisted' and adapter.calls==0
    with image_factory() as session:
        ledger=session.get(BudgetOperation,operation_id)
        assert ledger.status=='settled' and Decimal(ledger.settled_usd)==0
        operation=session.get(DesignOperation,operation_id)
        assert operation.payload['status']=='failed' and operation.payload['error_code']=='image_heartbeat_unavailable'
        assert operation.payload['lease_owner'] is None
        storage=session.get(DesignOperation,operation.payload['storage_reservation_id'])
        assert storage.payload['status']=='released'
        assert session.scalar(select(DesignVersion)) is None


@pytest.mark.parametrize('expired',[False,True])
def test_image_capacity_uses_bounded_text_leases_without_refunding_text_money(image_factory,image_configuration,tmp_path,expired):
    design_id,operation_id=seed_image(image_factory,image_configuration)
    run_id=seed(image_factory,suffix='text')
    with image_factory.begin() as session:
        for n in range(3):
            step=Step(id=f'text_step_{n}',run_id=run_id,stage='proposal',role_slot_id=str(n),provider='openai',status='authorized',lease_expires_at=utcnow()+timedelta(seconds=-1 if expired else 90))
            session.add(step);session.flush()
            session.add(Attempt(id=f'text_attempt_{n}',run_id=run_id,step_id=step.id,provider='openai',status='authorized',reserved_usd='0.1',configuration={}))
    adapter=FakeImageAdapter('openai',image_configuration,image_factory)
    result=worker.execute_image_operation(operation_id,session_factory=image_factory,provider_registry={'openai':adapter},artifact_directory=tmp_path)
    assert result['status']==('persisted' if expired else 'capacity_wait')
    assert adapter.calls==int(expired)
    with image_factory() as session:
        assert all(item.status=='authorized' for item in session.scalars(select(Attempt)))


@pytest.mark.parametrize('kind',['unknown','not_sent','invalid_shape','refusal','malformed_image','invalid_cost','interrupt','known_cost','fenced'])
def test_image_receipt_and_publication_failures_do_not_hide_usage_or_resend(image_factory,image_configuration,tmp_path,kind):
    design_id,operation_id=seed_image(image_factory,image_configuration)
    class Adapter(FakeImageAdapter):
        def submit(self,packet,authorization,*,idempotency_key):
            self._received(packet,authorization)
            if kind=='unknown':raise RuntimeError('private upstream response')
            if kind=='not_sent':raise ProviderFailure('before_send',may_have_sent=False)
            if kind=='interrupt':raise KeyboardInterrupt()
            if kind=='invalid_shape':return []
            if kind=='refusal':return {'status':'refused','actual_usd':'0.02','provider_request_id':'owned_refusal'}
            if kind=='fenced':
                with image_factory.begin() as session:
                    design=session.get(DesignSession,design_id);design.execution_fence+=1
            return {'status':'completed','image_bytes':b'bad image' if kind=='malformed_image' else pixels(),'actual_usd':'NaN' if kind=='invalid_cost' else '0.03','provider_request_id':'owned_receipt'}
    adapter=Adapter('openai',image_configuration,image_factory)
    if kind=='interrupt':
        with pytest.raises(KeyboardInterrupt):worker.execute_image_operation(operation_id,session_factory=image_factory,provider_registry={'openai':adapter},artifact_directory=tmp_path)
    else:
        assert worker.execute_image_operation(operation_id,session_factory=image_factory,provider_registry={'openai':adapter},artifact_directory=tmp_path)['status']=='persisted'
        with image_factory() as session:
            ledger=session.get(BudgetOperation,operation_id);operation=session.get(DesignOperation,operation_id)
            if kind in {'unknown','invalid_shape','invalid_cost'}:assert ledger.status=='unknown'
            elif kind=='not_sent':assert ledger.status=='settled' and Decimal(ledger.settled_usd)==0
            else:assert ledger.status=='settled' and Decimal(ledger.settled_usd)>0
            if kind in {'known_cost','invalid_cost'}:assert operation.payload['status']=='completed'
            else:assert session.scalar(select(DesignVersion)) is None
            assert 'private upstream' not in str(operation.payload)
    worker.execute_image_operation(operation_id,session_factory=image_factory,provider_registry={'openai':adapter},artifact_directory=tmp_path)
    assert adapter.calls==1


@pytest.mark.parametrize('case',['missing','missing_scope','status','kind','recipient','mode','fence','paused','owner','price','expiry','pool_busy','duplicate'])
def test_image_authority_and_reservation_mutations_never_send(image_factory,image_configuration,tmp_path,case):
    design_id,operation_id=seed_image(image_factory,image_configuration)
    with image_factory.begin() as session:
        design=session.get(DesignSession,design_id);operation=session.get(DesignOperation,operation_id)
        if case=='missing_scope':session.delete(design)
        if case=='status':operation.payload={**operation.payload,'status':'completed'}
        if case=='kind':operation.payload={**operation.payload,'kind':'arbitrary'}
        if case=='recipient':design.recipients=['anthropic','google']
        if case=='mode':design.mode='critique_only'
        if case=='fence':design.execution_fence+=1
        if case=='paused':
            dep,_=lock_policies(session,design.owner_id);dep.metadata_json={**dep.metadata_json,'dispatch_locked':True}
        if case=='owner':operation.payload={**operation.payload,'owner_id':'foreign'}
        if case=='price':operation.payload={**operation.payload,'bound_usd':'0.001'}
        if case=='expiry':operation.payload={**operation.payload,'initial_admission_expires_at':(utcnow()-timedelta(seconds=1)).isoformat()}
        if case=='pool_busy':session.add(BudgetOperation(id=f'visualfinish_{design_id}',run_id=design_id,owner_id=design.owner_id,kind='visual_finish_hold',status='held',reserved_usd='0.1',metadata_json={'target_tag':'other','roles_remaining':['google']}))
        if case=='duplicate':session.add(BudgetOperation(id=operation_id,run_id=design_id,owner_id=design.owner_id,kind='visual_generation',status='reserved',reserved_usd='0.4',metadata_json={}))
    adapter=FakeImageAdapter('openai',image_configuration,image_factory)
    result=worker.execute_image_operation('missing' if case=='missing' else operation_id,session_factory=image_factory,provider_registry={'openai':adapter},artifact_directory=tmp_path)
    assert result['status'] in {'blocked','already_authorized_or_settled'} and adapter.calls==0


def test_image_authorization_database_error_has_safe_public_status(image_factory,image_configuration,tmp_path):
    _,operation_id=seed_image(image_factory,image_configuration)
    @contextmanager
    def broken():raise SQLAlchemyError('private SQL values');yield
    assert worker.execute_image_operation(operation_id,session_factory=broken,artifact_directory=tmp_path)=={'status':'authorization_unconfirmed'}


@pytest.mark.parametrize('case',['missing','wrong_owner','eligible','authority_failure','database_failure'])
def test_image_heartbeat_has_no_authority_to_extend_fenced_or_missing_work(image_factory,image_configuration,monkeypatch,case):
    design_id,operation_id=seed_image(image_factory,image_configuration)
    old=(utcnow()+timedelta(seconds=1)).isoformat()
    with image_factory.begin() as session:
        operation=session.get(DesignOperation,operation_id);operation.payload={**operation.payload,'status':'authorized','lease_owner':'token','lease_expires_at':old}
    class Stop:
        n=0
        def wait(self,seconds):self.n+=1;return self.n>1
    if case=='authority_failure':
        def deny(*a,**kw):raise VisualLimit('not_authorized')
        monkeypatch.setattr(worker,'_authority',deny)
    @contextmanager
    def broken():raise SQLAlchemyError('private');yield
    worker._heartbeat('missing' if case=='missing' else operation_id,'different' if case=='wrong_owner' else 'token',broken if case=='database_failure' else image_factory,Stop())
    with image_factory() as session:
        expiry=session.get(DesignOperation,operation_id).payload['lease_expires_at']
        assert (expiry!=old)==(case=='eligible')


@pytest.mark.parametrize('case',['missing_after_send','persistence_failure','late_known','late_not_sent'])
def test_image_late_or_unconfirmed_persistence_never_authorizes_another_send(image_factory,image_configuration,tmp_path,monkeypatch,case):
    design_id,operation_id=seed_image(image_factory,image_configuration)
    class Adapter(FakeImageAdapter):
        def submit(self,packet,authorization,*,idempotency_key):
            self._received(packet,authorization)
            if case!='persistence_failure':
                with image_factory.begin() as session:
                    design=session.get(DesignSession,design_id);operation=session.get(DesignOperation,operation_id)
                    if case=='missing_after_send':session.delete(operation)
                    else:
                        operation.payload={**operation.payload,'lease_expires_at':(utcnow()-timedelta(seconds=1)).isoformat()}
                if case!='missing_after_send':assert worker.reconcile_image_operations(session_factory=image_factory)==1
            if case=='late_not_sent':raise ProviderFailure('not_sent',may_have_sent=False)
            return {'status':'completed','image_bytes':pixels(),'actual_usd':'0.1','provider_request_id':'late_receipt'}
    adapter=Adapter('openai',image_configuration,image_factory);calls=[]
    @contextmanager
    def faulty_factory():
        calls.append(1)
        if case=='persistence_failure' and len(calls)>1:raise SQLAlchemyError('private')
        with image_factory() as session:yield session
    monkeypatch.setattr(worker.time,'sleep',lambda _:None)
    result=worker.execute_image_operation(operation_id,session_factory=faulty_factory,provider_registry={'openai':adapter},artifact_directory=tmp_path)
    assert result['status']==('persistence_unconfirmed' if case in {'persistence_failure', 'missing_after_send'} else 'late_usage_reconciled')
    assert adapter.calls==1
    if case=='missing_after_send':
        with image_factory() as session:
            ledger=session.get(BudgetOperation,operation_id)
            assert ledger.status=='reserved' and ledger.settled_usd is None
            assert session.get(DesignOperation,operation_id) is None
    if case=='late_not_sent':
        with image_factory() as session:
            assert Decimal(session.get(BudgetOperation,operation_id).settled_usd)==0


def test_image_task_wrapper_and_broker_contract_are_explicit(monkeypatch):
    monkeypatch.setattr(worker,'execute_image_operation',lambda identity:{'identity':identity})
    assert worker.execute_image_task('image_owned')=={'identity':'image_owned'}
    assert worker._broker_database('rediss://127.0.0.1/2')==2
    assert worker._broker_database('redis://127.0.0.1')==0
    with pytest.raises(ValueError):worker._broker_database('https://example.test/')


@pytest.mark.parametrize('case',['active_image','generation_busy','underfunded_pool','recovery_marker'])
def test_image_capacity_pool_and_recovery_boundaries(image_factory,image_configuration,tmp_path,monkeypatch,case):
    design_id,operation_id=seed_image(image_factory,image_configuration)
    with image_factory.begin() as session:
        design=session.get(DesignSession,design_id);operation=session.get(DesignOperation,operation_id)
        if case in {'active_image','generation_busy'}:
            session.add(DesignOperation(id='other_image',session_id=design_id,provider='google',payload={'kind':'generation','status':'authorized','lease_expires_at':(utcnow()+timedelta(seconds=60)).isoformat()}));session.flush()
            if case=='active_image':operation.payload={**operation.payload,'kind':'vision'}
            assert worker._capacity(session,design,operation)==(case=='active_image')
        elif case=='underfunded_pool':
            operation.provider='google';operation.payload={**operation.payload,'kind':'vision','bound_usd':image_configuration['vision_prices']['google']}
            session.add(BudgetOperation(id=f'visualfinish_{design_id}',run_id=design_id,owner_id=design.owner_id,kind='visual_finish_hold',status='held',reserved_usd='0',metadata_json={'target_tag':operation.payload['review_pool_tag'],'roles_remaining':['google']}));session.flush()
            with pytest.raises(VisualLimit,match='underfunded'):
                worker._reserve_operation(session,design,operation,VisualState(**design.state))
            assert session.get(BudgetOperation,operation_id) is None
        else:
            import round_robin.operations.durability as durability
            monkeypatch.setattr(durability,'marker_pending',lambda *a,**kw:True)
            with pytest.raises(VisualLimit,match='recovery'):
                worker._authority(design,operation,(session.get(Meeting,design.meeting_id),lock_policies(session,design.owner_id)[0]),artifact_directory=tmp_path,session=session)


@pytest.mark.parametrize('case',['missing_version','foreign_version','generator_critic','prior_missing','prior_valid','prior_overflow','metadata_overflow','edit_pixels'])
def test_image_packet_immutable_versions_and_bounded_prior_evidence(image_factory,image_configuration,tmp_path,case):
    from round_robin.visual.reviews import PixelView, issue_review_receipt
    from round_robin.visual.schemas import Critique, Finding, ImageCapabilities
    from round_robin.visual.service import commit_review
    design_id,operation_id=seed_image(image_factory,image_configuration)
    adapter=FakeImageAdapter('openai',image_configuration,image_factory)
    assert worker.execute_image_operation(operation_id,session_factory=image_factory,provider_registry={'openai':adapter},artifact_directory=tmp_path)['status']=='persisted'
    with image_factory.begin() as session:
        design=session.get(DesignSession,design_id);version=session.scalar(select(DesignVersion))
        review=session.scalar(select(DesignOperation).where(DesignOperation.provider=='google'))
        provider=FakeImageAdapter('google',image_configuration,image_factory)
        expected=None
        if case=='missing_version':review.payload={**review.payload,'target_version_id':'missing'};expected='immutable parent'
        if case=='foreign_version':version.session_id='foreign';expected='immutable parent'
        if case=='generator_critic':review.provider='openai';expected='generator'
        if case=='prior_missing':version.parent_version_id='missing';expected='prior design'
        if case in {'prior_valid','prior_overflow'}:
            parent=DesignVersion(**{column.name:getattr(version,column.name) for column in DesignVersion.__table__.columns if column.name not in {'id','version','parent_version_id'}} ,id='parent_version',version=0,parent_version_id=None)
            parent.generator_provider=None
            session.add(parent);session.flush();version.parent_version_id=parent.id
            finding = {'severity':'material','category':'observed_defect','description':'The title is cropped at the top edge.'}
            expected_findings = []
            batches = (('anthropic', 20), ('google', 20), ('openai', 1)) if case=='prior_overflow' else (('anthropic', 1),)
            for provider_name, count in batches:
                caps = ImageCapabilities(provider=provider_name, endpoint_version='synthetic', understands_images=True, supported_media=['image/png'])
                delivery = issue_review_receipt(caps, parent.id, parent.canonical_hash, [PixelView(pixels(), parent.canonical_hash)])
                findings = [Finding(id=f'prior_{provider_name}_{n}', **finding) for n in range(count)]
                critique = Critique(provider=provider_name, version_id=parent.id, canonical_hash=parent.canonical_hash, receipt_id=delivery.id, findings=findings)
                row = commit_review(session, design, parent, critique, delivery, expected_fence=design.execution_fence)
                expected_findings.extend(row.critique['findings'])
            session.flush()
            assert len(expected_findings) == (41 if case=='prior_overflow' else 1)
            if case=='prior_overflow':expected='Prior findings exceed'
        if case=='metadata_overflow':
            review.payload={**review.payload,'kind':'generation','edit_intent':['x'*140000]};expected='metadata exceeds'
        if case=='edit_pixels':review.payload={**review.payload,'kind':'edit','parent_version_id':version.id,'edit_intent':'Improve the title spacing.'}
        if expected:
            with pytest.raises(VisualLimit,match=expected):worker._packet(session,design,review,provider,tmp_path)
        else:
            packet=worker._packet(session,design,review,provider,tmp_path)
            assert packet['images'][0]==pixels()
            if case=='prior_valid':
                assert packet['shared_prior_context']['parent_version_id']=='parent_version'
                assert packet['shared_prior_context']['findings']==expected_findings
                assert len(packet['images'])==2


@pytest.mark.parametrize('case',['future_due','recent_publication','sender_error','changed_before_lock'])
def test_image_dispatch_replays_only_due_owned_intents(image_factory,image_configuration,monkeypatch,case):
    design_id,operation_id=seed_image(image_factory,image_configuration)
    with image_factory.begin() as session:
        operation=session.get(DesignOperation,operation_id)
        if case=='future_due':operation.payload={**operation.payload,'due_at':(utcnow()+timedelta(seconds=60)).isoformat()}
        if case=='recent_publication':operation.payload={**operation.payload,'published_at':utcnow().isoformat(),'dispatch_status':'published'}
    if case=='changed_before_lock':monkeypatch.setattr(worker,'_locked_design',lambda *a:(None,None,None))
    sent=[]
    def sender(identity):
        sent.append(identity)
        if case=='sender_error':raise RuntimeError('broker connection lost')
    assert worker.dispatch_pending_image_operations(session_factory=image_factory,sender=sender)==0
    assert sent==([operation_id] if case=='sender_error' else [])
    with image_factory() as session:
        assert session.get(DesignOperation,operation_id).payload['status']=='queued'
        assert session.get(BudgetOperation,operation_id) is None


@pytest.mark.parametrize('case',['fresh_lease','changed_before_lock','no_storage','missing_storage'])
def test_image_reconciler_respects_live_lease_and_missing_optional_storage(image_factory,image_configuration,tmp_path,monkeypatch,case):
    design_id,operation_id=seed_image(image_factory,image_configuration)
    class Adapter(FakeImageAdapter):
        def submit(self,packet,authorization,*,idempotency_key):
            self._received(packet,authorization)
            raise KeyboardInterrupt()
    with pytest.raises(KeyboardInterrupt):worker.execute_image_operation(operation_id,session_factory=image_factory,provider_registry={'openai':Adapter('openai',image_configuration,image_factory)},artifact_directory=tmp_path)
    with image_factory.begin() as session:
        operation=session.get(DesignOperation,operation_id)
        if case!='fresh_lease':operation.payload={**operation.payload,'lease_expires_at':(utcnow()-timedelta(seconds=1)).isoformat()}
        if case=='no_storage':operation.payload={**operation.payload,'storage_reservation_id':None}
        if case=='missing_storage':session.delete(session.get(DesignOperation,operation.payload['storage_reservation_id']))
    if case=='changed_before_lock':monkeypatch.setattr(worker,'_locked_design',lambda *a:(None,None,None))
    changed=worker.reconcile_image_operations(session_factory=image_factory)
    assert changed==(0 if case in {'fresh_lease','changed_before_lock'} else 1)
    with image_factory() as session:
        ledger=session.get(BudgetOperation,operation_id)
        assert ledger.status==('reserved' if changed==0 else 'unknown') and Decimal(ledger.reserved_usd)>0


def test_image_reconciler_releases_only_unused_closed_session_pool(image_factory,image_configuration):
    design_id,_=seed_image(image_factory,image_configuration)
    with image_factory.begin() as session:
        design=session.get(DesignSession,design_id);design.status='cancelled'
        session.add(BudgetOperation(id=f'visualfinish_{design_id}',run_id=design_id,owner_id=design.owner_id,kind='visual_finish_hold',status='held',reserved_usd='0.2',metadata_json={}))
    assert worker.reconcile_image_operations(session_factory=image_factory)==0
    with image_factory() as session:
        pool=session.get(BudgetOperation,f'visualfinish_{design_id}')
        assert pool.status=='released' and Decimal(pool.reserved_usd)==0


@pytest.mark.parametrize('case',['filesystem_error','invalid_state','late_without_state'])
def test_image_post_send_storage_failures_preserve_authorization(image_factory,image_configuration,tmp_path,monkeypatch,case):
    design_id,operation_id=seed_image(image_factory,image_configuration)
    class Adapter(FakeImageAdapter):
        def submit(self,packet,authorization,*,idempotency_key):
            self._received(packet,authorization)
            if case=='late_without_state':
                with image_factory.begin() as session:
                    operation=session.get(DesignOperation,operation_id);operation.payload={**operation.payload,'status':'unknown'}
                    design=session.get(DesignSession,design_id);design.state={**design.state,'operations':{},'request_count':0,'submission_count':0,'step_calls':{}}
            return {'status':'completed','image_bytes':pixels(),'actual_usd':'0.03'}
    if case=='filesystem_error':
        def failed(*a,**kw):raise OSError('private path')
        monkeypatch.setattr(worker,'commit_version',failed)
        import round_robin.operations.durability as durability
        markers=[];monkeypatch.setattr(durability,'mark_failure',lambda *a,**kw:markers.append(a[1]))
        monkeypatch.setattr(worker.time,'sleep',lambda _:None)
    if case=='invalid_state':
        def invalid(*a,**kw):raise ValueError('invalid ledger state')
        monkeypatch.setattr(worker,'reconcile_operation',invalid)
    adapter=Adapter('openai',image_configuration,image_factory)
    result=worker.execute_image_operation(operation_id,session_factory=image_factory,provider_registry={'openai':adapter},artifact_directory=tmp_path)
    assert result['status']=={'filesystem_error':'persistence_unconfirmed','invalid_state':'invalid_result_not_published','late_without_state':'late_usage_reconciled'}[case]
    assert adapter.calls==1
    if case=='filesystem_error':assert markers==['artifact_storage_failed']*3
    with image_factory() as session:
        assert session.get(BudgetOperation,operation_id).status==('settled' if case=='late_without_state' else 'reserved')
        assert session.scalar(select(DesignVersion)) is None
