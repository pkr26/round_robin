"""Finish reservations retain their latest state and release only eligible holds."""

import pytest
from round_robin.database.models import BudgetOperation, Run
from round_robin.orchestration.budget import release_unused_finish, reserve_finish

from backend.tests.test_orchestration import factory as factory  # noqa: PLC0414 - pytest fixture
from backend.tests.test_orchestration import seed


def test_reserving_again_refreshes_a_hold_changed_by_another_session(factory):
    run_id = seed(factory)
    with factory.begin() as session:
        reserve_finish(session, session.get(Run, run_id))

    with factory() as reader:
        run = reader.get(Run, run_id)
        cached = reader.get(BudgetOperation, f"finish_{run_id}")
        assert cached.reserved_usd == "0"
        with factory.begin() as writer:
            writer.get(BudgetOperation, cached.id).reserved_usd = "0.27"

        refreshed = reserve_finish(reader, run)
        assert refreshed is cached
        assert refreshed.reserved_usd == "0.27"


@pytest.mark.parametrize(
    ("kind", "status", "released"),
    [
        ("finish_hold", "held", True),
        ("model", "held", False),
        ("audit", "held", False),
        ("finish_hold", "settled", False),
        ("finish_hold", "authorized", False),
    ],
)
def test_release_does_not_change_other_operation_kinds_or_states(factory, kind, status, released):
    run_id = seed(factory)
    with factory.begin() as session:
        run = session.get(Run, run_id)
        hold = BudgetOperation(
            id=f"finish_{run_id}", run_id=run_id, owner_id=run.owner_id,
            kind=kind, status=status, reserved_usd="0.40", metadata_json={},
        )
        session.add(hold)
        session.flush()

        release_unused_finish(session, run)

        assert hold.status == ("released" if released else status)
        assert hold.reserved_usd == ("0" if released else "0.40")
