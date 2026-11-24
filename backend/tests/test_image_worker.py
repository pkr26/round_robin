"""Durable image execution with synthetic adapters and no paid traffic."""
from dataclasses import asdict
from datetime import timedelta
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from backend.tests.test_image_provider import image_configuration, pixels
from round_robin.database.models import Base, BudgetOperation, Meeting, Owner, utcnow
from round_robin.database.services import lock_policies
from round_robin.database.visual_models import DesignOperation, DesignReview, DesignSession, DesignVersion
from round_robin.orchestration.budget import operations_exposure
from round_robin.providers.base import ProviderFailure
from round_robin.visual.iterations import VisualState
from round_robin.visual.schemas import Critique, ImageCapabilities, VisualBrief
from round_robin.visual.service import version_view
from round_robin.worker.image_tasks import dispatch_pending_image_operations, execute_image_operation, reconcile_image_operations


@pytest.fixture
def image_factory(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'image-worker.sqlite'}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    yield sessionmaker(engine, expire_on_commit=False)
    engine.dispose()


def seed_image(factory, configuration):
    with factory() as session, session.begin():
        session.add(Owner(id="owner_image"))
        session.flush()
        session.add(Meeting(id="meeting_image", owner_id="owner_image", title="Synthetic image test"))
        session.flush()
        deployment, owner = lock_policies(session, "owner_image")
        for bucket in (deployment, owner):
            bucket.spending_locked, bucket.limit_usd = False, "10"
            bucket.metadata_json = {"daily_usd": "10", "monthly_usd": "50", "deployment_epoch": 1}
        state = VisualState("improve", "2", utcnow().timestamp(), paid_admitted=False)
        design = DesignSession(id="design_image", meeting_id="meeting_image", owner_id="owner_image", mode="improve", status="active", revision=1, execution_fence=0, deployment_epoch=1, frozen_brief=VisualBrief(purpose="Create a synthetic green square", audience="Test", width=64, height=64).model_dump(mode="json"), recipients=["openai", "anthropic", "google"], maximum_usd="2", configuration=configuration, state=asdict(state), preflight={"initial_admission_expires_at": (utcnow() + timedelta(minutes=30)).isoformat()})
        session.add(design)
        session.flush()
        operation = DesignOperation(id="image_operation_one", session_id=design.id, provider="openai", payload={"kind": "generation", "status": "queued", "dispatch_status": "pending", "step_id": "generation:version_1", "owner_id": design.owner_id, "session_id": design.id, "fence": 0, "epoch": 1, "bound_usd": configuration["generation_price_usd"], "initial_admission_expires_at": design.preflight["initial_admission_expires_at"], "review_pool_tag": "generation:version_1"})
        session.add(operation)
    return design.id, operation.id


class FakeImageAdapter:
    def __init__(self, provider, configuration, factory, *, callback=None, fail=False):
        self.provider, self.factory = provider, factory
        self.capabilities = ImageCapabilities.model_validate(configuration["capabilities"][provider])
        self.callback, self.fail = callback, fail
        self.calls, self.packets = 0, []

    def _received(self, packet, authorization):
        self.calls += 1
        self.packets.append(packet)
        with self.factory() as session:
            assert session.get(BudgetOperation, authorization.operation_id).status == "reserved"
            assert session.get(DesignOperation, authorization.operation_id).payload["status"] == "authorized"
            assert session.get(DesignSession, authorization.session_id).state["step_calls"][authorization.step_id] <= 2
        if self.callback:
            self.callback(packet, authorization)
        if self.fail:
            raise ProviderFailure("timeout")

    def submit(self, packet, authorization, *, idempotency_key):
        self._received(packet, authorization)
        assert idempotency_key is None
        return {"status": "completed", "image_bytes": pixels(), "actual_usd": None, "provider_request_id": "synthetic_image_request", "usage": {}}

    def review(self, packet, authorization):
        self._received(packet, authorization)
        receipt = packet["receipt"]
        assert packet["images"][0] == pixels()
        return {"status": "completed", "actual_usd": None, "provider_request_id": "synthetic_critic_request", "usage": {}, "critique": Critique(provider=self.provider, version_id=receipt["version_id"], canonical_hash=receipt["canonical_hash"], receipt_id=receipt["id"], findings=[], limitations=["Synthetic fixture only"]).model_dump(mode="json")}


def test_image_worker_authorizes_before_send_and_reviews_same_pixels_with_two_outside_critics(image_factory, image_configuration, tmp_path):
    design_id, operation_id = seed_image(image_factory, image_configuration)
    registry = {provider: FakeImageAdapter(provider, image_configuration, image_factory) for provider in ("openai", "anthropic", "google")}
    directory = tmp_path / "artifacts"
    assert execute_image_operation(operation_id, session_factory=image_factory, provider_registry=registry, artifact_directory=directory)["status"] == "persisted"
    with image_factory() as session:
        queued = [operation.id for operation in session.scalars(select(DesignOperation)) if operation.payload.get("kind") == "vision"]
    assert len(queued) == 2
    for identity in queued:
        assert execute_image_operation(identity, session_factory=image_factory, provider_registry=registry, artifact_directory=directory)["status"] == "persisted"
    execute_image_operation(operation_id, session_factory=image_factory, provider_registry=registry, artifact_directory=directory)
    assert registry["openai"].calls == registry["anthropic"].calls == registry["google"].calls == 1
    with image_factory() as session:
        design = session.get(DesignSession, design_id)
        assert design.state["request_count"] == 3 and design.state["submission_count"] == 1
        version = session.scalar(select(DesignVersion))
        assert version_view(session, version)["coverage"]["complete"]
        assert len(list(session.scalars(select(DesignReview)))) == 2
        operations = list(session.scalars(select(BudgetOperation)))
        assert operations_exposure(operations) == Decimal("0.60")
        assert session.get(BudgetOperation, f"visualfinish_{design_id}").reserved_usd == "0.00"
        assert all(operation.status == "unknown" for operation in operations if operation.kind != "visual_finish_hold")


def test_image_worker_cancel_race_discards_late_asset_but_keeps_uncertain_charge(image_factory, image_configuration, tmp_path):
    design_id, operation_id = seed_image(image_factory, image_configuration)
    def cancel(packet, authorization):
        with image_factory() as session, session.begin():
            design = session.get(DesignSession, design_id)
            state = VisualState(**design.state)
            state.cancel()
            design.status, design.execution_fence, design.state = "cancelled", state.fence, asdict(state)
    registry = {"openai": FakeImageAdapter("openai", image_configuration, image_factory, callback=cancel)}
    execute_image_operation(operation_id, session_factory=image_factory, provider_registry=registry, artifact_directory=tmp_path)
    with image_factory() as session:
        assert session.scalar(select(DesignVersion)) is None
        assert session.get(BudgetOperation, operation_id).status == "unknown"
        assert session.get(DesignSession, design_id).status == "cancelled"
        assert operations_exposure(list(session.scalars(select(BudgetOperation)))) == Decimal("0.40")


def test_image_worker_lost_after_authorization_never_repeats_unknown_submission(image_factory, image_configuration, tmp_path):
    design_id, operation_id = seed_image(image_factory, image_configuration)
    def expire(packet, authorization):
        with image_factory() as session, session.begin():
            operation = session.get(DesignOperation, operation_id)
            operation.payload = {**operation.payload, "lease_expires_at": (utcnow() - timedelta(seconds=1)).isoformat()}
        assert reconcile_image_operations(session_factory=image_factory) == 1
    adapter = FakeImageAdapter("openai", image_configuration, image_factory, callback=expire)
    execute_image_operation(operation_id, session_factory=image_factory, provider_registry={"openai": adapter}, artifact_directory=tmp_path)
    execute_image_operation(operation_id, session_factory=image_factory, provider_registry={"openai": adapter}, artifact_directory=tmp_path)
    assert adapter.calls == 1
    with image_factory() as session:
        assert session.get(DesignOperation, operation_id).payload["status"] == "unknown"
        assert session.get(BudgetOperation, operation_id).status == "unknown"
        assert session.scalar(select(DesignVersion)) is None
        assert session.get(DesignSession, design_id).state["submission_count"] == 1


def test_image_budget_kill_switch_and_invalid_config_block_without_new_reservations(image_factory, image_configuration, tmp_path):
    design_id, operation_id = seed_image(image_factory, image_configuration)
    with image_factory() as session, session.begin():
        deployment, _ = lock_policies(session, "owner_image")
        deployment.spending_locked = True
    adapter = FakeImageAdapter("openai", image_configuration, image_factory)
    assert execute_image_operation(operation_id, session_factory=image_factory, provider_registry={"openai": adapter}, artifact_directory=tmp_path)["status"] == "blocked"
    assert adapter.calls == 0
    with image_factory() as session:
        assert list(session.scalars(select(BudgetOperation))) == []
        assert session.get(DesignSession, design_id).state["request_count"] == 0


def test_image_outbox_dispatch_requires_saved_scope_and_does_not_authorize_send(image_factory, image_configuration):
    _, operation_id = seed_image(image_factory, image_configuration)
    sent = []
    assert dispatch_pending_image_operations(session_factory=image_factory, sender=sent.append) == 1
    assert sent == [operation_id]
    with image_factory() as session:
        assert session.get(DesignOperation, operation_id).payload["dispatch_status"] == "published"
        assert session.scalar(select(BudgetOperation)) is None
