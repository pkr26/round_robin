"""Corrupt clock samples remain unavailable instead of claiming deadlines."""

import pytest
from round_robin.database.models import Run
from round_robin.database.services import append_event
from round_robin.reports.measurements import measure_run

from backend.tests.test_artifacts_api import artifacts as artifacts  # noqa: PLC0414
from backend.tests.test_measurements_mutation_regressions import BASE, event, run


@pytest.mark.parametrize("sample", [True, False, -1, "12", None, [], float("inf"), float("-inf"), float("nan"), 10**400, -(10**400)])
def test_invalid_clock_sample_cannot_establish_timely_completion(sample):
    events = [event(1, "content.committed", 10, qualifying_content=True, active_seconds=sample),
              event(2, "report.published", 20, mandatory_work_complete=True, active_seconds=sample)]
    result = measure_run(run(), events, now=BASE)
    assert result["first_useful_active_seconds"] is None
    assert result["mandatory_completion_active_seconds"] is None
    assert result["historical_missing_clock_samples"]
    assert not result["mandatory_work_complete"]
    assert not any(result["deadline_attainment"].values())


@pytest.mark.parametrize("sample", [0, 0.25])
def test_finite_nonnegative_subsecond_clock_samples_are_retained(sample):
    events = [event(1, "content.committed", 10, qualifying_content=True, active_seconds=sample),
              event(2, "report.published", 20, mandatory_work_complete=True, active_seconds=sample)]
    result = measure_run(run(), events, now=BASE)
    assert result["first_useful_active_seconds"] == sample
    assert result["mandatory_completion_active_seconds"] == sample
    assert result["mandatory_work_complete"] and all(result["deadline_attainment"].values())


@pytest.mark.parametrize("sample", [10**308, 1.7976931348623157e308])
def test_large_finite_clock_samples_remain_available_but_miss_deadlines(sample):
    events = [event(1, "content.committed", 10, qualifying_content=True, active_seconds=sample),
              event(2, "report.published", 20, mandatory_work_complete=True, active_seconds=sample)]
    result = measure_run(run(), events, now=BASE)
    assert result["first_useful_active_seconds"] == float(sample)
    assert result["mandatory_completion_active_seconds"] == float(sample)
    assert result["mandatory_work_complete"] and not any(result["deadline_attainment"].values())


@pytest.mark.parametrize("first_sample", [None, -1, True])
def test_missing_first_useful_clock_is_not_replaced_by_a_later_sample(first_sample):
    events = [
        event(3, "report.published", 30, qualifying_content=True, mandatory_work_complete=True, active_seconds=25),
        event(1, "content.committed", 10, qualifying_content=True, active_seconds=first_sample),
        event(2, "content.committed", 20, qualifying_content=True, active_seconds=15),
    ]
    result = measure_run(run(), events, now=BASE)
    assert result["first_useful_active_seconds"] is None
    assert result["historical_missing_clock_samples"]
    assert not result["deadline_attainment"]["first_useful_within_60"]
    assert result["mandatory_work_complete"]
    assert result["mandatory_completion_active_seconds"] == 25


@pytest.mark.parametrize("first_sample", [None, -1, True])
def test_missing_first_completion_clock_is_not_replaced_by_a_later_report(first_sample):
    events = [
        event(1, "content.committed", 5, qualifying_content=True, active_seconds=3),
        event(2, "report.published", 10, mandatory_work_complete=True, active_seconds=first_sample),
        event(3, "report.published", 20, mandatory_work_complete=True, active_seconds=15),
    ]
    result = measure_run(run(), events, now=BASE)
    assert result["first_useful_active_seconds"] == 3
    assert result["mandatory_completion_active_seconds"] is None
    assert result["historical_missing_clock_samples"]
    assert not result["mandatory_work_complete"]
    assert not any(result["deadline_attainment"].values())


def test_first_eligible_samples_remain_stable_across_later_events():
    events = [
        event(1, "content.committed", 5, qualifying_content=False, active_seconds=None),
        event(2, "report.published", 10, mandatory_work_complete=False, active_seconds=None),
        event(3, "content.committed", 15, qualifying_content=True, active_seconds=10),
        event(4, "report.published", 20, mandatory_work_complete=True, active_seconds=15),
        event(5, "report.published", 30, qualifying_content=True, mandatory_work_complete=True, active_seconds=25),
    ]
    result = measure_run(run(), events, now=BASE)
    assert result["first_useful_active_seconds"] == 10
    assert result["mandatory_completion_active_seconds"] == 15
    assert not result["historical_missing_clock_samples"]
    assert result["mandatory_work_complete"] and all(result["deadline_attainment"].values())


def test_oversized_saved_integer_keeps_measurement_api_readable(artifacts):
    app, client, _, run_id, _ = artifacts
    with app.state.session_factory.begin() as session:
        saved = session.get(Run, run_id)
        append_event(session, saved, "report.published", {"mandatory_work_complete": True, "active_seconds": 10**400})
    response = client.get(f"/api/runs/{run_id}/measurements")
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["mandatory_completion_active_seconds"] is None
    assert data["historical_missing_clock_samples"] and not data["mandatory_work_complete"]
    assert not any(data["deadline_attainment"].values())
