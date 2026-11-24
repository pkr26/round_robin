"""Independent offline session-audit counterexamples; product source unchanged."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from io import BytesIO
import json
from pathlib import Path
from types import SimpleNamespace

from pypdf import PdfReader
from pytest import MonkeyPatch

from backend.tests.test_evidence_regressions import complete_brief
from round_robin.orchestration.reporting import build_report, comparison_as_of
from round_robin.reports.exports import report_markdown, report_pdf
from round_robin.research.discussion import assessment_target, current_assessments, reduce_discussion
from round_robin.schemas.discussion import ScopedAssessment


def fixture(*, expiry=None):
    now=datetime(2026,11,17,19,0,tzinfo=timezone.utc)
    brief=complete_brief(valid_until=expiry).model_dump(mode='json')
    brief['options']=brief['options'][:2]
    brief['hard_requirements']=[{'id':'requirement_feature','description':'Required feature','metric':'manual','operator':'manual','origin':'user_supplied'}]
    for option in brief['options']:
        for component in option['cost_components']:component['source_ref']='evidence_cost'
    run=SimpleNamespace(id='run_probe',owner_id='owner_probe',created_at=now-timedelta(days=20),input_snapshot=brief,event_sequence=1,
        configuration={'mode':'live','resolved_brief':brief,'evidence_prepared':True})
    evidence=[{'id':'evidence_cost','sha256':'a'*64,'verification_status':'user_supplied'}, {'id':'evidence_feature','sha256':'b'*64,'verification_status':'user_supplied'}]
    values=[]
    for option in brief['options']:
        for target,requirement,refs,result in [('option_costs',None,['evidence_cost'],'supported'),('requirement','requirement_feature',['evidence_feature'],'pass')]:
            values.append(ScopedAssessment(id=f'assessment_{len(values)}',target_type=target,option_id=option['id'],requirement_id=requirement,
                target_hash=assessment_target(run,target,option['id'],requirement),reference_ids=refs,
                evidence_hashes={r:next(item['sha256'] for item in evidence if item['id']==r) for r in refs},result=result,
                attestation='The exact supplied inputs were checked while the supplier quotation was valid.',actor_id=run.owner_id,run_id=run.id,
                recorded_at=run.created_at,valid_until=None).model_dump(mode='json'))
    run.configuration['evidence_assessments']=values
    def step(identity,stage,provider):
        output={'summary':'Scoped selected option','recommendation_option_id':brief['options'][0]['id'],'reference_ids':['evidence_feature'],'questions':[],'assumptions':[],
            'constraints':[{'option_id':o['id'],'requirement_id':'requirement_feature','result':'pass','reason':'Synthetic feature assessment','basis_refs':['evidence_feature']} for o in brief['options']],
            'audit_passed':True if stage=='audit' else None,'critical_defects':[],'dissent':[]}
        return SimpleNamespace(id=identity,run_id=run.id,stage=stage,provider=provider,status='succeeded',error_code=None,result_json={'output':output})
    steps=[step('step_proposal_1','proposal','openai'),step('step_proposal_2','proposal','google'),step('step_synthesis','synthesis','openai'),step('step_audit','audit','anthropic')]
    usage={'simulated':False,'status':'reported','actual_cost':'0','conservative_exposure_usd':'0','unknown_operation_ids':[],'request_count':4}
    return now,run,steps,evidence,usage


def expired_reassessment():
    expiry=datetime(2026,11,10,tzinfo=timezone.utc).date()
    now,run,steps,evidence,usage=fixture(expiry=expiry)
    with MonkeyPatch.context() as patch:
        patch.setattr('round_robin.research.discussion.utcnow',lambda:now)
        record=build_report(run,steps,status='completed',stop_reason='scoped_attestation',report_id='report_new_assessment',version=2,usage=usage,evidence=evidence)
    accepted=current_assessments(run,{x['id']:x for x in evidence},now=now)
    result={'reassessment_time':now.isoformat(),'quote_valid_until':expiry.isoformat(),'calculation_as_of':comparison_as_of(run).isoformat(),
        'accepted_cost_attestations':sum(x['target_type']=='option_costs' for x in accepted),'new_report_version':record['version'],'new_report_readiness':record['readiness'],
        'new_report_costs':[x['total'] for x in record['comparison']]}
    assert result['new_report_readiness']=='ready_to_choose' and all(x is not None for x in result['new_report_costs'])
    assert expiry < now.date()
    return result


def export_loss():
    now,run,steps,evidence,usage=fixture()
    opener=SimpleNamespace(id='step_proposal_1',run_id=run.id,provider='openai',stage='proposal')
    _,values,_=reduce_discussion(run,opener,{'critical_defects':['Review the required supplier compatibility.']},{'evidence_feature'})
    run.configuration['discussion_issues']=values
    issue=values[0]['id']
    answer={'objection_responses':[{'objection_id':issue,'disposition':'answered','response':'SYNTHESIS_RESOLUTION_EXPLANATION_SENTINEL','reference_ids':['evidence_feature']}]}
    _,values,_=reduce_discussion(run,steps[2],answer,{'evidence_feature'})
    run.configuration['discussion_issues']=values
    answer['objection_responses'][0].update(disposition='resolved',response='AUDITOR_RESOLUTION_EXPLANATION_SENTINEL')
    _,values,_=reduce_discussion(run,steps[3],answer,{'evidence_feature'})
    run.configuration['discussion_issues']=values
    report=build_report(run,steps,status='completed',stop_reason='protocol_complete',report_id='report_exports',version=1,usage=usage,evidence=evidence)
    md=report_markdown(report,title='Synthetic export audit',sources=[])
    raw_pdf=report_pdf(report,title='Synthetic export audit',sources=[])
    pdf='\n'.join(page.extract_text() for page in PdfReader(BytesIO(raw_pdf)).pages)
    expected=['SYNTHESIS_RESOLUTION_EXPLANATION_SENTINEL','AUDITOR_RESOLUTION_EXPLANATION_SENTINEL']
    result={'saved_issue_status':report['issues'][0]['status'],'saved_event_count':len(report['issues'][0]['events']),
        'json_retains_both_explanations':all(x in json.dumps(report) for x in expected),'markdown_retains_both_explanations':all(x in md for x in expected),
        'pdf_retains_both_explanations':all(x in pdf for x in expected)}
    assert result['json_retains_both_explanations'] and not result['markdown_retains_both_explanations'] and not result['pdf_retains_both_explanations']
    return result


def stale_answer_resolution():
    now,run,steps,evidence,usage=fixture()
    _,values,_=reduce_discussion(run,steps[0],{'critical_defects':['The latest recommendation must contain a compatible delivery condition.']},{'evidence_feature'})
    run.configuration['discussion_issues']=values
    issue=values[0]['id']
    response={'objection_responses':[{'objection_id':issue,'disposition':'answered','response':'Synthesis zero contains the conditional delivery statement.','reference_ids':['evidence_feature']}]}
    _,values,_=reduce_discussion(run,steps[2],response,{'evidence_feature'})
    run.configuration['discussion_issues']=values
    correction=SimpleNamespace(id='step_synthesis_iteration_1',run_id=run.id,provider='openai',stage='synthesis')
    _,values,_=reduce_discussion(run,correction,{'summary':'Replacement synthesis silently omits the delivery condition.'},{'evidence_feature'})
    run.configuration['discussion_issues']=values
    response['objection_responses'][0].update(disposition='resolved',response='Resolved against the old answer, with no response in replacement synthesis.')
    auditor=SimpleNamespace(id='step_audit_iteration_1',run_id=run.id,provider='anthropic',stage='audit')
    _,values,_=reduce_discussion(run,auditor,response,{'evidence_feature'})
    run.configuration['discussion_issues']=values
    correction.status='succeeded'
    correction.error_code=None
    correction.result_json={'output':{**deepcopy(steps[2].result_json['output']), 'summary':'Replacement recommendation omits the required delivery condition.'}}
    auditor.status='succeeded'
    auditor.error_code=None
    auditor.result_json=deepcopy(steps[3].result_json)
    report=build_report(run,[*steps,correction,auditor],status='completed',stop_reason='protocol_complete',report_id='report_after_correction',version=2,usage=usage,evidence=evidence)
    result={'latest_synthesis_answered_issue':False,'accepted_status':values[0]['status'],'events':[{'step_id':e['step_id'],'disposition':e['disposition']} for e in values[0]['events']],
        'resolution_binding_fields':list(values[0]['events'][-1]), 'report_readiness':report['readiness'], 'latest_summary':report['recommendation']['summary']}
    assert result['accepted_status']=='resolved' and result['report_readiness']=='ready_to_choose'
    return result


if __name__=='__main__':
    result={'expired_reassessment':expired_reassessment(),'export_ledger_loss':export_loss(),'stale_answer_resolution':stale_answer_resolution(),'external_network_calls':0}
    Path(__file__).with_name('probe-results.json').write_text(json.dumps(result,indent=2)+'\n')
    print(json.dumps(result,indent=2))
