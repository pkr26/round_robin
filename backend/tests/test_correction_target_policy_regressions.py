import pytest
from round_robin.orchestration.protocol import correction_required


@pytest.mark.parametrize("passed", [True, False, None])
def test_negative_flag_alone_never_creates_an_actionable_target(passed):
    assert not correction_required(audit_passed=passed)


@pytest.mark.parametrize("passed", [True, False, None])
@pytest.mark.parametrize("iteration", [0, 1])
def test_reported_defect_requires_at_most_one_correction(passed, iteration):
    assert correction_required(audit_passed=passed, critical_defects=["Synthetic actionable defect"], iteration=iteration) is (iteration == 0)


@pytest.mark.parametrize("severity", ["minor", "material", "critical"])
@pytest.mark.parametrize("status", ["open", "answered_pending_review", "resolved", "withdrawn"])
@pytest.mark.parametrize("iteration", [0, 1])
def test_only_unresolved_severe_obligations_trigger_at_most_one_correction(severity, status, iteration):
    issue = {"severity": severity, "status": status}
    expected = severity in {"material", "critical"} and status in {"open", "answered_pending_review"} and iteration == 0
    assert correction_required(audit_passed=True, open_objections=[issue], iteration=iteration) is expected


def test_missing_status_defaults_open_and_a_low_severity_peer_does_not_hide_it():
    issues = [{"severity": "minor", "status": "open"}, {"severity": "material"}]
    assert correction_required(audit_passed=True, open_objections=issues)
