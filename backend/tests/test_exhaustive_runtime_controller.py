"""Controller invariant attacks from frozen inputs through truthful finalization."""
from copy import deepcopy
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
import hashlib
import json
from types import SimpleNamespace

import pytest
from sqlalchemy import select,delete

from backend.tests.test_orchestration import factory, seed, drain
from backend.tests.test_provider_configuration import synthetic_configuration
from round_robin.configuration import Settings
from round_robin.api.preflight import run_configuration
from round_robin.database.models import BudgetOperation,EvidenceRecord,Meeting,Outbox,ReportVersion,Run,Step,Upload,utcnow
from round_robin.database.services import lock_policies
from round_robin.fixtures import sample_brief
from round_robin.orchestration.admission import lock_work_run
from round_robin.orchestration.budget import BudgetDenied
from round_robin.orchestration import controller,reporting
from round_robin.orchestration.configuration import verify_frozen_configuration
from round_robin.providers.simulated import SimulatedProvider
from round_robin.schemas.briefs import DraftBrief
from round_robin.worker.tasks import execute_step


def test_explicit_budget_deadline_are_never_dropped_or_confused_with_user_aliases():
    brief=sample_brief().model_dump(mode='json')
    manual={'id':'requirement_server_budget','description':'A separate user condition','origin':'user_supplied','metric':'manual','operator':'manual','threshold':None}
    brief.update(hard_requirements=[manual],budget='1500',deadline='2027-02-01')
    resolved,added=controller.resolved_brief(brief)
    assert added==['requirement_server_budget_1','requirement_server_deadline']
    again,added2=controller.resolved_brief(resolved)
    assert again==resolved and added2==[]
    brief['hard_requirements']=[{**manual,'id':f'requirement_{n}'} for n in range(20)]
    with pytest.raises(BudgetDenied,match='hard_requirement_limit'):
        controller.resolved_brief(brief)


@pytest.mark.parametrize('case,code',[('structured','structured_quote'),('manifest','input_manifest_changed'),('missing_upload','input_scope_invalid'),('foreign_upload','input_scope_invalid'),('changed_hash','input_hash_invalid'),('too_many','selected_passages')])
def test_frozen_selected_evidence_fails_closed_before_partial_ingestion(factory,case,code):
    run_id=seed(factory)
    with factory.begin() as session:
        run=session.get(Run,run_id);brief=deepcopy(run.input_snapshot)
        if case=='structured':
            for option in brief['options']:
                option['label']='x'*500
                for component in option['cost_components']:component['source_ref']='evidence_same'
        if case=='manifest':run.configuration={**run.configuration,'input_manifest':[{'id':'upload_unselected'}]}
        if case in {'missing_upload','foreign_upload','changed_hash','too_many'}:
            brief['selected_attachment_ids']=['upload_selected']
            text='x'*50000 if case=='too_many' else 'Selected exact supplier quote'
            digest=hashlib.sha256(text.encode()).hexdigest()
            run.configuration={**run.configuration,'input_manifest':{'items':[{'id':'upload_selected','sha256':digest}]}}
            if case!='missing_upload':
                session.add(Upload(id='upload_selected',meeting_id=run.meeting_id,owner_id='foreign' if case=='foreign_upload' else run.owner_id,filename='quote.txt',status='ready',canonical_text='altered' if case=='changed_hash' else text,sha256=digest,byte_size=len(text),media_type='text/plain',recipients=['openai','anthropic','google']))
                session.flush()
        run.input_snapshot=brief
        with pytest.raises(BudgetDenied,match=code):controller.prepare_evidence(session,run)
        assert session.scalar(select(EvidenceRecord)) is None
        assert not run.configuration.get('evidence_prepared')


def test_prepared_evidence_can_resume_without_overwriting_existing_version(factory):
    run_id=seed(factory)
    with factory.begin() as session:
        run=session.get(Run,run_id)
        controller.prepare_evidence(session,run)
        rows=list(session.scalars(select(EvidenceRecord)));snapshot={row.id:deepcopy(row.content) for row in rows}
        run.configuration={**run.configuration,'evidence_prepared':False}
        controller.prepare_evidence(session,run)
        assert {row.id:row.content for row in session.scalars(select(EvidenceRecord))}==snapshot


def test_step_repair_restores_only_lost_pending_intent_and_research_barrier(factory,monkeypatch):
    run_id=seed(factory)
    with factory.begin() as session:
        run=lock_work_run(session,run_id);controller.advance_run(session,run)
        step=session.scalar(select(Step));session.execute(delete(Outbox).where(Outbox.step_id==step.id))
        assert controller._ensure_steps(session,run)==[step]
        assert len(list(session.scalars(select(Outbox).where(Outbox.step_id==step.id))))==1
        run.stage='proposals'
        monkeypatch.setattr('round_robin.research.service.schedule_research',lambda *a:True)
        assert controller._ensure_steps(session,run)==[]
        run.input_snapshot={**run.input_snapshot,'horizon_months':None}
        with pytest.raises(BudgetDenied,match='confirmed_brief_incomplete'):controller._ensure_steps(session,run)


@pytest.mark.parametrize('case',['missing','same_provider','older_iteration'])
def test_auditor_requires_current_synthesis_and_an_independent_allowed_provider(factory,case):
    run_id=seed(factory)
    with factory.begin() as session:
        run=lock_work_run(session,run_id);run.stage='audit'
        if case!='missing':
            session.add(Step(id='prior_synth',run_id=run_id,stage='synthesis',role_slot_id='synthesis',provider='openai',status='succeeded',stage_iteration=0))
        if case=='same_provider':run.configuration={**run.configuration,'fallbacks':{'audit':['openai']}}
        if case=='older_iteration':run.configuration={**run.configuration,'stage_iteration':1}
        with pytest.raises(BudgetDenied):controller._ensure_steps(session,run)
        assert session.scalar(select(Step.id).where(Step.stage=='audit')) is None


@pytest.mark.parametrize('case',['terminal','closed_barrier','incomplete_proposals','unsupported','waiting_quota','failed_proposals'])
def test_controller_barriers_never_reopen_successes_or_invent_missing_prerequisites(factory,case):
    run_id=seed(factory)
    if case=='waiting_quota':
        for n in range(3):
            other=seed(factory,suffix=f'waiting{n}')
            with factory.begin() as session:session.get(Run,other).status='awaiting_input'
    with factory.begin() as session:
        run=lock_work_run(session,run_id);controller.advance_run(session,run)
        step=session.scalar(select(Step).where(Step.run_id==run_id))
        if case=='terminal':
            run.status='awaiting_input';sequence=run.event_sequence;controller.advance_run(session,run)
            assert run.event_sequence==sequence;return
        if case=='closed_barrier':
            step.status='succeeded';step.result_json={'output':{'questions':[]}}
            run.configuration={**run.configuration,'closed_barriers':['intake:0:0']}
            sequence=run.event_sequence;controller.advance_run(session,run)
            assert run.stage=='intake' and run.event_sequence==sequence;return
        if case=='incomplete_proposals':run.stage='proposals';run.input_snapshot={**run.input_snapshot,'horizon_months':None}
        if case=='unsupported':run.stage=step.stage='unrecognized';step.status='failed'
        if case=='waiting_quota':step.status='succeeded';step.result_json={'output':{'questions':['Confirm the horizon']}}
        if case=='failed_proposals':run.stage='proposals';step.stage='proposal';step.status='failed'
        controller.advance_run(session,run)
        expected={'incomplete_proposals':'confirmed_brief_incomplete','unsupported':'unsupported_stage','waiting_quota':'awaiting_input_quota_exceeded','failed_proposals':'insufficient_independent_proposals'}[case]
        assert run.stop_reason==expected and run.status=='failed'


def test_finalization_does_not_resurrect_deleted_or_already_published_runs(factory,monkeypatch):
    run_id=seed(factory);drain(factory)
    with factory.begin() as session:
        run=lock_work_run(session,run_id);version=run.report_version
        controller.finalize_run(session,run)
        assert run.report_version==version
        session.get(Meeting,run.meeting_id).deleted_at=utcnow()
        controller.finalize_run(session,run,status='cancelled')
        assert run.report_version==version
    other=seed(factory,suffix='second')
    with factory.begin() as session:
        run=lock_work_run(session,other)
        def freeze(*a):raise BudgetDenied('other_invariant_failure')
        monkeypatch.setattr(controller,'freeze_active',freeze)
        with pytest.raises(BudgetDenied,match='other_invariant'):controller.finalize_run(session,run)


def test_finalization_foreign_parent_cannot_link_private_report_and_preserves_retry_hold(factory):
    parent=seed(factory);drain(factory)
    child=seed(factory,suffix='child')
    with factory.begin() as session:
        run=lock_work_run(session,child);run.parent_run_id=parent
        controller.advance_run(session,run)
        step=session.scalar(select(Step).where(Step.run_id==child))
        step.stage='synthesis';step.status='failed';step.error_code='timeout';step.calls=1;step.infrastructure_claims=1
        controller.finalize_run(session,run)
        assert session.get(BudgetOperation,f'finish_{child}').status=='held'
        report=session.scalar(select(ReportVersion).where(ReportVersion.run_id==child)).content
        assert report['parent_report_id'] is None


def test_complete_cost_rows_are_deterministic_under_every_comparator():
    body=sample_brief().model_dump(mode='json')
    for option in body['options']:
        present={component['kind'] for component in option['cost_components']}
        for kind in {'upfront','installation','recurring','maintenance','tax_delivery'}-present:
            option['cost_components'].append({'id':f'component_{kind}_{option["id"]}','label':f'{kind} not applicable','kind':kind,'amount':None,'currency':'USD','unit':'item','frequency':'once','status':'not_applicable','source_ref':'evidence_owned'})
    # Factory validation still determines the exact cost contract; comparator
    # thresholds below and above the observed totals should change only results.
    for operator,threshold in [('lte','0'),('gte','0'),('eq','0')]:
        body['hard_requirements']=[{'id':'requirement_cost','description':'Bounded exact comparison','origin':'user_supplied','metric':'total_cost','operator':operator,'threshold':threshold}]
        brief=DraftBrief.model_validate(body)
        comparisons,calculations=reporting.comparison_data(brief)
        assert all(item['total'] is not None for item in comparisons)
        assert comparisons[0]['constraint_results'][0]['result']==('pass' if operator=='gte' else 'fail')
    simulated=SimulatedProvider('openai')
    from backend.tests.test_providers import packet
    assert simulated.generate(replace(packet(),brief=brief.model_dump(mode='json'))).output.recommendation_option_id


@pytest.mark.parametrize('case',['oversized','epoch','missing','invalid_json','visual_failure'])
def test_frozen_configuration_validation_rejects_incompatible_operator_state(tmp_path,case,monkeypatch):
    path=tmp_path/'providers.json';path.write_text(json.dumps(synthetic_configuration()))
    settings=replace(Settings(),provider_config_file=path,paid_access_enabled=True)
    config=run_configuration(settings,'live')
    run=SimpleNamespace(configuration=config,deployment_epoch=settings.deployment_epoch)
    if case=='oversized':path.write_text(' '* (128*1024+1))
    if case=='epoch':settings=replace(settings,deployment_epoch=settings.deployment_epoch+1)
    if case=='missing':path.unlink()
    if case=='invalid_json':path.write_text('{')
    if case=='visual_failure':
        run.configuration={'visual_configuration':True}
        from round_robin.providers.images import ImageFundingLocked
        def fail(*a):raise ImageFundingLocked('funding_missing')
        monkeypatch.setattr('round_robin.providers.images.verify_image_configuration',fail)
    with pytest.raises(BudgetDenied):verify_frozen_configuration(run,operator_settings=settings)


@pytest.mark.parametrize('change',['missing','prompt','schema','packet_limit'])
def test_live_run_never_silently_adopts_changed_application_request_contract(tmp_path,monkeypatch,change):
    from round_robin.providers import http,base
    path=tmp_path/'providers.json';path.write_text(json.dumps(synthetic_configuration()))
    settings=replace(Settings(),provider_config_file=path,paid_access_enabled=True)
    run=SimpleNamespace(configuration=run_configuration(settings,'live'),deployment_epoch=settings.deployment_epoch)
    verify_frozen_configuration(run,operator_settings=settings)
    before=run.configuration['request_contract_hash']
    if change=='missing':run.configuration.pop('request_contract_hash')
    if change=='prompt':monkeypatch.setattr(http,'SYSTEM',http.SYSTEM+' changed instruction')
    if change=='schema':monkeypatch.setattr(http,'_output_schema',lambda:{'type':'object','properties':{'new':{'type':'string'}}})
    if change=='packet_limit':monkeypatch.setattr(http,'MAX_PACKET_BYTES',64)
    with pytest.raises(BudgetDenied,match='frozen_request_contract_changed'):
        verify_frozen_configuration(run,operator_settings=settings)
    fresh=run_configuration(settings,'live')
    if change!='missing':assert fresh['request_contract_hash']!=before
    verify_frozen_configuration(SimpleNamespace(configuration=fresh,deployment_epoch=settings.deployment_epoch),operator_settings=settings)


def test_live_report_uses_checked_costs_and_preserves_matching_model_findings(factory):
    body=sample_brief().model_dump(mode='json')
    for option in body['options']:
        present={component['kind'] for component in option['cost_components']}
        for kind in {'upfront','installation','recurring','maintenance','tax_delivery'}-present:
            option['cost_components'].append({'id':f'component_{kind}_{option["id"]}','label':f'{kind}','kind':kind,'amount':None,'currency':'USD','unit':'item','frequency':'once','status':'not_applicable','source_ref':f'evidence_{kind}_{option["id"]}'})
    run_id=seed(factory,brief=DraftBrief.model_validate(body));drain(factory)
    with factory.begin() as session:
        run=session.get(Run,run_id);steps=list(session.scalars(select(Step).where(Step.run_id==run_id).order_by(Step.created_at,Step.id)))
        run.configuration={**run.configuration,'mode':'live'}
        synth=next(step for step in steps if step.stage=='synthesis')
        audit=next(step for step in steps if step.stage=='audit')
        brief=run.configuration['resolved_brief'];source=next(iter(run.configuration['source_aliases'].values()))
        manual=next(item for item in brief['hard_requirements'] if item['metric']=='manual')
        finding={'option_id':'option_a','requirement_id':manual['id'],'result':'unknown','reason':'The source is explicit but feasibility still needs review.','basis_refs':[source]}
        synth.result_json={**synth.result_json,'output':{**synth.result_json['output'],'constraints':[finding]}}
        audit.result_json={**audit.result_json,'output':{**audit.result_json['output'],'constraints':[finding]}}
        report=reporting.build_report(run,steps,status='completed',stop_reason='protocol_complete',report_id='report_owned',version=2,usage={'simulated':False,'status':'reported','actual_cost':'0','conservative_exposure_usd':'0','unknown_operation_ids':[],'request_count':6},evidence=controller.validated_evidence(session,run),parent_report={'id':'report_parent','recommendation':{'option_id':'option_foreign'},'readiness':'ready_to_choose'},parent_brief=run.input_snapshot)
        option=next(item for item in report['comparison'] if item['option_id']=='option_a')
        assert any(item['method']=='deterministic_calculation' for item in option['constraint_results'])
        assert any(item['method']=='model' and item['result']=='unknown' for item in option['constraint_results'])
        assert any('preferred-option result changed' in change for change in report['change_summary'])
        assert any('Decision readiness changed' in change for change in report['change_summary'])
