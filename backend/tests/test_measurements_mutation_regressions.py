from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from round_robin.reports.measurements import measure_run

BASE = datetime(2026, 11, 1, tzinfo=UTC)


def run(status='completed', **overrides):
    values = {'id': 'run_test', 'status': status, 'created_at': BASE, 'first_admitted_at': BASE + timedelta(seconds=10),
                  'active_seconds': 0, 'active_started_at': None, 'configuration': {'mode': 'simulated'}}
    values.update(overrides)
    return SimpleNamespace(**values)


def event(sequence, kind, second, **payload):
    return {'sequence': sequence, 'event_type': kind, 'timestamp': BASE + timedelta(seconds=second), 'payload': payload}


def test_wait_and_queue_periods_accumulate_in_sequence_order():
    events = [event(6, 'run.admitted', 100), event(4, 'run.admitted', 50), event(1, 'brief.confirmation_required', 20),
              event(7, 'report.published', 120), event(2, 'brief.confirmed', 40),
              event(5, 'brief.confirmation_required', 60), event(3, 'run.admitted', 45)]
    result = measure_run(run(), events, now=BASE + timedelta(seconds=200))
    assert result['user_input_wait_seconds'] == 80
    assert result['resumption_queue_seconds'] == 5
    assert result['wall_seconds_from_accepted_start'] == 120


def test_unfinished_queue_and_zero_elapsed_are_not_rounded_up():
    events = [event(1, 'brief.confirmation_required', 20), event(2, 'brief.confirmation_accepted', 20),
              event(3, 'run.admitted', 20), event(4, 'brief.confirmation_accepted', 30)]
    result = measure_run(run('running', first_admitted_at=None, active_started_at=BASE + timedelta(seconds=30)), events, now=BASE + timedelta(seconds=30))
    assert result['user_input_wait_seconds'] == result['resumption_queue_seconds'] == 0
    assert result['active_seconds'] == 0
    assert result['initial_queue_seconds'] == 30


@pytest.mark.parametrize('status', ['completed_with_limitations', 'failed', 'cancelled'])
def test_terminal_partial_results_stop_timing_at_published_report(status):
    result = measure_run(run(status, active_started_at=BASE + timedelta(seconds=20)), [event(1, 'report.published', 50)], now=BASE + timedelta(seconds=100))
    assert result['wall_seconds_from_accepted_start'] == 50
    assert result['active_seconds'] == 30
    assert not result['mandatory_work_complete']
    assert not any(result['deadline_attainment'].values())


def test_terminal_without_report_uses_current_time_and_running_ignores_publish_time():
    assert measure_run(run('failed'), [], now=BASE + timedelta(seconds=80))['wall_seconds_from_accepted_start'] == 80
    assert measure_run(run('running'), [event(1, 'report.published', 50)], now=BASE + timedelta(seconds=80))['wall_seconds_from_accepted_start'] == 80


def test_report_clock_requires_explicit_mandatory_completion():
    result = measure_run(run(), [event(1, 'content.committed', 20, qualifying_content=True, active_seconds=10),
                                 event(2, 'report.published', 50, mandatory_work_complete=False, active_seconds=40)], now=BASE + timedelta(seconds=80))
    assert result['first_useful_active_seconds'] == 10
    assert result['mandatory_completion_active_seconds'] is None
    assert not result['mandatory_work_complete']
    assert result['historical_missing_clock_samples']
    assert not any(result['deadline_attainment'].values())


@pytest.mark.parametrize(('useful', 'complete', 'first', 'within_120', 'within_300'), [
    (60, 120, True, True, True), (61, 121, False, False, True), (60, 300, True, False, True),
    (61, 301, False, False, False),
])
def test_deadline_thresholds_are_inclusive(useful, complete, first, within_120, within_300):
    result = measure_run(run(), [event(1, 'content.committed', 20, qualifying_content=True, active_seconds=useful),
                                 event(2, 'report.published', 50, mandatory_work_complete=True, active_seconds=complete)], now=BASE + timedelta(seconds=80))
    assert result['deadline_attainment'] == {'first_useful_within_60': first, 'complete_within_120': within_120, 'complete_within_300': within_300}
    assert result['mandatory_work_complete'] and not result['historical_missing_clock_samples']


def test_mode_and_missing_clock_samples_are_truthful():
    completed = [event(1, 'report.published', 20, mandatory_work_complete=True, active_seconds=10)]
    result = measure_run(run(configuration={'mode': 'live'}), completed, now=BASE + timedelta(seconds=50))
    assert result['run_id'] == 'run_test' and result['status'] == 'completed'
    assert 'human usefulness' in result['scope'] and 'real-provider' in result['scope']
    assert result['simulated'] is False
    assert result['first_useful_active_seconds'] is None and result['mandatory_completion_active_seconds'] == 10
    assert result['historical_missing_clock_samples']
    assert measure_run(run(configuration={}), [], now=BASE + timedelta(seconds=50))['simulated'] is True


def test_elapsed_bounds_include_zero_when_times_equal():
    result = measure_run(run('running', first_admitted_at=BASE), [], now=BASE)
    assert result['wall_seconds_from_accepted_start'] == result['initial_queue_seconds'] == result['active_seconds'] == 0


def test_open_wait_and_resumption_queue_preserve_earlier_periods():
    waiting = [event(1, 'brief.confirmation_required', 10), event(2, 'brief.confirmed', 20),
               event(3, 'run.admitted', 30), event(4, 'brief.confirmation_required', 40)]
    result = measure_run(run('running'), waiting, now=BASE + timedelta(seconds=40))
    assert result['user_input_wait_seconds'] == 10
    queued = [event(1, 'brief.confirmed', 10), event(2, 'run.admitted', 20), event(3, 'brief.confirmed', 30)]
    result = measure_run(run('running'), queued, now=BASE + timedelta(seconds=30))
    assert result['resumption_queue_seconds'] == 10


def test_repeated_closed_wait_and_queue_periods_accumulate():
    events = [event(1, 'brief.confirmation_required', 10), event(2, 'brief.confirmed', 20),
              event(3, 'run.admitted', 25), event(4, 'brief.confirmation_required', 30),
              event(5, 'brief.confirmed', 50), event(6, 'run.admitted', 60)]
    result = measure_run(run('running'), events, now=BASE + timedelta(seconds=70))
    assert result['user_input_wait_seconds'] == 30
    assert result['resumption_queue_seconds'] == 15


def test_unadmitted_run_has_zero_queue_time_at_creation():
    result = measure_run(run('running', first_admitted_at=None), [], now=BASE)
    assert not result['admitted'] and result['initial_queue_seconds'] == 0
