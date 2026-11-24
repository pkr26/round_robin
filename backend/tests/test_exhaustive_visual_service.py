"""Fresh exact-version, backup containment and review provenance adversaries."""
from copy import deepcopy
from dataclasses import asdict, replace
from datetime import timedelta
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy import select

from backend.tests.test_image_provider import image_configuration
from backend.tests.test_image_worker import image_factory, seed_image
from backend.tests.test_visual import visual_app, png, caps, critique, receipt, FakeImageProvider, upload_original
from round_robin.database.models import Command, utcnow
from round_robin.database.visual_models import DesignOperation, DesignReview, DesignSession, DesignVersion
from round_robin.providers.images import ImageAuthorization
from round_robin.visual import service
from round_robin.visual.assets import UnsafeRaster, store_raster, validate_raster
from round_robin.visual.iterations import VisualState, VisualLimit
from round_robin.visual.jobs import ImageJob, _same_authorization, request_hash, submit_saved_job
from round_robin.visual.reviews import PixelView, coverage, independent_review_packet, issue_review_receipt, validate_critique
from round_robin.visual.schemas import Critique, ImageCapabilities, VisualBrief
from round_robin.visual.views import DesignSessionView


def test_review_parent_context_binds_exact_parent_pixels_and_prior_findings():
    original,views=receipt()
    parent=PixelView(views[0].image_bytes,views[0].sha256,'comparison_parent_version_version_parent')
    delivery=issue_review_receipt(caps(),'version_one',views[0].sha256,views+[parent])
    brief=VisualBrief(purpose='test',audience='test')
    context={'parent_hash':parent.sha256,'parent_version_id':'version_parent','findings':[{'id':'finding_prior'}],'edit_intent':'preserve text'}
    assert independent_review_packet(brief,delivery,views+[parent],prior_context=context)['shared_prior_context']==context
    for changed in [{**context,'parent_hash':'0'*64},{**context,'parent_version_id':'version_foreign'},{**context,'findings':{}},{**context,'findings':[{'id':'same'}]*2},{**context,'findings':[{'id':str(i)} for i in range(41)]},{**context,'edit_intent':'x'*2001}]:
        with pytest.raises(ValueError):independent_review_packet(brief,delivery,views+[parent],prior_context=changed)
    review=critique(delivery)
    review.findings[0].view_index=3
    with pytest.raises(ValueError):validate_critique(review,delivery)
    review.findings[0].view_index=0;review.findings[0].previous_finding_id='finding_other'
    with pytest.raises(ValueError):validate_critique(review,delivery,prior_finding_ids={'finding_prior'})
    assert not coverage([critique(original)],[],version_id=original.version_id,canonical_hash=original.canonical_hash,generator_provider=None)['complete']


def test_visual_contracts_reject_bad_pixels_duplicate_findings_and_invented_jobs():
    with pytest.raises(ValidationError):VisualBrief(purpose='test',audience='test',width=8192,height=8192)
    delivery,_=receipt();value=critique(delivery).model_dump(mode='json');value['findings']*=2
    with pytest.raises(ValidationError):Critique.model_validate(value)
    with pytest.raises(ValidationError):ImageCapabilities(provider='openai',endpoint_version='test',job_lookup=True)


def test_public_session_selection_and_counters_are_consistent(visual_app):
    _,_,_,design=visual_app
    for changes in [{'selected_version_id':'version_foreign'},{'mode':'improve'}]:
        with pytest.raises(ValidationError):DesignSessionView.model_validate({**design,**changes})


def test_job_submission_rejects_changed_authority_and_missing_remote_identity():
    packet={'pixels':[b'private bytes'],'dict':{'x':True}}
    job=ImageJob('image_job_one','op_one','step_one','openai',request_hash(packet),0,1,'owner_one','session_one','0')
    auth=ImageAuthorization('op_one','owner_one','session_one','0',0,1,'step_one')
    assert not _same_authorization(job,replace(auth,bound_usd='NaN'))
    provider=FakeImageProvider();provider.capabilities=provider.capabilities.model_copy(update={'generates_images':False})
    with pytest.raises(VisualLimit):submit_saved_job(job,packet,provider,auth,persist=lambda _:None)
    provider=FakeImageProvider()
    with pytest.raises(VisualLimit):submit_saved_job(job,{'changed':True},provider,auth,persist=lambda _:None)
    provider.submit=lambda *a,**k:{}
    saved=[]
    with pytest.raises(VisualLimit):submit_saved_job(job,packet,provider,auth,persist=lambda value:saved.append(asdict(value)))
    assert job.status=='unknown' and saved[-1]['unknown_outcome']


@pytest.fixture
def saved_version(image_factory,image_configuration,tmp_path):
    design_id,_=seed_image(image_factory,image_configuration)
    with image_factory.begin() as session:
        design=session.get(DesignSession,design_id)
        version=service.commit_version(session,design,validate_raster(png()),tmp_path/'visual',expected_fence=design.execution_fence)
        identity=version.id
    return image_factory,design_id,identity,tmp_path


@pytest.mark.parametrize('change', ['recipients','terminal','capability','exhausted'])
def test_review_queue_requires_frozen_independent_recipients_and_remaining_role(saved_version,change):
    factory,design_id,version_id,_=saved_version
    with factory.begin() as session:
        design=session.get(DesignSession,design_id);version=session.get(DesignVersion,version_id)
        if change=='recipients':design.recipients=['openai']
        elif change=='terminal':design.status='cancelled'
        elif change=='capability':
            config=deepcopy(design.configuration);config['capabilities']['anthropic']['understands_images']=False;design.configuration=config
        else:
            state=VisualState(**design.state);state.paid_admitted=True
            for index in range(2):state.authorize('op_'+str(index),f'vision:{version.id}:anthropic','vision','0',now=state.admitted_at,expected_fence=0,epoch=1)
            design.state=asdict(state)
        with pytest.raises(VisualLimit):service.queue_version_reviews(session,design,version)


def test_review_queue_deduplicates_saved_or_pending_critic_roles(saved_version):
    factory,design_id,version_id,directory=saved_version
    with factory.begin() as session:
        design=session.get(DesignSession,design_id);version=session.get(DesignVersion,version_id)
        first=service.queue_version_reviews(session,design,version);session.flush()
        assert len(first)==2 and service.queue_version_reviews(session,design,version)==[]
        session.delete(first[0]);session.flush()
        raw = service.owned_asset_bytes(version, design, directory)
        capability = caps(first[0].provider)
        delivery = issue_review_receipt(capability, version.id, version.canonical_hash, [PixelView(raw, version.canonical_hash)])
        review = Critique(provider=first[0].provider, version_id=version.id, canonical_hash=version.canonical_hash, receipt_id=delivery.id)
        service.commit_review(session, design, version, review, delivery, expected_fence=design.execution_fence)
        session.flush();assert service.queue_version_reviews(session,design,version)==[]


@pytest.mark.parametrize('change', ['parent','upload','reservation','source','operation','generation_after_v1'])
def test_new_candidate_must_preserve_original_lineage_and_counted_authority(saved_version,change):
    factory,design_id,version_id,directory=saved_version
    with factory.begin() as session:
        design=session.get(DesignSession,design_id)
        kwargs={'expected_fence':0,'parent_version_id':version_id,'source_kind':'native_diagram'}
        if change=='parent':kwargs['parent_version_id']='version_foreign'
        elif change=='upload':kwargs['source_kind']='upload'
        elif change=='reservation':kwargs['storage_reservation_id']='missing'
        elif change=='source':kwargs['source_kind']='unsupported'
        elif change=='operation':kwargs.update(source_kind='edit',operation_id='missing',generator_provider='openai')
        else:
            state=VisualState(**design.state);state.paid_admitted=True;state.authorize('generation_op','generation','generation','0',now=state.admitted_at,expected_fence=0,epoch=1);design.state=asdict(state)
            kwargs.update(source_kind='generated',operation_id='generation_op',generator_provider='openai')
        with pytest.raises(VisualLimit):service.commit_version(session,design,validate_raster(png()),directory/'visual',**kwargs)
        assert len(list(session.scalars(select(DesignVersion))))==1


def test_review_commit_enforces_fence_scope_prior_ids_and_unique_provider(saved_version):
    factory,design_id,version_id,directory=saved_version
    with factory.begin() as session:
        design=session.get(DesignSession,design_id);version=session.get(DesignVersion,version_id)
        raw=service.owned_asset_bytes(version,design,directory)
        delivery=issue_review_receipt(caps(),'version_foreign',version.canonical_hash,[PixelView(raw,version.canonical_hash)])
        with pytest.raises(VisualLimit):service.commit_review(session,design,version,critique(delivery),delivery,expected_fence=0)
        delivery=replace(delivery,version_id=version.id)
        with pytest.raises(VisualLimit):service.commit_review(session,design,version,critique(delivery),delivery,expected_fence=9)
        service.commit_review(session,design,version,critique(delivery),delivery,expected_fence=0);session.flush()
        with pytest.raises(VisualLimit):service.commit_review(session,design,version,critique(delivery),delivery,expected_fence=0)
        next_version=service.commit_version(session,design,validate_raster(png((50,32))),directory/'visual',expected_fence=0,parent_version_id=version.id,source_kind='native_diagram')
        raw2=service.owned_asset_bytes(next_version,design,directory)
        next_receipt=issue_review_receipt(caps(),next_version.id,next_version.canonical_hash,[PixelView(raw2,next_version.canonical_hash)])
        previous=session.scalar(select(DesignReview))
        next_critique=critique(next_receipt);next_critique.findings[0].previous_finding_id=previous.critique['findings'][0]['id']
        service.commit_review(session,design,next_version,next_critique,next_receipt,expected_fence=0)


def test_native_sources_are_hash_bound_idempotent_and_content_tampering_rejected(saved_version):
    from round_robin.visual.renderers.diagram import Diagram,render_diagram
    factory,design_id,version_id,directory=saved_version
    rendered=render_diagram(Diagram.model_validate({'width':500,'height':200,'nodes':[{'id':'node_one','label':'Test','x':10,'y':10}]}))
    with factory.begin() as session:
        design=session.get(DesignSession,design_id);version=session.get(DesignVersion,version_id)
        service.save_native_sources(session,design,version,rendered,directory/'visual')
        service.save_native_sources(session,design,version,rendered,directory/'visual')
        with pytest.raises(VisualLimit):service.save_native_sources(session,design,version,replace(rendered,svg_hash='0'*64),directory/'visual')
        Path(version.provenance['svg_path']).write_bytes(b'corrupt')
        with pytest.raises(VisualLimit):service.save_native_sources(session,design,version,rendered,directory/'visual')
        source='<p>private</p>';value=SimpleNamespace(source=source,source_hash=hashlib.sha256(source.encode()).hexdigest(),renderer_version='test',configuration_hash='a'*64,checks=[],snapshot=SimpleNamespace(model_dump=lambda **_:{}))
        service.save_interface_source(session,design,version,value,directory/'visual');service.save_interface_source(session,design,version,value,directory/'visual')
        bad=SimpleNamespace(**{**value.__dict__,'source_hash':'0'*64})
        with pytest.raises(VisualLimit):service.save_interface_source(session,design,version,bad,directory/'visual')
        Path(version.provenance['source_path']).write_bytes(b'corrupt')
        with pytest.raises(VisualLimit):service.save_interface_source(session,design,version,value,directory/'visual')


def test_clean_orphans_preserves_exact_saved_content_and_rejects_unexpected_entries(saved_version):
    factory,design_id,version_id,directory=saved_version
    with factory.begin() as session:
        design=session.get(DesignSession,design_id);version=session.get(DesignVersion,version_id)
        namespace=Path(version.asset_path).parent
        orphan=namespace/'orphan.png';orphan.write_bytes(b'orphan private bytes')
        service.remove_unreferenced_assets(session,design,directory)
        assert not orphan.exists() and Path(version.asset_path).exists()
        (namespace/'directory').mkdir()
        with pytest.raises(VisualLimit):service.remove_unreferenced_assets(session,design,directory)


def test_reaper_releases_only_old_unattached_holds_and_resolves_original_uuid(visual_app):
    app,_,_,saved=visual_app;command=str(uuid4());reservation='visualupload_'+command.replace('-','')
    with app.state.session_factory.begin() as session:
        design=session.get(DesignSession,saved['id']);row=service.reserve_asset_storage(session,design,reservation);row.created_at=utcnow()-timedelta(minutes=11)
        session.add(Command(owner_id=design.owner_id,command_id=command,command_type='upload_design',body_hash='a'*64,result={},resource_id=design.id))
        other=service.reserve_asset_storage(session,design,'external_hold');other.created_at=utcnow()-timedelta(minutes=11);other.payload={**other.payload,'external_operation_id':'sent_external'}
        session.flush()
    with app.state.session_factory.begin() as session:
        assert service.reap_asset_reservations(session,app.state.settings.artifact_directory)==1
        assert session.get(Command,('owner_local',command)).result['upload_status']=='expired'
        assert session.get(DesignOperation,'external_hold').payload['status']=='reserved'


def test_restore_rejects_archive_escapes_unsupported_or_missing_assets(tmp_path):
    data=b'private';digest=hashlib.sha256(data).hexdigest();root=tmp_path/'backup';(root/'visual_assets').mkdir(parents=True)
    source=root/'visual_assets'/f'{digest}.png';source.write_bytes(data)
    original={'key':f'visual_assets/{digest}.png','relative_path':f'visual/owner_a/design_a/{digest}.png','sha256':digest,'original_path':'original'}
    for changes in [{'key':'../outside.png'},{'relative_path':'../outside.png'},{'key':'/absolute'},{'relative_path':'other/asset.png'},{'key':'visual_assets/wrong.png'},{'relative_path':'visual/file.exe'}]:
        with pytest.raises(VisualLimit):service.restore_visual_assets(root,tmp_path/'restore',[{**original,**changes}],{})
    with pytest.raises(VisualLimit):service.restore_visual_assets(root,tmp_path/'restore',[original]*2001,{})
    with pytest.raises(VisualLimit):service.restore_visual_assets(root,tmp_path/'restore',[],{'design_versions':[{'asset_path':'missing'}]})
    with pytest.raises(VisualLimit):service.restore_visual_assets(root,tmp_path/'restore',[original],{'design_versions':[{'asset_path':'original','provenance':{'source_path':'missing'}}]})
    tables={'design_versions':[{'asset_path':'original','provenance':{'source_path':'original'}}]}
    service.restore_visual_assets(root,tmp_path/'restore',[original],tables)
    assert tables['design_versions'][0]['asset_path']==tables['design_versions'][0]['provenance']['source_path']


def test_owner_and_deployment_storage_count_other_owners_and_pending_reservations(saved_version,monkeypatch):
    from round_robin.database.models import Meeting,Owner
    factory,design_id,version_id,_=saved_version
    monkeypatch.setattr(service,'OWNER_VISUAL_BYTES',1000);monkeypatch.setattr(service,'DEPLOYMENT_VISUAL_BYTES',2000)
    with factory.begin() as session:
        design=session.get(DesignSession,design_id);version=session.get(DesignVersion,version_id)
        version.provenance={'canonical_bytes':100,'native_bytes':20}
        session.add(Owner(id='owner_other'));session.flush();session.add(Meeting(id='meeting_other',owner_id='owner_other',title='other'));session.flush()
        other=DesignSession(id='design_other',meeting_id='meeting_other',owner_id='owner_other',mode='review_only',status='active',revision=1,execution_fence=0,deployment_epoch=1,frozen_brief=design.frozen_brief,recipients=[],maximum_usd='0',configuration={},state=asdict(VisualState('review_only','0',100)))
        session.add(other);session.flush()
        session.add(DesignVersion(id='version_other',session_id=other.id,version=1,canonical_hash='0'*64,original_hash='0'*64,asset_path='/unread',width=1,height=1,source_kind='upload',provenance={'canonical_bytes':1000}))
        for identity,scope,status,size in [('hold_current',design.id,'reserved',100),('hold_other',other.id,'reserved',200),('hold_released',other.id,'released',999999)]:
            session.add(DesignOperation(id=identity,session_id=scope,provider='local',payload={'kind':'asset_reservation','status':status,'maximum_bytes':size}))
        session.flush()
        service.check_storage_capacity(session,design,580)
        service.check_storage_capacity(session,design,680,exclude_reservation='hold_current')
        with pytest.raises(VisualLimit):service.check_storage_capacity(session,design,581)
        with pytest.raises(VisualLimit):service.check_storage_capacity(session,design,-1)


def test_purge_removes_orphans_and_preserves_unknown_usage_for_active_operations(saved_version):
    factory,design_id,version_id,directory=saved_version
    with factory.begin() as session:
        design=session.get(DesignSession,design_id);version=session.get(DesignVersion,version_id)
        state=VisualState(**design.state);state.paid_admitted=True;state.authorize('op_pending','step_pending','vision','0',now=state.admitted_at,expected_fence=0,epoch=1);design.state=asdict(state)
        namespace=Path(version.asset_path).parent
        orphan=namespace/'orphan';orphan.write_bytes(b'private')
        service.purge_designs(session,design.meeting_id,directory)
        assert design.state['operations']['op_pending']['status']=='unknown'
        assert not orphan.exists() and not Path(version.asset_path).exists()


def test_purge_rejects_linked_or_directory_orphans_without_deleting_foreign_data(saved_version,tmp_path):
    factory,design_id,version_id,directory=saved_version
    foreign=tmp_path/'foreign';foreign.write_bytes(b'private other owner')
    with factory.begin() as session:
        design=session.get(DesignSession,design_id);version=session.get(DesignVersion,version_id)
        orphan=Path(version.asset_path).parent/'orphan';orphan.symlink_to(foreign)
        with pytest.raises(VisualLimit):service.purge_designs(session,design.meeting_id,directory)
    assert foreign.read_bytes()==b'private other owner'


def test_visual_backup_rejects_unsafe_paths_types_and_linked_components(tmp_path):
    root=tmp_path/'artifacts';private=root/'visual'/'owner_a'/'design_a';private.mkdir(parents=True)
    data=b'private';digest=hashlib.sha256(data).hexdigest();path=private/(digest+'.png');path.write_bytes(data)
    row={'asset_path':str(path),'canonical_hash':digest,'provenance':{}}
    tables={'design_versions':[row,deepcopy(row)]}
    items=service.backup_visual_assets(tables,root,tmp_path/'backup')
    assert len(items)==1
    with pytest.raises(VisualLimit):service.backup_visual_assets({'design_versions':[{**row,'asset_path':str(tmp_path/'foreign.png')}]},root,tmp_path/'second')
    unsupported=private/'private.exe';unsupported.write_bytes(data)
    with pytest.raises(VisualLimit):service.backup_visual_assets({'design_versions':[{**row,'asset_path':str(unsupported)}]},root,tmp_path/'second')
    linked=tmp_path/'root_link';linked.symlink_to(root,target_is_directory=True)
    with pytest.raises(VisualLimit):service.backup_visual_assets(tables,linked,tmp_path/'second')
    nested=private/'linked';nested.symlink_to(private,target_is_directory=True)
    with pytest.raises(VisualLimit):service.backup_visual_assets({'design_versions':[{**row,'asset_path':str(nested/path.name)}]},root,tmp_path/'second')
    # A final-component archive symlink is rejected before any restore write.
    backup=tmp_path/'backup';archive=backup/items[0]['key'];archive.unlink();archive.symlink_to(path)
    with pytest.raises(VisualLimit):service.restore_visual_assets(backup,tmp_path/'restored',items,{})


def test_orphan_cleanup_rejects_linked_artifact_ancestor_before_resolving(saved_version,tmp_path):
    factory,design_id,version_id,directory=saved_version
    linked=tmp_path/'linked_artifacts';linked.symlink_to(directory,target_is_directory=True)
    with factory.begin() as session:
        design=session.get(DesignSession,design_id);version=session.get(DesignVersion,version_id)
        orphan=Path(version.asset_path).parent/'retained-orphan';orphan.write_bytes(b'private bytes')
        with pytest.raises(VisualLimit):service.remove_unreferenced_assets(session,design,linked)
        assert orphan.read_bytes()==b'private bytes' and Path(version.asset_path).exists()
