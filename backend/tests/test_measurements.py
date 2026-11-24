from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from round_robin.reports.measurements import measure_run


BASE = datetime(2026, 11, 16, 12, tzinfo=timezone.utc)


def run(status='completed'):
    return SimpleNamespace(id='run_test', status=status, created_at=BASE, first_admitted_at=BASE + timedelta(seconds=10), active_seconds=40, active_started_at=None, configuration={'mode': 'simulated'})


def event(sequence, kind, second, **payload):
    return {'sequence': sequence, 'event_type': kind, 'timestamp': BASE + timedelta(seconds=second), 'payload': payload}


def test_progress_and_intake_echo_do_not_establish_first_useful_content():
    events = [event(1, 'run.admitted', 10), event(2, 'content.committed', 12, stage='intake', qualifying_content=False, active_seconds=2), event(3, 'content.committed', 20, qualifying_content=True, active_seconds=10), event(4, 'report.published', 50, qualifying_content=True, mandatory_work_complete=True, active_seconds=40)]
    measured = measure_run(run(), events)
    assert measured['first_useful_active_seconds'] == 10
    assert measured['mandatory_completion_active_seconds'] == 40
    assert measured['initial_queue_seconds'] == 10 and measured['wall_seconds_from_accepted_start'] == 50
    assert all(measured['deadline_attainment'].values())


def test_failed_and_cancelled_admitted_trials_remain_deadline_misses():
    for status in ('failed', 'cancelled', 'completed_with_limitations'):
        measured = measure_run(run(status), [event(1, 'content.committed', 20, qualifying_content=True, active_seconds=10), event(2, 'report.published', 50, qualifying_content=True, active_seconds=40)])
        assert measured['admitted'] and not any(measured['deadline_attainment'].values())


def test_input_wait_and_resumption_queue_are_separate_from_active_time():
    events = [event(1, 'run.admitted', 10), event(2, 'brief.confirmation_required', 20), event(3, 'brief.confirmed', 7220), event(4, 'run.admitted', 7250), event(5, 'report.published', 7280, qualifying_content=True, mandatory_work_complete=True, active_seconds=40)]
    measured = measure_run(run(), events)
    assert measured['user_input_wait_seconds'] == 7200 and measured['resumption_queue_seconds'] == 30
    assert measured['active_seconds'] == 40


def test_historical_missing_samples_remain_unknown_not_fabricated_zero():
    measured = measure_run(run(), [event(1, 'report.published', 50)])
    assert measured['first_useful_active_seconds'] is None and measured['mandatory_completion_active_seconds'] is None
    assert measured['historical_missing_clock_samples'] and not any(measured['deadline_attainment'].values())
