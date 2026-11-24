"""Lost image workers retain spend exposure and release only their storage hold."""

from datetime import timedelta
from decimal import Decimal

import pytest
from round_robin.database.models import BudgetOperation, utcnow
from round_robin.database.visual_models import DesignOperation
from round_robin.worker import image_tasks
from round_robin.worker.image_tasks import (
    execute_image_operation,
    reconcile_image_operations,
)

from backend.tests.test_image_worker import FakeImageAdapter, seed_image

pytest_plugins = ("backend.tests.test_image_provider", "backend.tests.test_image_worker")


class LostImageAdapter(FakeImageAdapter):
    def submit(self, packet, authorization, *, idempotency_key):
        self._received(packet, authorization)
        raise KeyboardInterrupt


@pytest.mark.parametrize("earlier_operation", ["none", "live", "removed_before_lock"])
@pytest.mark.parametrize("expiry_boundary", ["past", "equal"])
def test_reconciler_preserves_exposure_and_releases_lost_operation_storage(
    image_factory, image_configuration, tmp_path, monkeypatch, earlier_operation, expiry_boundary
):
    _, original_id = seed_image(image_factory, image_configuration)
    operation_id = original_id
    if earlier_operation != "none":
        # Keep a still-leased operation ahead of the expired operation in the
        # same queue. It must not prevent later work from being reconciled.
        with image_factory.begin() as session:
            first = session.get(DesignOperation, original_id)
            queued = dict(first.payload)
            first.payload = {
                **queued,
                "kind": "vision",
                "status": "authorized",
                "lease_owner": "live_lease",
                "lease_expires_at": (utcnow() + timedelta(minutes=10)).isoformat(),
            }
            first.provider = "anthropic"
            operation_id = "image_operation_lost"
            session.add(DesignOperation(id=operation_id, session_id=first.session_id, provider="openai", payload=queued))

    adapter = LostImageAdapter("openai", image_configuration, image_factory)
    with pytest.raises(KeyboardInterrupt):
        execute_image_operation(operation_id, session_factory=image_factory, provider_registry={"openai": adapter}, artifact_directory=tmp_path)
    assert adapter.calls == 1
    now = utcnow()
    with image_factory.begin() as session:
        operation = session.get(DesignOperation, operation_id)
        storage_id = operation.payload["storage_reservation_id"]
        assert session.get(DesignOperation, storage_id).payload["status"] != "released"
        expires = now if expiry_boundary == "equal" else now - timedelta(seconds=1)
        operation.payload = {**operation.payload, "lease_expires_at": expires.isoformat()}

    monkeypatch.setattr(image_tasks, "utcnow", lambda: now)
    if earlier_operation == "removed_before_lock":
        original_lock = image_tasks._locked_design

        def lock_unless_removed(session, identity):
            return (None, None, None) if identity == original_id else original_lock(session, identity)

        monkeypatch.setattr(image_tasks, "_locked_design", lock_unless_removed)
    assert reconcile_image_operations(session_factory=image_factory) == 1
    with image_factory() as session:
        operation = session.get(DesignOperation, operation_id)
        assert operation.payload["status"] == "unknown"
        assert operation.payload["dispatch_status"] == "done"
        assert operation.payload["lease_owner"] is None
        assert operation.payload["lease_expires_at"] is None
        assert operation.payload["error_code"] == "image_worker_lost_after_authorization"
        assert session.get(DesignOperation, storage_id).payload["status"] == "released"
        budget = session.get(BudgetOperation, operation_id)
        assert budget.status == "unknown" and Decimal(budget.reserved_usd) > 0
        if earlier_operation != "none":
            assert session.get(DesignOperation, original_id).payload["status"] == "authorized"

    assert reconcile_image_operations(session_factory=image_factory) == 0
    assert execute_image_operation(
        operation_id, session_factory=image_factory, provider_registry={"openai": adapter}, artifact_directory=tmp_path
    )["status"] == "already_authorized_or_settled"
    assert adapter.calls == 1
