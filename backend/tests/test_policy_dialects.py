"""Policy creation preserves existing state and defaults to locked spending."""

import os
import sys
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import uuid4

import pytest
from round_robin.database.models import PolicyBucket
from round_robin.database.safety import require_disposable_postgres
from round_robin.database.services import lock_policies
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import Session, sessionmaker


@pytest.fixture(params=["sqlite", "postgresql"])
def policy_engine(request, tmp_path):
    if request.param == "sqlite":
        engine = create_engine(f"sqlite:///{tmp_path / 'policy.sqlite'}", connect_args={"timeout": 5})
        admin = None
    else:
        value = os.environ.get("ROUND_ROBIN_TEST_DATABASE_URL")
        if not value:
            pytest.skip("Disposable PostgreSQL URL is not configured")
        url = require_disposable_postgres(value)
        admin = create_engine(url)
        schema = "policy_" + uuid4().hex
        with admin.begin() as connection:
            connection.execute(text(f"CREATE SCHEMA {schema}"))
        engine = create_engine(url, connect_args={"options": "-csearch_path=" + schema})
    # Dialect names are strings; callers cannot rely on their object identity.
    engine.dialect.name = engine.dialect.name.encode().decode()
    PolicyBucket.__table__.create(engine)
    try:
        yield engine
    finally:
        engine.dispose()
        if admin:
            with admin.begin() as connection:
                connection.execute(text(f"DROP SCHEMA {schema} CASCADE"))
            admin.dispose()


def test_existing_policies_are_preserved_and_missing_owner_is_locked(policy_engine):
    factory = sessionmaker(policy_engine, expire_on_commit=False)
    with factory.begin() as session:
        session.add(PolicyBucket(id="deployment_existing", key="deployment", limit_usd="123.45", spending_locked=False, version=7, metadata_json={"synthetic": "keep"}))
    with factory.begin() as session:
        deployment, owner = lock_policies(session, "example")
        assert (deployment.id, deployment.limit_usd, deployment.spending_locked, deployment.version, deployment.metadata_json) == ("deployment_existing", "123.45", False, 7, {"synthetic": "keep"})
        assert (owner.key, owner.limit_usd, owner.spending_locked, owner.version, owner.metadata_json) == ("owner:example", "0", True, 1, {})
    with factory.begin() as session:
        assert [row.key for row in lock_policies(session, "example")] == ["deployment", "owner:example"]
        assert len(list(session.scalars(select(PolicyBucket)))) == 2


def test_interned_dialect_names_select_the_correct_insert(policy_engine):
    policy_engine.dialect.name = sys.intern(policy_engine.dialect.name)
    with sessionmaker(policy_engine).begin() as session:
        assert [row.key for row in lock_policies(session, "example")] == ["deployment", "owner:example"]


def test_concurrent_initial_policy_creation_succeeds(policy_engine, monkeypatch):
    factory = sessionmaker(policy_engine, expire_on_commit=False)
    barrier = Barrier(2, timeout=5)
    original = Session.scalar

    def aligned(session, statement, *args, **kwargs):
        result = original(session, statement, *args, **kwargs)
        if result is None and "deployment" in statement.compile().params.values():
            barrier.wait()
        return result

    monkeypatch.setattr(Session, "scalar", aligned)

    def call():
        with factory.begin() as session:
            return [bucket.key for bucket in lock_policies(session, "example")]

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(call) for _ in range(2)]
        assert [future.result(timeout=10) for future in futures] == [["deployment", "owner:example"]] * 2
    with factory() as session:
        assert sorted(session.scalars(select(PolicyBucket.key))) == ["deployment", "owner:example"]
