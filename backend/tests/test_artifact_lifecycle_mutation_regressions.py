import fcntl
import os

import pytest
from round_robin.operations.artifact_lifecycle import (
    artifact_lifecycle,
    hold_artifact_lifecycle,
)
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker


def available(directory):
    fd = os.open(directory / ".artifact-lifecycle.lock", os.O_RDWR)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        fcntl.flock(fd, fcntl.LOCK_UN)
        return True
    finally:
        os.close(fd)


@pytest.fixture
def factory(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'synthetic.sqlite'}")
    yield sessionmaker(engine)
    engine.dispose()


@pytest.mark.parametrize("terminal", ["commit", "rollback"])
def test_transaction_guard_spans_nested_transactions_until_end(factory, tmp_path, terminal):
    directory = tmp_path / "artifacts"
    with factory() as session:
        with pytest.raises(RuntimeError):
            hold_artifact_lifecycle(session, directory)
        assert not directory.exists()
        session.begin()
        hold_artifact_lifecycle(session, directory)
        hold_artifact_lifecycle(session, directory / ".")
        assert not available(directory)
        nested = session.begin_nested()
        nested.commit()
        assert not available(directory)
        getattr(session, terminal)()
        assert available(directory)
        assert "artifact_lifecycle_guards" not in session.info


def test_distinct_transaction_guards_remain_held_and_all_release(factory, tmp_path):
    first, second = tmp_path / "first", tmp_path / "second"
    with factory() as session:
        session.begin()
        hold_artifact_lifecycle(session, first)
        hold_artifact_lifecycle(session, second)
        assert not available(first) and not available(second)
        session.commit()
        assert available(first) and available(second)


def test_transaction_guard_is_reentrant_with_existing_guard(factory, tmp_path):
    directory = tmp_path / "artifacts"
    with artifact_lifecycle(directory):
        with factory() as session:
            session.begin()
            hold_artifact_lifecycle(session, directory)
            assert not available(directory)
            session.commit()
            assert not available(directory)
        assert not available(directory)
    assert available(directory)
