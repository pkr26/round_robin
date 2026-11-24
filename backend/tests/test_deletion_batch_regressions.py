import math

import pytest
from round_robin.database.models import Base, Meeting, Owner
from round_robin.operations.deletion_scope import deletion_scopes_consistent
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker


@pytest.fixture
def factory(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'scope.sqlite'}")
    Base.metadata.create_all(engine)
    maker = sessionmaker(engine, expire_on_commit=False)
    with maker.begin() as session:
        session.add_all([Owner(id="owner_test"), Owner(id="owner_other")])
        session.flush()
        session.add_all(Meeting(id=f"meeting_{i:04}", owner_id="owner_test", title="Synthetic") for i in range(250))
    yield maker, engine
    engine.dispose()


@pytest.mark.parametrize("bad_index", [0, 99, 100, 199, 200, 249])
def test_any_bad_owner_fences_all_chunks(factory, bad_index):
    maker, _ = factory
    pairs = [(f"meeting_{i:04}", "owner_test") for i in range(250)]
    with maker.begin() as session:
        session.get(Meeting, pairs[bad_index][0]).owner_id = "owner_other"
    with maker() as session:
        assert not deletion_scopes_consistent(session, pairs)


def test_scope_precheck_uses_one_query_per_bounded_chunk(factory):
    maker, engine = factory
    pairs = [(f"meeting_{i:04}", "owner_test") for i in range(250)]
    calls = []
    def count(*args):
        calls.append(1)
    event.listen(engine, "before_cursor_execute", count)
    try:
        with maker() as session:
            assert deletion_scopes_consistent(session, pairs)
    finally:
        event.remove(engine, "before_cursor_execute", count)
    assert len(calls) == math.ceil(len(pairs) / 100)
