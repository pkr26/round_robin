from decimal import Decimal

import pytest
from round_robin.database.models import BudgetOperation, Run, Step, utcnow
from round_robin.database.services import locked_run
from round_robin.orchestration.budget import BudgetDenied, authorize_operation
from sqlalchemy import update
from sqlalchemy.dialects import postgresql

from backend.tests.test_orchestration import seed

pytest_plugins = ("backend.tests.test_orchestration",)


def test_locked_run_selects_only_exact_identity(factory):
    first = seed(factory, suffix="one")
    second = seed(factory, suffix="two")
    with factory() as session:
        assert locked_run(session, first).id == first
        assert locked_run(session, second).id == second
        assert locked_run(session, "run_missing") is None


def test_cached_run_is_refreshed_before_cancelled_run_can_be_authorized(factory):
    run_id = seed(factory)
    with factory.begin() as session:
        run = session.get(Run, run_id)
        run.status, run.first_admitted_at, run.active_started_at = "running", utcnow(), utcnow()
        session.add(Step(id="step_synthetic", run_id=run_id, stage="intake", role_slot_id="intake", provider="openai", status="pending"))
    with factory.begin() as session:
        cached = session.get(Run, run_id)
        step = session.get(Step, "step_synthetic")
        session.execute(update(Run).where(Run.id == run_id).values(status="cancel_requested", execution_fence=9).execution_options(synchronize_session=False))
        assert cached.status == "running"
        with pytest.raises(BudgetDenied, match="run_ineligible"):
            authorize_operation(session, cached, "operation_synthetic", "model", Decimal(0), step=step)
        assert cached.status == "cancel_requested" and cached.execution_fence == 9
        assert session.get(BudgetOperation, "operation_synthetic") is None


def test_postgresql_lock_intent_and_refresh_are_requested_without_connecting():
    captured = []
    class InspectSession:
        def scalar(self, statement):
            captured.append(statement)
    assert locked_run(InspectSession(), "run_synthetic") is None
    assert len(captured) == 1
    query = captured[0]
    assert "FOR UPDATE" in str(query.compile(dialect=postgresql.dialect()))
    assert query.get_execution_options().get("populate_existing") is True


def test_stale_cancelled_state_never_commits_a_model_reservation(factory):
    run_id = seed(factory)
    with factory.begin() as session:
        run = session.get(Run, run_id)
        run.status, run.first_admitted_at, run.active_started_at = "running", utcnow(), utcnow()
        session.add(Step(id="step_synthetic", run_id=run_id, stage="intake", role_slot_id="intake", provider="openai", status="pending"))
    denial = None
    with factory.begin() as session:
        cached, step = session.get(Run, run_id), session.get(Step, "step_synthetic")
        session.execute(update(Run).where(Run.id == run_id).values(status="cancel_requested").execution_options(synchronize_session=False))
        try:
            authorize_operation(session, cached, "operation_synthetic", "model", Decimal(0), step=step)
        except BudgetDenied as exc:
            denial = str(exc)
    with factory() as session:
        assert session.get(Run, run_id).status == "cancel_requested"
        assert (denial, session.get(BudgetOperation, "operation_synthetic") is None) == ("run_ineligible", True)
