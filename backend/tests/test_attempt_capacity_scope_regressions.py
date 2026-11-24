from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from backend.tests.test_orchestration import factory as factory, seed  # noqa: PLC0414
from round_robin.database.models import Attempt, Step, utcnow
from round_robin.orchestration.admission import attempt_occupies_capacity, provider_capacity_available


def add_step(session, run_id, identity, expiry):
    session.add(Step(id=identity, run_id=run_id, stage='proposal', role_slot_id=identity, provider='openai',
                     status='authorized', lease_expires_at=expiry))
    session.flush()


@pytest.mark.parametrize('offset,expected', [(-0.000001, False), (0, False), (0.000001, True)])
def test_actual_step_lease_has_exclusive_expiry_boundary(factory, offset, expected):
    run_id = seed(factory)
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    with factory.begin() as session:
        add_step(session, run_id, 'step_lease', now + timedelta(seconds=offset))
        attempt = SimpleNamespace(step_id='step_lease', authorized_at=now - timedelta(minutes=10), run_id=run_id, provider='openai', lease_owner=None)
        assert attempt_occupies_capacity(session, attempt, now=now) is expected


@pytest.mark.parametrize('step_mode', ['no_step', 'no_lease'])
@pytest.mark.parametrize('age,expected', [(149.999999, True), (150, False), (150.000001, False)])
def test_missing_lease_uses_exclusive_150_second_authorization_window(factory, step_mode, age, expected):
    run_id = seed(factory)
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    with factory.begin() as session:
        if step_mode == 'no_lease': add_step(session, run_id, 'step_missing_lease', None)
        attempt = SimpleNamespace(step_id='step_missing_lease', authorized_at=(now - timedelta(seconds=age)).replace(tzinfo=None), run_id=run_id, provider='openai', lease_owner=None)
        assert attempt_occupies_capacity(session, attempt, now=now) is expected


def test_foreign_step_expired_lease_cannot_free_capacity_before_attempt_window(factory):
    own = seed(factory, suffix='owned')
    other = seed(factory, suffix='other')
    now = utcnow()
    with factory.begin() as session:
        add_step(session, own, 'step_owned', now + timedelta(seconds=60))
        add_step(session, other, 'step_foreign', now - timedelta(seconds=60))
        for identity, step in [('attempt_1', 'step_owned'), ('attempt_2', 'step_owned'), ('attempt_bad', 'step_foreign')]:
            session.add(Attempt(id=identity, run_id=own, step_id=step, provider='openai', status='authorized',
                                authorized_at=now, reserved_usd='0.10', configuration={}))
        session.flush()
        assert not provider_capacity_available(session, 'google', mandatory=True)


def test_foreign_step_does_not_shorten_capacity_delay(factory):
    from round_robin.orchestration.admission import capacity_delay_seconds
    own, other = seed(factory, suffix='owned'), seed(factory, suffix='other')
    now = utcnow()
    with factory.begin() as session:
        add_step(session, other, 'step_foreign', now - timedelta(seconds=60))
        session.add(Attempt(id='attempt_bad', run_id=own, step_id='step_foreign', provider='openai', status='authorized',
                            authorized_at=now, reserved_usd='0.10', configuration={}))
        session.flush()
        assert 145 <= capacity_delay_seconds(session) <= 150


def test_foreign_future_step_does_not_hold_past_attempt_window(factory):
    own, other = seed(factory, suffix='owned'), seed(factory, suffix='other')
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    with factory.begin() as session:
        add_step(session, other, 'step_foreign', now + timedelta(days=3))
        attempt = SimpleNamespace(step_id='step_foreign', authorized_at=now-timedelta(seconds=151), run_id=own, provider='openai', lease_owner=None)
        assert attempt_occupies_capacity(session, attempt, now=now) is False


def test_capacity_refreshes_stale_step_before_scope_decision(factory):
    own, other = seed(factory, suffix='owned'), seed(factory, suffix='other')
    now = utcnow()
    with factory.begin() as session:
        add_step(session, own, 'step_changed', now - timedelta(seconds=60))
    with factory() as reader:
        cached = reader.get(Step, 'step_changed')
        with factory.begin() as writer:
            writer.get(Step, 'step_changed').run_id = other
        attempt = SimpleNamespace(step_id='step_changed', authorized_at=now, run_id=own, provider='openai', lease_owner=None)
        assert attempt_occupies_capacity(reader, attempt, now=now) is True
        assert cached.run_id == other
