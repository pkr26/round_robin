"""Concurrent first policy creation must not fail an otherwise safe operation."""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

from round_robin.database.models import PolicyBucket
from round_robin.database.services import lock_policies
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker


def test_concurrent_sqlite_first_operator_policy_creation_is_idempotent(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'policy-race.sqlite'}", connect_args={"timeout": 5})
    PolicyBucket.__table__.create(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory.begin() as session:
        session.add(PolicyBucket(id="policy_deployment", key="deployment", limit_usd="0", spending_locked=True, metadata_json={}))

    # If a select-then-insert implementation observes a missing operator
    # policy, align both readers before either can create it. An atomic insert
    # makes the subsequent read nonempty and never reaches this barrier.
    barrier = Barrier(2, timeout=5)
    original_scalar = Session.scalar

    def aligned_scalar(session, statement, *args, **kwargs):
        result = original_scalar(session, statement, *args, **kwargs)
        if result is None and "owner:owner_operator" in statement.compile().params.values():
            barrier.wait()
        return result

    monkeypatch.setattr(Session, "scalar", aligned_scalar)

    def create_policy():
        with factory.begin() as session:
            return tuple(row.key for row in lock_policies(session, "owner_operator"))

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(create_policy) for _ in range(2)]
        assert [future.result(timeout=10) for future in futures] == [
            ("deployment", "owner:owner_operator"), ("deployment", "owner:owner_operator"),
        ]
    with factory() as session:
        keys = list(session.scalars(select(PolicyBucket.key).order_by(PolicyBucket.key)))
        assert keys == ["deployment", "owner:owner_operator"]
    engine.dispose()
