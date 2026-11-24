"""Isolated counterexamples for recovery with damaged stored operation metadata."""
from copy import deepcopy
from dataclasses import asdict
from datetime import timedelta

import pytest
from backend.tests.test_image_provider import image_configuration as image_configuration
from backend.tests.test_image_worker import image_factory as image_factory, seed_image
from round_robin.database.models import BudgetOperation, utcnow
from round_robin.database.visual_models import DesignOperation, DesignSession
from round_robin.visual.iterations import VisualState
from round_robin.worker.image_tasks import reconcile_image_operations


def test_malformed_storage_does_not_block_unknown_settlement_in_another_session(image_factory, image_configuration):
    design_id, operation_id = seed_image(image_factory, image_configuration)
    second_id, second_op = "design_synthetic_second", "image_operation_second"
    now = utcnow()
    with image_factory.begin() as session:
        original = session.get(DesignSession, design_id)
        operation = session.get(DesignOperation, operation_id)
        state = VisualState(**original.state)
        state.paid_admitted = True
        state.authorize(operation_id, operation.payload["step_id"], "generation", "0.40", now=now.timestamp(),
                        expected_fence=original.execution_fence, epoch=original.deployment_epoch, review_calls=0, review_seconds=0)
        original.state = asdict(state)
        expired = (now - timedelta(seconds=1)).isoformat()
        operation.payload = {**operation.payload, "status": "authorized", "lease_expires_at": expired,
                             "storage_reservation_id": "storage_synthetic_corrupt"}
        session.add(DesignOperation(id="storage_synthetic_corrupt", session_id=design_id, provider="local", payload=["invalid"]))
        session.add(BudgetOperation(id=operation_id, run_id=design_id, owner_id=original.owner_id, kind="generation",
                                    status="reserved", reserved_usd="0.40", metadata_json={}))
        second_state = deepcopy(original.state)
        second_state["operations"][second_op] = second_state["operations"].pop(operation_id)
        data = {column.name: deepcopy(getattr(original, column.name)) for column in DesignSession.__table__.columns
                if column.name not in {"id", "state"}}
        second = DesignSession(id=second_id, state=second_state, **data)
        session.add(second)
        session.flush()
        second_payload = {**operation.payload, "session_id": second_id, "storage_reservation_id": None}
        session.add(DesignOperation(id=second_op, session_id=second_id, provider="openai", payload=second_payload))
        session.add(BudgetOperation(id=second_op, run_id=second_id, owner_id=original.owner_id, kind="generation",
                                    status="reserved", reserved_usd="0.40", metadata_json={}))
    assert reconcile_image_operations(session_factory=image_factory) >= 1
    with image_factory() as session:
        assert session.get(BudgetOperation, operation_id).status == "unknown"
        assert session.get(BudgetOperation, operation_id).reserved_usd == "0.40"
        assert session.get(DesignOperation, "storage_synthetic_corrupt").payload == ["invalid"]
        assert session.get(BudgetOperation, second_op).status == "unknown"
        assert session.get(BudgetOperation, second_op).reserved_usd == "0.40"
        assert session.get(DesignOperation, second_op).payload["status"] == "unknown"


@pytest.mark.parametrize("broken", ["lease", "missing_ledger", "missing_state"])
def test_malformed_authorized_operation_does_not_stop_other_session_recovery(image_factory, image_configuration, broken):
    design_id, operation_id = seed_image(image_factory, image_configuration)
    second_id, second_op = "design_synthetic_second", "image_operation_second"
    now = utcnow()
    with image_factory.begin() as session:
        original, operation = session.get(DesignSession, design_id), session.get(DesignOperation, operation_id)
        state = VisualState(**original.state)
        state.paid_admitted = True
        state.authorize(operation_id, operation.payload["step_id"], "generation", "0.40", now=now.timestamp(),
                        expected_fence=original.execution_fence, epoch=original.deployment_epoch, review_calls=0, review_seconds=0)
        original.state = asdict(state)
        base = {**operation.payload, "status": "authorized", "lease_expires_at": (now - timedelta(seconds=1)).isoformat(),
                "storage_reservation_id": None}
        operation.payload = {**base, "lease_expires_at": "not-a-time"} if broken == "lease" else base
        if broken != "missing_ledger":
            session.add(BudgetOperation(id=operation_id, run_id=design_id, owner_id=original.owner_id, kind="generation",
                                        status="reserved", reserved_usd="0.40", metadata_json={}))
        second_state = deepcopy(original.state)
        second_state["operations"][second_op] = second_state["operations"].pop(operation_id)
        if broken == "missing_state":
            original.state = {**original.state, "operations": {}}
        data = {column.name: deepcopy(getattr(original, column.name)) for column in DesignSession.__table__.columns
                if column.name not in {"id", "state"}}
        session.add(DesignSession(id=second_id, state=second_state, **data)); session.flush()
        session.add(DesignOperation(id=second_op, session_id=second_id, provider="openai", payload={**base, "session_id": second_id}))
        session.add(BudgetOperation(id=second_op, run_id=second_id, owner_id=original.owner_id, kind="generation",
                                    status="reserved", reserved_usd="0.40", metadata_json={}))
    reconcile_image_operations(session_factory=image_factory)
    with image_factory() as session:
        assert session.get(BudgetOperation, second_op).status == "unknown"
        assert session.get(BudgetOperation, second_op).reserved_usd == "0.40"
        assert session.get(DesignOperation, second_op).payload["status"] == "unknown"
        first = session.get(BudgetOperation, operation_id)
        if first is not None:
            assert first.status == "reserved" and first.reserved_usd == "0.40"
            assert session.get(DesignOperation, operation_id).payload["status"] == "authorized"


@pytest.mark.parametrize("existing_reservation", [False, True])
def test_malformed_target_never_sends_or_rewrites_uncertain_authorization(image_factory, image_configuration, tmp_path, existing_reservation):
    from backend.tests.test_image_worker import FakeImageAdapter
    from round_robin.worker.image_tasks import execute_image_operation
    design_id, operation_id = seed_image(image_factory, image_configuration)
    with image_factory.begin() as session:
        design = session.get(DesignSession, design_id)
        operation = session.get(DesignOperation, operation_id)
        operation.payload = ["damaged"]
        if existing_reservation:
            session.add(BudgetOperation(id=operation_id, run_id=design_id, owner_id=design.owner_id, kind="generation",
                                        status="reserved", reserved_usd="0.40", metadata_json={}))
    adapter = FakeImageAdapter("openai", image_configuration, image_factory)
    result = execute_image_operation(operation_id, session_factory=image_factory, provider_registry={"openai": adapter}, artifact_directory=tmp_path)
    assert result["status"] == "authorization_unconfirmed" and adapter.calls == 0
    with image_factory() as session:
        assert session.get(DesignOperation, operation_id).payload == ["damaged"]
        budget = session.get(BudgetOperation, operation_id)
        if existing_reservation:
            assert budget.status == "reserved" and budget.reserved_usd == "0.40"
        else:
            assert budget is None


@pytest.mark.parametrize("existing_reservation", [False, True])
def test_malformed_sibling_blocks_new_send_without_erasing_possible_prior_reservation(image_factory, image_configuration, tmp_path, existing_reservation):
    from backend.tests.test_image_worker import FakeImageAdapter
    from round_robin.worker.image_tasks import execute_image_operation
    design_id, operation_id = seed_image(image_factory, image_configuration)
    with image_factory.begin() as session:
        design = session.get(DesignSession, design_id)
        session.add(DesignOperation(id="designoperation_damaged", session_id=design_id, provider="anthropic", payload=["damaged"]))
        if existing_reservation:
            session.add(BudgetOperation(id=operation_id, run_id=design_id, owner_id=design.owner_id, kind="generation",
                                        status="reserved", reserved_usd="0.40", metadata_json={}))
    adapter = FakeImageAdapter("openai", image_configuration, image_factory)
    result = execute_image_operation(operation_id, session_factory=image_factory, provider_registry={"openai": adapter}, artifact_directory=tmp_path)
    assert result["status"] == "blocked" and adapter.calls == 0
    with image_factory() as session:
        assert session.get(DesignOperation, operation_id).payload["status"] == "blocked"
        assert session.get(DesignOperation, "designoperation_damaged").payload == ["damaged"]
        budget = session.get(BudgetOperation, operation_id)
        if existing_reservation:
            assert budget.status == "reserved" and budget.reserved_usd == "0.40"
        else:
            assert budget is None


def test_lost_operation_cannot_release_another_session_storage_reservation(image_factory, image_configuration):
    design_id, operation_id = seed_image(image_factory, image_configuration)
    second_id, second_op, foreign_storage = "design_synthetic_second", "image_operation_second", "storage_foreign"
    now = utcnow()
    with image_factory.begin() as session:
        original, operation = session.get(DesignSession, design_id), session.get(DesignOperation, operation_id)
        state = VisualState(**original.state)
        state.paid_admitted = True
        state.authorize(operation_id, operation.payload["step_id"], "generation", "0.40", now=now.timestamp(),
                        expected_fence=original.execution_fence, epoch=original.deployment_epoch, review_calls=0, review_seconds=0)
        original.state = asdict(state)
        base = {**operation.payload, "status": "authorized", "lease_expires_at": (now - timedelta(seconds=1)).isoformat()}
        operation.payload = {**base, "storage_reservation_id": foreign_storage}
        session.add(BudgetOperation(id=operation_id, run_id=design_id, owner_id=original.owner_id, kind="generation",
                                    status="reserved", reserved_usd="0.40", metadata_json={}))
        second_state = deepcopy(original.state)
        second_state["operations"][second_op] = second_state["operations"].pop(operation_id)
        data = {column.name: deepcopy(getattr(original, column.name)) for column in DesignSession.__table__.columns
                if column.name not in {"id", "state"}}
        session.add(DesignSession(id=second_id, state=second_state, **data)); session.flush()
        session.add(DesignOperation(id=second_op, session_id=second_id, provider="openai", payload={**base, "session_id": second_id, "storage_reservation_id": None}))
        session.add(BudgetOperation(id=second_op, run_id=second_id, owner_id=original.owner_id, kind="generation",
                                    status="reserved", reserved_usd="0.40", metadata_json={}))
        session.add(DesignOperation(id=foreign_storage, session_id=second_id, provider="local",
            payload={"kind": "asset_reservation", "status": "reserved", "maximum_bytes": 67108864, "external_operation_id": second_op}))
    reconcile_image_operations(session_factory=image_factory)
    with image_factory() as session:
        assert session.get(BudgetOperation, operation_id).status == "unknown"
        assert session.get(BudgetOperation, operation_id).reserved_usd == "0.40"
        assert session.get(DesignOperation, foreign_storage).payload["status"] == "reserved"
        assert session.get(DesignOperation, foreign_storage).payload["external_operation_id"] == second_op


@pytest.mark.parametrize("storage_status", ["settled", "released"])
def test_purged_meeting_operations_do_not_block_other_session_capacity(image_factory, image_configuration, tmp_path, storage_status):
    from round_robin.database.models import Meeting
    from round_robin.visual.service import purge_designs
    from round_robin.worker.image_tasks import _capacity
    design_id, operation_id = seed_image(image_factory, image_configuration)
    other_id, other_op = "design_synthetic_other", "image_operation_other"
    with image_factory.begin() as session:
        original, operation = session.get(DesignSession, design_id), session.get(DesignOperation, operation_id)
        session.add(DesignOperation(id="storage_prior", session_id=design_id, provider="local", payload={"kind": "asset_reservation", "status": storage_status, "external_operation_id": operation_id, "maximum_bytes": 67108864}))
        session.add(Meeting(id="meeting_synthetic_other", owner_id=original.owner_id, title="Other synthetic")); session.flush()
        data = {column.name: deepcopy(getattr(original, column.name)) for column in DesignSession.__table__.columns
                if column.name not in {"id", "meeting_id"}}
        session.add(DesignSession(id=other_id, meeting_id="meeting_synthetic_other", **data)); session.flush()
        session.add(DesignOperation(id=other_op, session_id=other_id, provider=operation.provider,
                                    payload={**operation.payload, "session_id": other_id}))
        meeting_id = original.meeting_id
    with image_factory.begin() as session:
        purge_designs(session, meeting_id, tmp_path)
    with image_factory() as session:
        assert session.get(DesignSession, design_id).status == "deleted"
        assert "kind" not in session.get(DesignOperation, operation_id).payload
        assert _capacity(session, session.get(DesignSession, other_id), session.get(DesignOperation, other_op))


def test_purged_authorized_tombstone_still_occupies_generation_capacity(image_factory, image_configuration, tmp_path):
    from round_robin.database.models import Meeting
    from round_robin.visual.service import purge_designs
    from round_robin.worker.image_tasks import _capacity
    design_id, operation_id = seed_image(image_factory, image_configuration)
    with image_factory.begin() as session:
        design, op = session.get(DesignSession, design_id), session.get(DesignOperation, operation_id)
        op.payload = {**op.payload, "status": "authorized"}
        session.add(Meeting(id="meeting_other", owner_id=design.owner_id, title="Other")); session.flush()
        data = {column.name: deepcopy(getattr(design, column.name)) for column in DesignSession.__table__.columns if column.name not in {"id", "meeting_id"}}
        session.add(DesignSession(id="design_other", meeting_id="meeting_other", **data)); session.flush()
        session.add(DesignOperation(id="operation_other", session_id="design_other", provider="google", payload={**op.payload, "session_id": "design_other", "status": "queued"}))
        meeting_id = design.meeting_id
    with image_factory.begin() as session:
        purge_designs(session, meeting_id, tmp_path)
    with image_factory() as session:
        assert session.get(DesignOperation, operation_id).payload["status"] == "authorized"
        assert not _capacity(session, session.get(DesignSession, "design_other"), session.get(DesignOperation, "operation_other"))


def test_corruption_after_provider_send_preserves_accounting(image_factory, image_configuration, tmp_path):
    from backend.tests.test_image_worker import FakeImageAdapter
    from round_robin.worker.image_tasks import execute_image_operation
    design_id, operation_id = seed_image(image_factory, image_configuration)
    def corrupt(packet, authorization):
        with image_factory.begin() as session:
            session.get(DesignOperation, operation_id).payload = ["damaged"]
    adapter = FakeImageAdapter("openai", image_configuration, image_factory, callback=corrupt)
    result = execute_image_operation(operation_id, session_factory=image_factory, provider_registry={"openai": adapter}, artifact_directory=tmp_path)
    assert result["status"] == "persistence_unconfirmed"
    assert adapter.calls == 1
    with image_factory() as session:
        assert session.get(DesignOperation, operation_id).payload == ["damaged"]
        assert session.get(BudgetOperation, operation_id).status == "reserved"


def test_publication_does_not_settle_another_operation_storage_hold(image_factory, image_configuration, tmp_path):
    from backend.tests.test_image_worker import FakeImageAdapter
    from round_robin.worker.image_tasks import execute_image_operation
    design_id, operation_id = seed_image(image_factory, image_configuration)
    def corrupt(packet, authorization):
        with image_factory.begin() as session:
            session.add(DesignOperation(id="storage_wrong", session_id=design_id, provider="local", payload={"kind": "asset_reservation", "status": "reserved", "maximum_bytes": 1, "external_operation_id": "different_operation"}))
            operation = session.get(DesignOperation, operation_id)
            operation.payload = {**operation.payload, "storage_reservation_id": "storage_wrong"}
    adapter = FakeImageAdapter("openai", image_configuration, image_factory, callback=corrupt)
    execute_image_operation(operation_id, session_factory=image_factory, provider_registry={"openai": adapter}, artifact_directory=tmp_path)
    assert adapter.calls == 1
    with image_factory() as session:
        assert session.get(DesignOperation, "storage_wrong").payload["status"] == "reserved"


def test_unscoped_review_supplement_blocks_new_paid_dispatch(image_factory, image_configuration, tmp_path):
    from backend.tests.test_image_worker import FakeImageAdapter
    from round_robin.worker.image_tasks import execute_image_operation
    design_id, operation_id = seed_image(image_factory, image_configuration)
    with image_factory.begin() as session:
        session.add(DesignOperation(id="supplement_unscoped", session_id=design_id, provider="anthropic", payload={"kind": "review_supplement", "status": "completed", "critique": {}, "receipt": {}}))
    adapter = FakeImageAdapter("openai", image_configuration, image_factory)
    result = execute_image_operation(operation_id, session_factory=image_factory, provider_registry={"openai": adapter}, artifact_directory=tmp_path)
    assert result["status"] == "blocked" and adapter.calls == 0
    with image_factory() as session:
        assert session.get(BudgetOperation, operation_id) is None
