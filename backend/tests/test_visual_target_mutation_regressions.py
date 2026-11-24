import pytest
from round_robin.visual.feedback import regression_summary, regression_targets
from round_robin.visual.schemas import Finding, VisualBrief


def brief(requirements=()):
    return VisualBrief(purpose='Synthetic graphic', audience='Test', locked_requirements=list(requirements)).model_dump(mode='json')


def finding(identity, severity='material', previous=None, disposition='pending'):
    return Finding(id=identity, description=f'Defect {identity}', category='observed_defect', severity=severity,
                   previous_finding_id=previous, disposition=disposition).model_dump(mode='json')


def test_unresolved_material_and_critical_findings_remain_review_blockers():
    findings = [finding('finding_material'), finding('finding_critical', 'critical'),
                finding('finding_persistent', previous='finding_previous', disposition='persistent'),
                finding('finding_addressed_new', disposition='addressed'),
                finding('finding_addressed_prior', previous='finding_old', disposition='addressed'),
                finding('finding_minor', 'minor')]
    targets = regression_targets(brief(), findings)
    expected = {'finding_material', 'finding_critical', 'finding_persistent', 'finding_addressed_new'}
    assert {row['id'] for row in targets} == expected
    result = regression_summary(targets, [], ['anthropic', 'google'])
    assert not result['complete']
    assert set(result['pending_target_ids']) == expected
    assert set(result['unresolved_blockers']) == expected


def test_locked_requirements_keep_distinct_stable_targets_and_critical_metadata():
    value = brief(['Keep the title', 'Keep the title', 'Keep the brand'])
    targets = regression_targets(value, [])
    assert targets == regression_targets(value, [])
    assert [row['description'] for row in targets] == value['locked_requirements']
    assert len({row['id'] for row in targets}) == 3
    assert all(row['id'].startswith('requirement_') and len(row['id']) == 44 for row in targets)
    assert all(row['kind'] == 'locked_requirement' and row['severity'] == 'critical' for row in targets)
    result = regression_summary(targets, [], ['anthropic', 'google'])
    assert set(result['unresolved_blockers']) == {row['id'] for row in targets}


def test_finding_limit_and_identity_are_checked_independently():
    forty = [finding(f'finding_{number}', severity='minor') for number in range(40)]
    assert regression_targets(brief(), forty) == []
    with pytest.raises(ValueError):
        regression_targets(brief(), forty + [finding('finding_extra', severity='minor')])
    with pytest.raises(ValueError):
        regression_targets(brief(), [forty[0], forty[0]])
