"""Adversarial acceptance bookkeeping checks; all data are synthetic."""
from copy import deepcopy
from dataclasses import replace

import pytest

from backend.tests.test_evaluation import study
from backend.tests.test_release_evaluation import manifest as release_manifest, result as release_result
from experiments.evaluation import deadline_attainment, fingerprint, summarize_workflow
from experiments.release import summarize as release_summary, validate_manifest as release_validate


@pytest.mark.parametrize('field,value', [('admitted','false'),('admitted',1),('mandatory_work_complete','false'),('mandatory_work_complete',1)])
def test_strings_and_numbers_cannot_establish_timing_success(field,value):
    row={'admitted':True,'status':'completed','mandatory_work_complete':True,'active_seconds':1,'first_useful_active_seconds':0}
    row[field]=value
    with pytest.raises(ValueError): deadline_attainment([row])


def test_fixed_evidence_rejects_forged_public_output_hash_in_private_mapping():
    data, outputs, grades, mapping=study()
    selected=grades[0]
    forged=deepcopy(mapping)
    forged['identities'][selected.packet_token]['public_output_hash']=fingerprint('A favorable unrelated public result')
    changed=[replace(selected,output_hash=forged['identities'][selected.packet_token]['public_output_hash'])]
    with pytest.raises(ValueError): summarize_workflow(data,'independent_review',outputs,changed,forged)


def test_fixed_evidence_same_reviewer_cannot_count_twice_through_alias_packets():
    data, outputs, grades, mapping=study()
    selected=next(g for g in grades if g.task_id=='BUD-01' and mapping['identities'][g.packet_token]['primary'])
    records=[g for g in grades if not (g.packet_token==selected.packet_token and g.reviewer_id!=selected.reviewer_id)]
    alias=selected.packet_token+'_alias'
    forged=deepcopy(mapping)
    forged['identities'][alias]=deepcopy(forged['identities'][selected.packet_token])
    duplicate=replace(selected,packet_token=alias,grade_id=selected.grade_id+'_duplicate')
    with pytest.raises(ValueError): summarize_workflow(data,'independent_review',outputs,records+[duplicate],forged)


@pytest.mark.parametrize('timestamp',['not-a-date','2026-11-16T13:00:00','2026-11-15T13:00:00+00:00'])
def test_release_grades_require_prospective_timezone_aware_timestamps(timestamp):
    data=release_manifest(); digest=release_validate(data)
    record=release_result(data['tasks'][0],digest)
    record['original_grades'][0]['timestamp']=timestamp
    with pytest.raises(ValueError): release_summary(data,[record])


def test_release_cannot_reuse_an_original_grade_across_distinct_tasks():
    data=release_manifest(); digest=release_validate(data)
    one,two=[release_result(task,digest) for task in data['tasks'][:2]]
    two['original_grades'][0]['grade_id']=one['original_grades'][0]['grade_id']
    with pytest.raises(ValueError): release_summary(data,[one,two])


def test_anonymous_single_grade_adjudication_cannot_override_a_critical_defect():
    data=release_manifest(); digest=release_validate(data)
    record=release_result(data['tasks'][0],digest,score=2)
    record['adjudication']={'original_grade_ids':[record['original_grades'][0]['grade_id']],'reason':'Retuned result is favorable','usefulness':5,'critical_defect':False}
    with pytest.raises(ValueError): release_summary(data,[record])


def test_visual_usage_never_rounds_away_exposure_above_a_declared_ceiling():
    from backend.tests.test_visual_evaluation import manifest, output
    from experiments.visual_evaluation import build_plan, summarize
    data=manifest(); data['per_trial_limits']['maximum_usd']='1'; data['batch_limits']['maximum_usd']='1'
    item=output(build_plan(data)[0])
    item['usage'].update(reported_usd='0.99999999999999999999999999999',unknown_exposure_usd='0.00000000000000000000000000002')
    summary=summarize(data,[item])
    assert summary['per_trial_limit_violations']==[item['trial_id']]
    assert summary['batch_within_declared_limits'] is False
    assert summary['usage']['reported_usd']=='0.99999999999999999999999999999'


@pytest.mark.parametrize('field,value',[('reported_usd','NaN'),('unknown_exposure_usd','Infinity'),('reported_usd','-0.1'),('reported_usd',True),('active_seconds',float('nan')),('active_seconds',-1),('polls',True)])
def test_visual_study_rejects_invalid_usage_instead_of_improving_the_gate(field,value):
    from backend.tests.test_visual_evaluation import manifest, output
    from experiments.visual_evaluation import build_plan, summarize
    data=manifest(); item=output(build_plan(data)[0]); item['usage'][field]=value
    with pytest.raises(ValueError): summarize(data,[item])


def test_attributed_two_original_adjudication_can_resolve_a_disagreement():
    data=release_manifest(); digest=release_validate(data)
    record=release_result(data['tasks'][0],digest,score=2,critical=True)
    first=record['original_grades'][0]
    second={**first,'grade_id':first['grade_id']+'_second','reviewer_id':'second_reviewer','usefulness':4,'critical_defect':False,'critical_defect_reason':None}
    record['original_grades'].append(second)
    record['adjudication']={'grade_id':'adjudication_original','reviewer_id':'adjudicator','timestamp':'2026-11-16T14:00:00+00:00','output_hash':record['output_hash'],'original_grade_ids':[first['grade_id'],second['grade_id']],'reason':'Both reviewers checked the exact source and resolved the claimed defect','usefulness':4,'critical_defect':False}
    summary=release_summary(data,[record])
    assert summary['useful_primary_tasks']==1
    assert summary['original_critical_defect_task_ids']==[record['task_id']]
    assert summary['phase_5_passed'] is False
