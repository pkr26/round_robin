"""Fresh independent offline probes of session visual/evaluation additions."""
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
import hashlib
import json

import httpx
from pytest import MonkeyPatch
from backend.tests.test_image_provider import authorization, generation_packet, image_configuration, pixels
from backend.tests.test_study_runner import funded_study, mock_registry
from backend.tests.test_image_worker import image_factory
from backend.tests.test_exhaustive_visual_service import saved_version
from experiments.journal import Journal
from experiments.study_runner import FundedStudyRunner
from round_robin.database.models import BudgetOperation
from round_robin.database.visual_models import DesignOperation, DesignSession, DesignVersion
from round_robin.providers.base import ProviderOutput, validate_output
from round_robin.providers.images import HTTPImageProvider, ImageFundingLocked
from round_robin.visual.assets import validate_raster
from round_robin.visual.feedback import build_edit_bundle
from round_robin.visual.reviews import PixelView, issue_review_receipt
from round_robin.visual.schemas import Critique, Finding, ImageCapabilities, RegressionCheck
from round_robin.visual import service
from round_robin.worker.image_tasks import execute_image_operation


def lost_claim_closure(directory):
    _, (approval, manifest, configuration) = funded_study.__wrapped__(directory)
    registry, sent = mock_registry(configuration)
    marker = 'UNIQUE_PRIOR_ASSUMPTION_MEANING'
    for adapter in registry.values():
        original = adapter.generate
        def generate(request, original=original):
            result = original(request)
            body = result.output.model_dump(mode='json')
            if request.stage == 'proposal':
                body['claims'] = [{'alias':'new_claim_1','proposition':marker,'kind':'assumption',
                    'evidence_status':'not_checked','reference_ids':[]}]
            if request.stage in {'synthesis','audit'}:
                body['reference_ids'] = [request.claims[0]['id']]
            parsed = ProviderOutput.model_validate(body)
            validate_output(parsed, request)
            return replace(result, output=parsed, raw_text=parsed.model_dump_json())
        adapter.generate = generate
    with Journal(approval.journal_directory, mode='explicit_live_study') as journal:
        runner = FundedStudyRunner(journal, approval, manifest, configuration, provider_registry=registry, sleeper=lambda _:None)
        trial = next(row for row in runner.plan if row.workflow=='independent_review' and row.primary)
        result = runner.execute_trial(trial)
    public = json.loads(result['public_text'])
    prior = result['stage_records'][0]['output']['claims'][0]
    assert prior['id'] in result['public_text'] and marker not in result['public_text']
    assert result['status']=='completed'
    return {'status':result['status'],'request_count':result['request_count'],
        'cited_prior_claim_id':prior['id'],'private_claim_proposition':prior['proposition'],
        'public_keys':sorted(public),'claim_meaning_in_public_output':marker in result['public_text'],
        'anonymous_sources_present':bool(public.get('anonymous_contribution_sources')),
        'public_synthesis_reference_ids':public['recommendation']['reference_ids'],
        'qualification':'Saved private stage records retain the claim. The missing closure affects the blinded reviewer artifact, not raw study storage.'}


def oversized_feedback(directory):
    with MonkeyPatch.context() as patch:
        config=image_configuration.__wrapped__(directory,patch)
        packet=generation_packet('edit')
        intent='Fix the selected title issue and preserve the frozen requirements.'
        packet['edit_intent']=intent
        findings=[Finding(id=f'finding_{index}',description=f'Finding {index}: '+ 'x'*470,
            category='observed_defect',severity='critical').model_dump(mode='json') for index in range(40)]
        bundle,bundle_hash=build_edit_bundle(brief=packet['brief'],parent_version_id='version_parent',
            parent_hash=hashlib.sha256(packet['images'][0]).hexdigest(),findings=findings,
            dispositions={findings[0]['id']:'address'},intent=intent)
        packet.update(edit_bundle=bundle,edit_bundle_hash=bundle_hash)
        sends=[]
        adapter=HTTPImageProvider('openai',config,transport=httpx.MockTransport(lambda request:sends.append(request)),api_key='synthetic')
        adapter.preflight(packet,authorization('edit'))
        try:
            adapter.submit(packet,authorization('edit'))
        except ImageFundingLocked as error:
            message=str(error)
        else:
            raise AssertionError('Expected local prompt limit failure')
        assert 'prompt exceeds' in message and not sends
        metadata=json.dumps({key:value for key,value in packet.items() if key!='images'},ensure_ascii=False)
        return {'valid_prior_findings':len(findings),'metadata_utf8_bytes':len(metadata.encode()),
            'worker_metadata_limit':128*1024,'adapter_preflight_accepted':True,
            'submit_rejected_locally':message,'http_requests':len(sends),
            'qualification':'Worker calls this preflight before authorization, but submit performs the later16,000-character prompt check after authorization.'}


def ancestry_overflow(directory):
    directory.mkdir()
    with MonkeyPatch.context() as patch:
        config=image_configuration.__wrapped__(directory,patch)
        fixture=image_factory.__wrapped__(directory)
        factory=next(fixture)
        try:
            factory,design_id,parent_id,root=saved_version.__wrapped__(factory,config,directory)
            with factory.begin() as session:
                design=session.get(DesignSession,design_id)
                parent=session.get(DesignVersion,parent_id)
                raw=service.owned_asset_bytes(parent,design,root)
                for provider in ('anthropic','google'):
                    caps=ImageCapabilities(provider=provider,endpoint_version='synthetic',understands_images=True,supported_media=['image/png'])
                    receipt=issue_review_receipt(caps,parent.id,parent.canonical_hash,[PixelView(raw,parent.canonical_hash)])
                    findings=[Finding(id=f'finding_{index}',description=f'Parent fault {index}',severity='critical',category='observed_defect') for index in range(20)]
                    service.commit_review(session,design,parent,Critique(provider=provider,version_id=parent.id,canonical_hash=parent.canonical_hash,receipt_id=receipt.id,findings=findings),receipt,expected_fence=0)
                session.flush()
                child=service.commit_version(session,design,validate_raster(raw),root/'visual',expected_fence=0,parent_version_id=parent.id,source_kind='native_diagram')
                targets=service.version_regression_targets(session,design,child)
                for provider in ('anthropic','google'):
                    caps=ImageCapabilities(provider=provider,endpoint_version='synthetic',understands_images=True,supported_media=['image/png'])
                    receipt=issue_review_receipt(caps,child.id,child.canonical_hash,[PixelView(raw,child.canonical_hash),PixelView(raw,parent.canonical_hash,f'comparison_parent_version_{parent.id}')])
                    findings=[Finding(id=f'finding_new_{index}',description=f'New child fault {index}',severity='critical',category='observed_defect') for index in range(20)]
                    checks=[RegressionCheck(target_id=row['id'],result='persistent',reason='Original fault persists') for row in targets]
                    service.commit_review(session,design,child,Critique(provider=provider,version_id=child.id,canonical_hash=child.canonical_hash,receipt_id=receipt.id,findings=findings,regression_checks=checks),receipt,expected_fence=0)
                session.flush()
                view=service.version_view(session,child)
                readable_view=service.design_view(session,design)
                try:
                    service.version_feedback_findings(session,design,child)
                except Exception as error:
                    raised={'type':type(error).__name__,'message':str(error)}
                else:
                    raise AssertionError('Expected bounded inherited ledger overflow')
                assert view['coverage']['complete'] and view['regression_review']['complete']
                return {'parent_targets':len(targets),'new_findings':40,
                    'coverage_complete':view['coverage']['complete'],
                    'regression_complete':view['regression_review']['complete'],
                    'existing_session_view_readable':readable_view['id']==design.id,
                    'unresolved_blocker_count':len(view['regression_review']['unresolved_blockers']),
                    'next_edit_feedback_error':raised,
                    'qualification':'No prior concern is silently discarded. Valid per-stage data instead makes the next edit path unavailable; API improve_design calls the raising helper without a VisualLimit handler.'}
        finally:
            fixture.close()


def worker_local_rejection(directory):
    directory.mkdir()
    with MonkeyPatch.context() as patch:
        config=image_configuration.__wrapped__(directory,patch)
        fixture=image_factory.__wrapped__(directory)
        factory=next(fixture)
        try:
            factory,design_id,parent_id,root=saved_version.__wrapped__(factory,config,directory)
            with factory.begin() as session:
                design=session.get(DesignSession,design_id)
                parent=session.get(DesignVersion,parent_id)
                raw=service.owned_asset_bytes(parent,design,root)
                session.get(DesignOperation,'image_operation_one').payload={'kind':'generation','status':'completed'}
                for provider in ('anthropic','google'):
                    caps=ImageCapabilities(provider=provider,endpoint_version='synthetic',understands_images=True,supported_media=['image/png'])
                    receipt=issue_review_receipt(caps,parent.id,parent.canonical_hash,[PixelView(raw,parent.canonical_hash)])
                    findings=[Finding(id=f'finding_{index}',description=f'Fault {index}: '+'x'*470,severity='critical',category='observed_defect') for index in range(20)]
                    service.commit_review(session,design,parent,Critique(provider=provider,version_id=parent.id,canonical_hash=parent.canonical_hash,receipt_id=receipt.id,findings=findings),receipt,expected_fence=0)
                session.flush()
                findings=service.version_feedback_findings(session,design,parent)
                intent='Correct the selected title issue.'
                dispositions={findings[0]['id']:'address'}
                bundle,bundle_hash=build_edit_bundle(brief=design.frozen_brief,parent_version_id=parent.id,parent_hash=parent.canonical_hash,findings=findings,dispositions=dispositions,intent=intent)
                session.add(DesignOperation(id='audit_long_edit',session_id=design.id,provider='openai',payload={
                    'kind':'edit','step_id':'edit:version_2','status':'queued','dispatch_status':'pending',
                    'target_version_id':parent.id,'parent_version_id':parent.id,'input_hash':parent.canonical_hash,
                    'owner_id':design.owner_id,'session_id':design.id,'fence':0,'epoch':1,
                    'bound_usd':config['edit_price_usd'],'edit_intent':intent,'finding_dispositions':dispositions,
                    'initial_admission_expires_at':design.preflight['initial_admission_expires_at'],
                    'review_pool_tag':'edit:version_2','edit_bundle':bundle,'edit_bundle_hash':bundle_hash}))
            sends=[]
            adapter=HTTPImageProvider('openai',config,transport=httpx.MockTransport(lambda request:sends.append(request)),api_key='synthetic')
            outcome=execute_image_operation('audit_long_edit',session_factory=factory,provider_registry={'openai':adapter},artifact_directory=root)
            with factory() as session:
                ledger=session.get(BudgetOperation,'audit_long_edit')
                design=session.get(DesignSession,design_id)
                payload=session.get(DesignOperation,'audit_long_edit').payload
                result={'worker_result':outcome,'http_requests':len(sends),'operation_status':payload['status'],
                    'error_code':payload.get('error_code'),'ledger_status':ledger.status,
                    'ledger_reserved_usd':ledger.reserved_usd,'ledger_settled_usd':ledger.settled_usd,
                    'request_count':design.state['request_count'],'submission_count':design.state['submission_count'],
                    'step_calls':design.state['step_calls'],
                    'qualification':'No request was sent. Unknown is conservative exposure, not an actual charge.'}
                assert not sends and ledger.status=='unknown' and payload['error_code']=='image_operation_unknown'
                return result
        finally:
            fixture.close()


if __name__=='__main__':
    with TemporaryDirectory(prefix='session-eval-audit-') as temporary:
        root=Path(temporary).resolve()
        result={}
        for key,probe in [('claim_reference_closure',lost_claim_closure),('oversized_feedback',oversized_feedback)]:
            directory=root/key;directory.mkdir()
            result[key]=probe(directory)
        result['visual_ancestry_overflow']=ancestry_overflow(root/'ancestry')
        result['worker_local_rejection']=worker_local_rejection(root/'worker_rejection')
    Path(__file__).with_name('probe-results.json').write_text(json.dumps(result,indent=2)+'\n')
    print(json.dumps(result,indent=2))
