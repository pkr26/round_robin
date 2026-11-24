"""Deterministic database-boundary faults complement real process termination."""

from datetime import timedelta

from sqlalchemy import event, select
from sqlalchemy.exc import OperationalError

from backend.tests.test_orchestration import factory, seed
from round_robin.database.models import Attempt, BudgetOperation, PolicyBucket, Run, Step, utcnow
from round_robin.dispatcher.service import reconcile_once
from round_robin.orchestration.admission import lock_work_run
from round_robin.orchestration.controller import advance_run
from round_robin.providers.simulated import SimulatedProvider
from round_robin.worker.tasks import execute_step


def first_step(factory):
    run_id = seed(factory)
    with factory() as session, session.begin():
        advance_run(session, lock_work_run(session, run_id))
        step_id = session.scalar(select(Step.id))
    return run_id, step_id


def test_database_unavailable_before_authorization_sends_nothing(factory):
    _, step_id = first_step(factory)
    provider = SimulatedProvider("openai")
    engine = factory.kw["bind"]
    def down(*args):
        raise OperationalError("synthetic database unavailable", {}, Exception("synthetic"))
    event.listen(engine, "before_cursor_execute", down)
    try:
        result = execute_step(step_id, session_factory=factory, adapter_registry={"openai": provider})
        assert result["status"] == "authorization_unconfirmed"
    finally:
        event.remove(engine, "before_cursor_execute", down)
    assert provider.calls == 0
    with factory() as session:
        assert session.scalar(select(Attempt)) is None
        assert session.get(Step, step_id).calls == 0


def test_lost_authorization_commit_ack_never_sends_unconfirmed_effect(factory):
    _, step_id = first_step(factory)
    provider = SimulatedProvider("openai")
    events = []
    def lost_ack(session):
        nested = session.in_nested_transaction()
        events.append("savepoint" if nested else "outer")
        if not nested:
            raise OperationalError("synthetic lost commit acknowledgment", {}, Exception("synthetic"))
    event.listen(factory.class_, "after_commit", lost_ack)
    try:
        result = execute_step(step_id, session_factory=factory, adapter_registry={"openai": provider})
        assert result["status"] == "authorization_unconfirmed"
    finally:
        event.remove(factory.class_, "after_commit", lost_ack)
    assert provider.calls == 0
    assert events == ["savepoint", "outer"]
    with factory() as session:
        attempt = session.scalar(select(Attempt))
        assert attempt is not None and attempt.status == "authorized" and attempt.completed_at is None
        assert session.get(BudgetOperation, attempt.id).status == "reserved"
        step = session.get(Step, step_id)
        assert step.status == "authorized" and step.calls == 1


def test_savepoint_release_ack_failure_rolls_back_authorization_without_sending(factory):
    run_id, step_id = first_step(factory)
    provider = SimulatedProvider("openai")
    from round_robin.orchestration.circuits import _scope
    with factory.begin() as session:
        run = session.get(Run, run_id)
        original_step = session.get(Step, step_id)
        original_status, original_calls = original_step.status, original_step.calls
        scope = _scope(run, "openai")
        bucket = session.scalar(select(PolicyBucket).where(PolicyBucket.key == "deployment"))
        original = {"open_until": (utcnow() - timedelta(seconds=1)).isoformat(), "probe_in_flight": False}
        bucket.metadata_json = {**bucket.metadata_json, "circuits": {scope: original}}
    events = []
    def lost_savepoint_ack(session):
        nested = session.in_nested_transaction()
        events.append("savepoint" if nested else "outer")
        if nested:
            raise OperationalError("synthetic savepoint release acknowledgment", {}, Exception("synthetic"))
    event.listen(factory.class_, "after_commit", lost_savepoint_ack)
    try:
        result = execute_step(step_id, session_factory=factory, adapter_registry={"openai": provider})
        assert result["status"] == "authorization_unconfirmed"
    finally:
        event.remove(factory.class_, "after_commit", lost_savepoint_ack)
    assert events == ["savepoint"] and provider.calls == 0
    with factory() as session:
        assert session.scalar(select(Attempt).where(Attempt.step_id == step_id)) is None
        step = session.get(Step, step_id)
        assert step.status == original_status and step.calls == original_calls
        bucket = session.scalar(select(PolicyBucket).where(PolicyBucket.key == "deployment"))
        assert bucket.metadata_json["circuits"][scope] == original
        assert not [operation for operation in session.scalars(select(BudgetOperation).where(BudgetOperation.run_id == run_id))
                    if operation.metadata_json.get("step_id") == step_id and operation.metadata_json.get("model_call") is True]


def test_response_persistence_retries_same_result_without_regenerating(factory):
    run_id, step_id = first_step(factory)
    provider = SimulatedProvider("openai")
    engine = factory.kw["bind"]
    failures = []
    def failing_writes(connection, cursor, statement, params, context, executemany):
        if statement.startswith("UPDATE attempts") and "response_json" in statement and len(failures) < 3:
            failures.append(1)
            raise OperationalError("synthetic response write outage", {}, Exception("synthetic"))
    event.listen(engine, "before_cursor_execute", failing_writes)
    try:
        result = execute_step(step_id, session_factory=factory, adapter_registry={"openai": provider})
        assert result["status"] == "persistence_unconfirmed"
    finally:
        event.remove(engine, "before_cursor_execute", failing_writes)
    assert provider.calls == 1 and len(failures) == 3
    with factory() as session, session.begin():
        assert session.get(Step, step_id).result_json is None
        step = session.get(Step, step_id)
        step.lease_expires_at = utcnow() - timedelta(seconds=1)
    reconcile_once(session_factory=factory)
    with factory() as session:
        attempt = session.scalar(select(Attempt))
        assert attempt.usage_certainty == "unknown"
        assert session.get(Run, run_id).status == "failed"
        assert session.get(BudgetOperation, attempt.id).status == "unknown"


def test_failure_before_savepoint_release_rolls_back_everything_without_send(factory):
    from round_robin.orchestration import circuits
    run_id, step_id = first_step(factory)
    with factory.begin() as session:
        run, step = session.get(Run, run_id), session.get(Step, step_id)
        original_status, original_calls = step.status, step.calls
        scope = circuits._scope(run, step.provider)
        probe = {"open_until": (utcnow() - timedelta(seconds=1)).isoformat(), "probe_in_flight": False}
        bucket = session.scalar(select(PolicyBucket).where(PolicyBucket.key == "deployment"))
        bucket.metadata_json = {**bucket.metadata_json, "circuits": {scope: probe}}
    provider = SimulatedProvider("openai")
    engine = factory.kw["bind"]
    seen = []
    def fail_release(connection, cursor, statement, params, context, executemany):
        if statement.upper().startswith("RELEASE SAVEPOINT"):
            seen.append(statement)
            raise OperationalError("synthetic savepoint release failure", {}, Exception("synthetic"))
    event.listen(engine, "before_cursor_execute", fail_release)
    try:
        result = execute_step(step_id, session_factory=factory, adapter_registry={"openai": provider})
    finally:
        event.remove(engine, "before_cursor_execute", fail_release)
    assert result["status"] == "authorization_unconfirmed" and provider.calls == 0 and len(seen) == 1
    with factory() as session:
        assert session.scalar(select(Attempt).where(Attempt.step_id == step_id)) is None
        assert not [row for row in session.scalars(select(BudgetOperation)) if row.metadata_json.get("model_call") is True]
        step = session.get(Step, step_id)
        assert step.status == original_status and step.calls == original_calls
        assert session.scalar(select(PolicyBucket).where(PolicyBucket.key == "deployment")).metadata_json["circuits"][scope] == probe
