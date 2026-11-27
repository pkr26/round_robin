"""Saved report tampering must fail independently of serialization success."""
from copy import deepcopy
from decimal import Decimal

import pytest
from pydantic import ValidationError

from backend.tests.test_orchestration import factory
from backend.tests.test_runtime_report import runtime_body
from round_robin.schemas.runtime import RuntimeDecisionReport, RuntimeUsage, runtime_cost


@pytest.mark.parametrize('value',[1,True,'-1','1e3','01','NaN','1.'+'0'*25,'9'*31])
def test_runtime_cost_rejects_noncanonical_or_unbounded_values(value):
    with pytest.raises(ValueError): runtime_cost(value)


@pytest.mark.parametrize('mutation,code',[
    ({'status':'reported','actual_cost':None},'Reported usage'),
    ({'status':'reported','unknown_operation_ids':['attempt_unknown']},'Reported usage'),
    ({'actual_cost':'0.01'},'Simulation cannot report'),
])
def test_usage_modes_cannot_claim_false_final_spend(mutation,code):
    body={'simulated':True,'status':'reported','actual_cost':'0','conservative_exposure_usd':'0','unknown_operation_ids':[],'request_count':0,**mutation}
    with pytest.raises(ValidationError,match=code): RuntimeUsage.model_validate(body)


@pytest.mark.parametrize('case,code',[
    ('duplicate_ids','Authoritative scoped'),('mode','mode labels'),('usage_mode','mode labels'),
    ('duplicate_option','unique identities'),('duplicate_calc','unique identities'),('overlap','cannot overlap'),
    ('claim_overlap','Claim identities'),('unscoped_claim','Claim scope'),('contribution_context','Contribution scope'),
    ('run_context','another run'),('mode_context','Report mode'),('parent_context','lineage'),
    ('missing_calculation','exact saved calculation'),('currency','currency and horizon'),('horizon','currency and horizon'),
    ('missing_subtotal','known subtotal'),('altered_subtotal','known subtotal'),('cost_gaps','cost gaps'),
    ('duplicate_requirement','same hard requirement'),('foreign_requirement','frozen scope'),('foreign_basis','frozen scope'),
    ('foreign_choice','unconsidered option'),('missing_synthesis','Missing synthesis'),('readiness_reason','explicit reason'),
])
def test_saved_report_rejects_tampering_on_each_authoritative_binding(runtime_body,case,code):
    body=deepcopy(runtime_body);ctx={}
    if case=='duplicate_ids': body['evidence_ids'].append(body['evidence_ids'][0])
    if case=='mode': body['fictional']=False
    if case=='usage_mode': body['usage']['simulated']=False
    if case=='duplicate_option': body['comparison'].append(deepcopy(body['comparison'][0]))
    if case=='duplicate_calc': body['calculations'].append(deepcopy(body['calculations'][0]))
    if case=='overlap': body['contribution_ids'].append(body['evidence_ids'][0])
    if case=='claim_overlap': body['claim_ids']=[body['evidence_ids'][0]]
    if case=='unscoped_claim': ctx['allowed_claim_ids']=['claim_missing']
    if case=='contribution_context': ctx['allowed_contribution_ids']=[]
    if case=='run_context': ctx['run_id']='run_foreign'
    if case=='mode_context': ctx['expected_simulated']=False
    if case=='parent_context': ctx['parent_report_id']='report_other'
    if case=='missing_calculation': body['comparison'][0]['calculation_id']='calc_missing'
    if case=='currency': body['comparison'][0]['currency']='EUR'
    if case=='horizon': body['comparison'][0]['horizon_months']=99
    if case=='missing_subtotal': body['comparison'][0]['known_subtotal']=None
    if case=='altered_subtotal': body['comparison'][0]['known_subtotal']='1'
    if case=='cost_gaps': body['comparison'][0]['missing_components']=['hidden omitted charge']
    if case=='duplicate_requirement': body['comparison'][0]['constraint_results'].append(deepcopy(body['comparison'][0]['constraint_results'][0]))
    if case=='foreign_requirement': body['comparison'][0]['constraint_results'][0]['requirement_id']='requirement_foreign'
    if case=='foreign_basis': body['comparison'][0]['constraint_results'][0]['basis_refs']=['evidence_foreign']
    if case=='foreign_choice': body['recommendation']={'option_id':'option_foreign','summary':'Bad choice','conditions':[]}
    if case=='missing_synthesis': body['assessment']['synthesis_valid']=False
    if case=='readiness_reason': body['readiness_unavailable_reason']='readiness is actually unavailable'
    with pytest.raises(ValidationError,match=code): RuntimeDecisionReport.model_validate(body,context=ctx)


def genuinely_ready(body):
    body=deepcopy(body)
    body['fictional']=body['simulated']=body['usage']['simulated']=False
    body['assessment'].update(synthesis_valid=True,audit_succeeded=True,unresolved_critical_defect=False,proposal_providers=['openai','anthropic','google'],synthesizer='openai',auditor='google')
    option=body['comparison'][0]
    for result in option['constraint_results']:
        result.update(result='pass',basis_refs=[option['calculation_id']])
    for dependency in body['essential_dependencies']:
        dependency.update(status='supported',accepted_scoped_support=True,reference_ids=[body['evidence_ids'][0]])
    body['recommendation']={'option_id':option['option_id'],'summary':'Scoped live reviewed choice','conditions':[]}
    body['readiness']='ready_to_choose'
    body['readiness_unavailable_reason']=None
    # A positive review quorum is insufficient under the scoped-assessment
    # contract. This synthetic ready fixture explicitly supplies its receipts
    # and complete readiness dimensions; endpoint binding is tested separately.
    from datetime import datetime, timezone
    body['evidence_assessments']=[]
    for index,item in enumerate(body['comparison']):
        if item is not option:
            calculation=next(value for value in body['calculations'] if value['id']==item['calculation_id'])
            item['total']=calculation['output']=item['known_subtotal']
            item['missing_components']=calculation['missing_components']=[]
        body['evidence_assessments'].append({'id':f'assessment_synthetic_{index}','target_type':'option_costs',
            'option_id':item['option_id'],'requirement_id':None,'target_hash':'a'*64,
            'reference_ids':[body['evidence_ids'][0]],'evidence_hashes':{body['evidence_ids'][0]:'b'*64},
            'result':'supported','attestation':'Synthetic owner attestation of exact fixture inputs.',
            'actor_id':'owner_test','run_id':body['run_id'],'recorded_at':datetime.now(timezone.utc).isoformat()})
    body['readiness_dimensions']={key:True for key in ('complete_comparison','checked_arithmetic','scoped_cost_support','requirements_assessed','distinct_review','material_objections_resolved')}
    body['readiness_dimensions']['basis_scope']='Explicit synthetic owner attestations; no real factual certification.'
    return body


def test_actual_ready_requires_complete_checked_cost_even_with_all_positive_reviews(runtime_body):
    body=genuinely_ready(runtime_body)
    # The seed includes installation/maintenance gaps. Deliberately keep every
    # cost identity aligned while exposing the missing charge to the runtime gate.
    selected=body['comparison'][0]
    calc=next(item for item in body['calculations'] if item['id']==selected['calculation_id'])
    selected['missing_components']=calc['missing_components']=['Supplier must confirm installation charge']
    selected['total']=calc['output']=None
    with pytest.raises(ValidationError,match='incomplete comparable cost'):
        RuntimeDecisionReport.model_validate(body)
    selected['missing_components']=calc['missing_components']=[]
    selected['total']=calc['output']=selected['known_subtotal']
    from round_robin.reports.insights import cost_comparison
    body['decision_insights'].update(cost_comparison(set(body['hard_requirement_ids']), body['comparison']))
    assert RuntimeDecisionReport.model_validate(body).readiness=='ready_to_choose'
    body['fictional']=body['simulated']=body['usage']['simulated']=True
    with pytest.raises(ValidationError,match='simulated runtime'):
        RuntimeDecisionReport.model_validate(body)
