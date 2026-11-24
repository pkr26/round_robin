from decimal import Decimal

import pytest
from round_robin.database.models import Attempt, BudgetOperation, Event, Step
from round_robin.orchestration.admission import lock_work_run
from round_robin.orchestration.controller import advance_run
from round_robin.providers.simulated import SimulatedProvider
from round_robin.worker.tasks import execute_step
from sqlalchemy import event, select
from sqlalchemy.exc import OperationalError

from backend.tests.test_orchestration import factory as factory  # noqa: PLC0414
from backend.tests.test_orchestration import seed


@pytest.mark.parametrize("boundary", ["savepoint_release", "outer_commit"])
def test_post_send_commit_ack_failure_never_repeats_provider(factory, boundary):
    rid = seed(factory)
    with factory.begin() as session:
        advance_run(session, lock_work_run(session, rid))
        sid = session.scalar(
            select(Step.id).where(Step.run_id == rid, Step.status == "pending")
        )
        assert sid
    provider = SimulatedProvider("openai")
    seen = []
    armed = []

    class RecordingProvider(SimulatedProvider):
        def generate(self, request):
            result = super().generate(request)
            armed.append(True)
            return result

    provider = RecordingProvider("openai")

    def fail_once(session):
        if not armed:
            return
        nested = session.in_nested_transaction()
        seen.append(nested)
        if not getattr(fail_once, "fired", False) and nested == (
            boundary == "savepoint_release"
        ):
            fail_once.fired = True
            raise OperationalError(
                "synthetic acknowledgement loss", {}, Exception("synthetic")
            )

    event.listen(factory.class_, "after_commit", fail_once)
    try:
        outcome = execute_step(
            sid, session_factory=factory, adapter_registry={"openai": provider}
        )
    finally:
        event.remove(factory.class_, "after_commit", fail_once)
    assert fail_once.fired and provider.calls == 1, (outcome, seen)
    assert outcome["status"] in {"persisted", "already_persisted"}, outcome
    with factory() as session:
        attempts = list(session.scalars(select(Attempt).where(Attempt.step_id == sid)))
        assert len(attempts) == 1
        ledger = session.get(BudgetOperation, attempts[0].id)
        assert (
            attempts[0].completed_at is not None
            and ledger.status == "settled"
            and Decimal(ledger.settled_usd) == 0
        )
        committed = list(
            session.scalars(
                select(Event.id).where(
                    Event.run_id == rid, Event.event_type == "content.committed"
                )
            )
        )
        assert len(committed) == 1
    execute_step(sid, session_factory=factory, adapter_registry={"openai": provider})
    assert provider.calls == 1


@pytest.mark.parametrize("usage", ["reported", "unknown"])
def test_unavailable_fallback_outer_ack_loss_preserves_receipt_and_fence(
    factory, monkeypatch, usage
):
    from round_robin.api.preflight import run_configuration
    from round_robin.configuration import Settings
    from round_robin.database.models import Run
    from round_robin.database.services import lock_policies
    from round_robin.providers.base import ProviderFailure
    from sqlalchemy import text

    monkeypatch.setattr(
        "round_robin.orchestration.configuration.verify_frozen_configuration",
        lambda _: None,
    )
    config = run_configuration(Settings(), "simulated")
    config.update(mode="live", funded=True)
    rid = seed(factory, config=config)
    with factory.begin() as session:
        for policy in lock_policies(session, "owner_test"):
            policy.spending_locked = False
            policy.limit_usd = "10"
            policy.metadata_json = {"daily_usd": "10", "monthly_usd": "50"}
        advance_run(session, lock_work_run(session, rid))
        sid = session.scalar(
            select(Step.id).where(Step.run_id == rid, Step.status == "pending")
        )
        assert sid
    armed = []

    class Provider(SimulatedProvider):
        def conservative_bound(self, request):
            return Decimal("0.40")

        def generate(self, request):
            super().generate(request)
            with factory.begin() as session:
                bad = Step(
                    id="bad_step",
                    run_id=rid,
                    stage="proposal",
                    role_slot_id="bad",
                    provider="google",
                    status="failed",
                    result_json={},
                )
                session.add(bad)
                session.flush()
                session.execute(
                    text("UPDATE steps SET result_json=:raw WHERE id=:id"),
                    {"raw": '{"PRIVATE_SENTINEL":', "id": bad.id},
                )
            armed.append(True)
            raise ProviderFailure(
                "refusal",
                may_have_sent=True,
                cost_usd=Decimal("0.25") if usage == "reported" else None,
            )

    provider = Provider("openai")
    seen = []

    def fail_once(session):
        if not armed:
            return
        nested = session.in_nested_transaction()
        seen.append(nested)
        if not nested and not getattr(fail_once, "fired", False):
            fail_once.fired = True
            raise OperationalError(
                "synthetic outer acknowledgement loss", {}, Exception("synthetic")
            )

    event.listen(factory.class_, "after_commit", fail_once)
    try:
        outcome = execute_step(
            sid, session_factory=factory, adapter_registry={"openai": provider}
        )
    finally:
        event.remove(factory.class_, "after_commit", fail_once)
    assert fail_once.fired and provider.calls == 1, (outcome, seen)
    assert outcome["status"] == "already_persisted"
    with factory() as session:
        attempt = session.scalar(select(Attempt).where(Attempt.step_id == sid))
        ledger = session.get(BudgetOperation, attempt.id)
        run = session.get(Run, rid)
        assert attempt.completed_at is not None and attempt.usage_certainty == usage
        assert ledger.status == ("settled" if usage == "reported" else "unknown")
        assert (
            Decimal(ledger.settled_usd) if ledger.settled_usd is not None else None
        ) == (Decimal("0.25") if usage == "reported" else None)
        assert (
            run.status == "failed"
            and run.configuration.get("saved_state_unavailable") is True
        )
        assert session.get(BudgetOperation, f"finish_{rid}").status == "held"
    execute_step(sid, session_factory=factory, adapter_registry={"openai": provider})
    assert provider.calls == 1


def test_failure_before_post_receipt_savepoint_release_never_resends(factory):
    rid = seed(factory)
    with factory.begin() as session:
        advance_run(session, lock_work_run(session, rid))
        sid = session.scalar(
            select(Step.id).where(Step.run_id == rid, Step.status == "pending")
        )
        assert sid
    armed = []
    root_savepoint = []
    injected = []

    class Provider(SimulatedProvider):
        def generate(self, request):
            result = super().generate(request)
            armed.append(True)
            return result

    provider = Provider("openai")
    engine = factory.kw["bind"]

    def fail_release(conn, cursor, statement, parameters, context, executemany):
        if not armed:
            return
        words = statement.strip().split()
        if len(words) == 2 and words[0].upper() == "SAVEPOINT" and not root_savepoint:
            root_savepoint.append(words[1])
        if (
            len(words) == 3
            and words[:2] == ["RELEASE", "SAVEPOINT"]
            and root_savepoint
            and words[2] == root_savepoint[0]
            and not injected
        ):
            injected.append(True)
            raise OperationalError(
                "synthetic savepoint release failure", {}, Exception("synthetic")
            )

    event.listen(engine, "before_cursor_execute", fail_release)
    try:
        outcome = execute_step(
            sid, session_factory=factory, adapter_registry={"openai": provider}
        )
    finally:
        event.remove(engine, "before_cursor_execute", fail_release)
    assert injected and provider.calls == 1, (outcome, root_savepoint)
    assert outcome["status"] in {"persisted", "already_persisted"}
    with factory() as session:
        attempts = list(session.scalars(select(Attempt).where(Attempt.step_id == sid)))
        assert len(attempts) == 1
        ledger = session.get(BudgetOperation, attempts[0].id)
        assert (
            attempts[0].completed_at
            and ledger.status == "settled"
            and Decimal(ledger.settled_usd) == 0
        )
        assert (
            len(
                list(
                    session.scalars(
                        select(Event.id).where(
                            Event.run_id == rid, Event.event_type == "content.committed"
                        )
                    )
                )
            )
            == 1
        )
    execute_step(sid, session_factory=factory, adapter_registry={"openai": provider})
    assert provider.calls == 1


def test_generic_programming_error_is_not_converted_to_saved_state_fence(
    factory, monkeypatch
):
    from round_robin.database.models import Run
    from round_robin.worker import tasks

    rid = seed(factory)
    with factory.begin() as session:
        advance_run(session, lock_work_run(session, rid))
        sid = session.scalar(
            select(Step.id).where(Step.run_id == rid, Step.status == "pending")
        )
        assert sid
    provider = SimulatedProvider("openai")

    def defect(*_args, **_kwargs):
        raise ValueError("synthetic programming error")

    monkeypatch.setattr(tasks, "advance_run", defect)
    with pytest.raises(ValueError, match="synthetic programming error"):
        execute_step(
            sid, session_factory=factory, adapter_registry={"openai": provider}
        )
    assert provider.calls == 1
    with factory() as session:
        attempt = session.scalar(select(Attempt).where(Attempt.step_id == sid))
        ledger = session.get(BudgetOperation, attempt.id)
        run = session.get(Run, rid)
        assert attempt.completed_at is None and ledger.status == "reserved"
        assert not run.configuration.get("saved_state_unavailable")
