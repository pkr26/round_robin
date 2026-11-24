"""Meeting summaries preserve owner scope and continuation ordering."""
from datetime import UTC, datetime, timedelta

from round_robin.database.models import PreflightRecord, Run
from sqlalchemy import event

from backend.tests.test_api import application as application  # noqa: PLC0414
from backend.tests.test_api import client as client  # noqa: PLC0414
from backend.tests.test_api import create, start


def test_meeting_overview_exact_pagination_boundary_and_tied_timestamps(client, application):
    meeting = create(client)
    same_time = datetime(2026, 1, 2, tzinfo=UTC)
    owned_ids = []

    def add_run():
        response = start(client, meeting)
        assert response.status_code == 202, response.text
        run_id = response.json()['id']
        with application.state.session_factory.begin() as session:
            run = session.get(Run, run_id)
            run.status = 'cancelled'
            run.created_at = same_time
        owned_ids.append(run_id)
        return run_id

    for _ in range(20):
        add_run()

    def check(detail, expected, cursor):
        assert detail.status_code == 200, detail.text
        value = detail.json()
        assert [run['id'] for run in value['runs']] == expected
        assert value['next_run_cursor'] == cursor

    twenty = sorted(owned_ids, reverse=True)
    check(client.get(f'/api/meetings/{meeting["id"]}'), twenty, None)
    listing = client.get('/api/meetings')
    assert listing.status_code == 200, listing.text
    assert [run['id'] for run in listing.json()['items'][0]['runs']] == twenty

    add_run()
    order = sorted(owned_ids, reverse=True)
    detail = client.get(f'/api/meetings/{meeting["id"]}')
    check(detail, order[:20], order[19])
    continuation = client.get(f'/api/meetings/{meeting["id"]}/runs', params={'cursor': order[19]})
    assert continuation.status_code == 200, continuation.text
    assert [run['id'] for run in continuation.json()['items']] == order[20:]
    assert continuation.json()['next_cursor'] is None
    # The newest run sorts first even when its ID would sort last, and vice
    # versa. Remaining ties continue to use descending IDs.
    with application.state.session_factory.begin() as session:
        session.get(Run, order[-1]).created_at = same_time + timedelta(days=1)
        session.get(Run, order[0]).created_at = same_time - timedelta(days=1)
    expected = [order[-1], *order[1:-1], order[0]]
    check(client.get(f'/api/meetings/{meeting["id"]}'), expected[:20], expected[19])


def test_meeting_overview_excludes_owner_mismatched_saved_run(client, application):
    meeting = create(client)
    created = start(client, meeting)
    assert created.status_code == 202, created.text
    foreign_id = created.json()['id']
    with application.state.session_factory.begin() as session:
        run = session.get(Run, foreign_id)
        # Deliberately inconsistent saved metadata; regular Start binds the
        # run owner to the meeting owner. No normal cross-owner write assumed.
        run.owner_id = 'owner_foreign'
        run.stop_reason = 'PRIVATE_OTHER_OWNER_SENTINEL'
        run.created_at = datetime.now(UTC) + timedelta(days=1)
        run.status = 'cancelled'
    owned_ids = []
    for _ in range(20):
        created = start(client, meeting)
        assert created.status_code == 202, created.text
        run_id = created.json()['id']
        owned_ids.append(run_id)
        with application.state.session_factory.begin() as session:
            session.get(Run, run_id).status = 'cancelled'
    for path in (f'/api/meetings/{meeting["id"]}', '/api/meetings', f'/api/meetings/{meeting["id"]}/runs'):
        response = client.get(path)
        assert response.status_code == 200, response.text
        assert foreign_id not in response.text
        assert 'PRIVATE_OTHER_OWNER_SENTINEL' not in response.text
    detail = client.get(f'/api/meetings/{meeting["id"]}').json()
    assert {run['id'] for run in detail['runs']} == set(owned_ids)
    assert detail['next_run_cursor'] is None


def test_meeting_overview_bounds_history_query(client, application):
    meeting = create(client)
    created = start(client, meeting)
    assert created.status_code == 202, created.text
    with application.state.session_factory.begin() as session:
        original = session.get(Run, created.json()['id'])
        preflight = session.get(PreflightRecord, original.preflight_id)
        original.status = 'cancelled'
        preflight_data = {column.key: getattr(preflight, column.key)
                          for column in PreflightRecord.__table__.columns
                          if column.key not in {'id', 'consumed_by_run_id'}}
        run_data = {column.key: getattr(original, column.key)
                    for column in Run.__table__.columns if column.key not in {'id', 'preflight_id'}}
        for index in range(120):
            session.add(PreflightRecord(id=f'preflight_bounded_{index}', **preflight_data))
        session.flush()
        for index in range(120):
            session.add(Run(id=f'run_bounded_{index}', preflight_id=f'preflight_bounded_{index}', **run_data))

    queries = []
    engine = application.state.session_factory.kw['bind']

    def capture(_connection, _cursor, sql, parameters, _context, _many):
        if 'FROM runs' in sql and 'ORDER BY runs.created_at DESC' in sql:
            queries.append((sql, parameters))

    event.listen(engine, 'before_cursor_execute', capture)
    try:
        detail = client.get(f'/api/meetings/{meeting["id"]}')
    finally:
        event.remove(engine, 'before_cursor_execute', capture)
    assert detail.status_code == 200, detail.text
    assert len(detail.json()['runs']) == 20
    assert len(queries) == 1
    sql, parameters = queries[0]
    assert 'LIMIT' in sql.upper()
    assert parameters[-2] <= 21
