"""Queue delivery order must not override durable FIFO admission tickets."""
from datetime import timedelta

from backend.tests.test_orchestration import factory, seed
from round_robin.database.models import Run, utcnow
from round_robin.orchestration.admission import admit_run, lock_work_run


def test_out_of_order_delivery_does_not_skip_an_older_queued_run(factory):
    first = seed(factory, suffix="first")
    second = seed(factory, suffix="second")
    with factory.begin() as session:
        assert admit_run(session, lock_work_run(session, second)) is False
        assert session.get(Run, second).status == "queued"
        assert admit_run(session, lock_work_run(session, first)) is True


def test_resumed_old_run_joins_queue_at_its_new_ticket_time(factory):
    resumed = seed(factory, suffix="resumed")
    waiting = seed(factory, suffix="waiting")
    with factory.begin() as session:
        now = utcnow()
        row = session.get(Run, resumed)
        row.created_at = now - timedelta(hours=1)
        row.first_admitted_at = now - timedelta(minutes=40)
        row.resumption_expires_at = now + timedelta(minutes=30)
        session.get(Run, waiting).created_at = now - timedelta(minutes=5)
    with factory.begin() as session:
        assert admit_run(session, lock_work_run(session, resumed)) is False
        assert admit_run(session, lock_work_run(session, waiting)) is True


def test_expired_older_ticket_does_not_block_a_valid_queued_run(factory):
    expired = seed(factory, suffix="expired")
    valid = seed(factory, suffix="valid")
    with factory.begin() as session:
        session.get(Run, expired).initial_admission_expires_at = utcnow() - timedelta(seconds=1)
    with factory.begin() as session:
        assert admit_run(session, lock_work_run(session, valid)) is True
