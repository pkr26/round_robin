from sqlalchemy import event

from backend.tests.test_orchestration import factory as factory, seed  # noqa: PLC0414
from round_robin.database.models import Run
from round_robin.research.reuse import _reference_statuses
from backend.tests.test_claim_support_roots_regressions import add_source


def test_large_reference_set_does_not_expand_sql_bind_count(factory):
    run_id = seed(factory)
    with factory.begin() as session:
        add_source(session, session.get(Run, run_id), 'evidence_source', 'supported')
    referenced = {f'evidence_{index:06d}' for index in range(66000)} | {'evidence_source'}
    parameter_counts = []
    engine = factory.kw['bind']
    def observe(connection, cursor, statement, parameters, context, executemany):
        if 'FROM evidence' in statement:
            parameter_counts.append(len(parameters))
    event.listen(engine, 'before_cursor_execute', observe)
    try:
        with factory() as session:
            assert _reference_statuses(session, session.get(Run, run_id), referenced) == {'evidence_source': 'supported'}
    finally:
        event.remove(engine, 'before_cursor_execute', observe)
    assert parameter_counts and max(parameter_counts) <= 501
