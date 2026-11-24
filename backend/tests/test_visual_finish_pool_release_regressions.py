from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from round_robin.database.models import Base, BudgetOperation, PolicyBucket
from round_robin.visual.service import release_unused_visual_finish


@pytest.fixture
def ledger(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'ledger.sqlite'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    with factory.begin() as session:
        for identity, run_id, kind, status in [
            ("held_first", "design_test", "visual_finish_hold", "held"),
            ("held_second", "design_test", "visual_finish_hold", "held"),
            ("other_design", "design_other", "visual_finish_hold", "held"),
            ("other_kind", "design_test", "visual_edit", "held"),
            ("unknown", "design_test", "visual_finish_hold", "unknown"),
            ("reserved", "design_test", "visual_finish_hold", "reserved"),
        ]:
            session.add(BudgetOperation(id=identity, run_id=run_id, owner_id="owner_test", kind=kind, status=status, reserved_usd="0.20", metadata_json={"target_tag": None, "roles_remaining": []}))
    yield factory
    engine.dispose()


def test_release_clears_only_held_pools_in_the_target_design(ledger):
    with ledger.begin() as session:
        release_unused_visual_finish(session, SimpleNamespace(id="design_test", owner_id="owner_test"), "user_cancellation")
    with ledger() as session:
        assert session.query(PolicyBucket).filter_by(key="owner:owner_test").first() is not None
        assert session.query(PolicyBucket).filter_by(key="owner:None").first() is None
        for identity in ("held_first", "held_second"):
            row = session.get(BudgetOperation, identity)
            assert (row.status, row.reserved_usd, row.metadata_json["release_reason"]) == ("released", "0", "user_cancellation")
        for identity, status in [("other_design", "held"), ("other_kind", "held"), ("unknown", "unknown"), ("reserved", "reserved")]:
            row = session.get(BudgetOperation, identity)
            assert row.status == status and row.reserved_usd == "0.20"
