"""Fresh multi-account adversarial races on a disposable PostgreSQL schema."""
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from threading import Barrier, Event, Lock
import time

from fastapi import Request
from fastapi.testclient import TestClient
from sqlalchemy import event, select

from backend.tests.integration.test_postgres_api import postgres_app, command, create
from backend.tests.test_orchestration import drain
from round_robin.api.auth import require_owner
from round_robin.database.models import ExportRecord, ReportVersion


def test_export_deployment_capacity_is_atomic_across_six_owners(postgres_app, monkeypatch):
    from round_robin.api import artifacts as routes
    app = postgres_app
    def owner(request: Request):
        return request.headers["x-test-owner"]
    app.dependency_overrides[require_owner] = owner
    prepared = []
    for index in range(6):
        identity = f"owner_capacity_{index}"
        with TestClient(app, headers={"x-test-owner": identity}) as client:
            meeting = create(client)
            quote = client.post(f"/api/meetings/{meeting['id']}/preflight", json=command(expected_draft_revision=1)).json()
            run = client.post(f"/api/meetings/{meeting['id']}/runs", json=command(expected_draft_revision=1, preflight_id=quote["id"])).json()
            prepared.append((identity, run["id"]))
    drain(app.state.session_factory, ceiling=60)
    with app.state.session_factory() as session:
        prepared = [(owner, run, session.scalar(select(ReportVersion.id).where(ReportVersion.run_id == run))) for owner, run in prepared]
    entered, released, count_lock = Event(), Event(), Lock()
    count = 0
    original = routes.render_saved_report
    def renderer(*args, **kwargs):
        nonlocal count
        with count_lock:
            count += 1
            if count >= 5:
                entered.set()
        assert released.wait(15), "Test did not release its bounded renderers"
        return original(*args, **kwargs)
    monkeypatch.setattr(routes, "render_saved_report", renderer)
    # Widen the count-to-insert race. The production deployment lock must
    # serialize it even when the other owners reach this query concurrently.
    def widen_race(connection, cursor, statement, parameters, context, executemany):
        if "FROM exports" in statement and "exports.status" in statement and "pending" in str(parameters):
            time.sleep(.05)
    event.listen(app.state.engine, "after_cursor_execute", widen_race)
    start = Barrier(6)
    def export(item):
        identity, run_id, report_id = item
        with TestClient(app, headers={"x-test-owner": identity}) as client:
            start.wait(timeout=10)
            return client.post(f"/api/runs/{run_id}/exports", json=command(report_id=report_id, format="json"))
    try:
        with ThreadPoolExecutor(max_workers=6) as pool:
            futures = [pool.submit(export, item) for item in prepared]
            try:
                assert entered.wait(10), "Five valid exports did not enter the renderer"
                done, _ = wait(futures, timeout=5, return_when=FIRST_COMPLETED)
                assert len(done) == 1, "Capacity must reject the sixth owner before any renderer finishes"
                response = next(iter(done)).result()
                assert response.status_code == 429, response.text
                assert response.json()["error"]["code"] == "export_capacity"
                with app.state.session_factory() as session:
                    assert len(list(session.scalars(select(ExportRecord).where(ExportRecord.status == "pending")))) == 5
            finally:
                released.set()
            assert sorted(future.result().status_code for future in futures) == [200] * 5 + [429]
    finally:
        event.remove(app.state.engine, "after_cursor_execute", widen_race)


def test_concurrent_active_parent_replacement_commits_one_child_and_preserves_original_exposure(postgres_app):
    """Two independent Start commands cannot both spend one replacement grant."""
    from dataclasses import replace
    from decimal import Decimal
    from round_robin.database.models import BudgetOperation, Command, Event as RunEvent, Outbox, PreflightRecord, Run, Step
    from round_robin.orchestration.budget import operations_exposure
    app=postgres_app
    app.state.settings=replace(app.state.settings,max_queued_per_owner=1)
    with TestClient(app) as client:
        meeting=create(client)
        quote=client.post(f"/api/meetings/{meeting['id']}/preflight",json=command(expected_draft_revision=1)).json()
        parent_response=client.post(f"/api/meetings/{meeting['id']}/runs",json=command(expected_draft_revision=1,preflight_id=quote['id']))
        assert parent_response.status_code==202,parent_response.text
        parent=parent_response.json()
        with app.state.session_factory.begin() as session:
            row=session.get(Run,parent['id']);row.active_seconds=100
            session.add(Step(id='step_parent_inflight',run_id=row.id,stage='proposal',role_slot_id='openai',provider='openai',status='authorized',calls=1,infrastructure_claims=1))
            session.add(BudgetOperation(id='op_parent_unknown',run_id=row.id,owner_id=row.owner_id,kind='model',status='unknown',reserved_usd='0.40',settled_usd='0.90',metadata_json={'model_call':True}))
        response=client.post(f"/api/meetings/{meeting['id']}/follow-ups",json=command(parent_run_id=parent['id'],expected_draft_revision=1,brief={**meeting['draft'],'title':'Explicitly reviewed replacement'}))
        assert response.status_code==201,response.text
        linked=response.json()
        quote_response=client.post(f"/api/meetings/{meeting['id']}/preflight",json=command(expected_draft_revision=linked['draft_revision']))
        assert quote_response.status_code==200,quote_response.text
        grant=quote_response.json()
        assert grant['parent_replacement']=={'run_id':parent['id'],'version':1,'status':'queued'}
    bodies=[command(expected_draft_revision=2,preflight_id=grant['id'],replace_parent_run_version=1) for _ in range(2)]
    barrier=Barrier(2)
    def replace_parent(body):
        with TestClient(app) as connection:
            barrier.wait(timeout=10)
            return connection.post(f"/api/meetings/{meeting['id']}/runs",json=body)
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses=list(pool.map(replace_parent,bodies))
    assert sorted(response.status_code for response in responses)==[202,409],[response.text for response in responses]
    accepted_index=next(index for index,response in enumerate(responses) if response.status_code==202)
    rejected_index=1-accepted_index
    child=responses[accepted_index].json()
    assert responses[rejected_index].json()['error']['code']=='preflight_consumed'
    with TestClient(app) as connection:
        replay=connection.post(f"/api/meetings/{meeting['id']}/runs",json=bodies[accepted_index])
        assert replay.status_code==202 and replay.json()==child
    with app.state.session_factory() as session:
        saved_parent=session.get(Run,parent['id']);saved_child=session.get(Run,child['id'])
        assert saved_parent.status=='cancel_requested' and saved_parent.stop_reason=='replaced_by_follow_up'
        assert saved_parent.execution_fence==1 and saved_parent.version==2 and saved_parent.active_seconds==100
        assert saved_parent.input_snapshot==meeting['draft']
        assert saved_child.parent_run_id==saved_parent.id and saved_child.status=='queued'
        assert len(list(session.scalars(select(Run))))==2
        assert len(list(session.scalars(select(Run).where(Run.owner_id==saved_parent.owner_id,Run.status=='queued'))))==1
        assert len(list(session.scalars(select(Outbox).where(Outbox.run_id==saved_parent.id,Outbox.kind=='finalize'))))==1
        assert len(list(session.scalars(select(RunEvent).where(RunEvent.run_id==saved_parent.id,RunEvent.event_type=='run.cancel_requested'))))==1
        assert session.get(PreflightRecord,grant['id']).consumed_by_run_id==saved_child.id
        assert session.get(Command,(saved_parent.owner_id,bodies[rejected_index]['command_id'])) is None
        operations=list(session.scalars(select(BudgetOperation)))
        assert len(operations)==1 and operations_exposure(operations)==Decimal('0.90')
        assert operations[0].status=='unknown' and operations[0].settled_usd=='0.90'
        assert session.get(Step,'step_parent_inflight').calls==1
