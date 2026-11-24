"""Remaining request counts reconcile only this run's saved evidence."""
import pytest
from round_robin.database.models import BudgetOperation, Run, Step

from backend.tests.test_api import application as application  # noqa: PLC0414
from backend.tests.test_api import client as client  # noqa: PLC0414
from backend.tests.test_api import create, start


@pytest.mark.parametrize(
    "step_counts,ledger_count,maximum,expected",
    [([2, 1], 2, 24, 21), ([1], 4, 24, 20), ([2, 1], 2, 3, 0), ([2, 1], 2, 7, 4)],
)
def test_run_and_snapshot_show_remaining_requests_from_own_counters_and_ledger(client, application, step_counts, ledger_count, maximum, expected):
    meeting, other_meeting = create(client), create(client)
    run_response, other_response = start(client, meeting), start(client, other_meeting)
    assert run_response.status_code == other_response.status_code == 202
    run_id, other_id = run_response.json()["id"], other_response.json()["id"]
    with application.state.session_factory.begin() as session:
        run = session.get(Run, run_id)
        run.configuration = {**run.configuration, "limits": {**run.configuration["limits"], "max_requests": maximum}}
        for owner_run, counts in ((run_id, step_counts), (other_id, [2, 2, 2, 2])):
            for index, calls in enumerate(counts):
                session.add(Step(id=f"{owner_run}_step_{index}", run_id=owner_run, stage="proposal", role_slot_id=f"role_{index}", provider="openai", status="failed", calls=calls))
        for owner_run, count in ((run_id, ledger_count), (other_id, 6)):
            for index in range(count):
                session.add(BudgetOperation(id=f"{owner_run}_operation_{index}", run_id=owner_run, owner_id="owner_local", kind="image", status="unknown", reserved_usd="1", metadata_json={"model_call": True}))
        session.add(BudgetOperation(id="non_model_hold", run_id=run_id, owner_id="owner_local", kind="finish_hold", status="held", reserved_usd="1", metadata_json={}))
    response = client.get(f"/api/runs/{run_id}")
    snapshot = client.get(f"/api/runs/{run_id}/snapshot")
    assert response.status_code == snapshot.status_code == 200
    assert response.json()["remaining_limits"]["requests"] == expected
    assert snapshot.json()["run"]["remaining_limits"]["requests"] == expected
