"""Corrupt visual capacity state cannot erase a paid text receipt."""
from copy import deepcopy
from decimal import Decimal

import pytest
from round_robin.database.models import Attempt, BudgetOperation, Event, Step
from round_robin.database.visual_models import DesignOperation
from round_robin.orchestration.admission import lock_work_run
from round_robin.orchestration.controller import advance_run
from round_robin.providers.base import ProviderFailure
from round_robin.providers.simulated import SimulatedProvider
from round_robin.worker.tasks import execute_step
from sqlalchemy import select

from backend.tests.test_image_provider import synthetic_image_configuration
from backend.tests.test_image_worker import seed_image
from backend.tests.test_orchestration import factory as factory  # noqa: PLC0414
from backend.tests.test_orchestration import seed


@pytest.mark.parametrize("damaged", [None, [], {"status": "authorized", "lease_expires_at": "invalid"},
                                      {"status": "authorized", "lease_expires_at": None},
                                      {"status": "authorized", "lease_expires_at": []},
                                      {"kind": "vision", "status": "claimed"},
                                      {"kind": "vision", "status": "running"},
                                      {"kind": "vision", "status": "unrecognized"}])
@pytest.mark.parametrize("reported", [False, True])
def test_corrupt_visual_capacity_after_send_preserves_text_receipt_without_retry(factory, damaged, reported):
    run_id = seed(factory)
    seed_image(factory, synthetic_image_configuration())
    with factory.begin() as session:
        advance_run(session, lock_work_run(session, run_id))
        step_id = session.scalar(select(Step.id).where(Step.run_id == run_id, Step.stage == "intake"))
    class FailingProvider(SimulatedProvider):
        def generate(self, request):
            self.calls += 1
            with factory.begin() as session:
                session.get(DesignOperation, "image_operation_one").payload = deepcopy(damaged)
            raise ProviderFailure("authentication", may_have_sent=True, cost_usd=Decimal(0) if reported else None,
                                  provider_request_id="synthetic_request")
    first, fallback = FailingProvider("openai"), SimulatedProvider("anthropic")
    result = execute_step(step_id, session_factory=factory, adapter_registry={"openai": first, "anthropic": fallback})
    assert result["status"] == "persisted" and first.calls == 1 and fallback.calls == 0
    with factory() as session:
        step = session.get(Step, step_id)
        attempt = session.scalar(select(Attempt).where(Attempt.step_id == step_id))
        assert attempt.completed_at is not None and attempt.provider_request_id == "synthetic_request"
        ledger = session.get(BudgetOperation, attempt.id)
        assert (attempt.status, attempt.usage_certainty, ledger.status) == (("failed", "reported", "settled") if reported else ("unknown", "unknown", "unknown"))
        assert step.status == "failed" and step.provider == "openai" and step.calls == 1
        assert session.get(DesignOperation, "image_operation_one").payload == damaged
        assert not list(session.scalars(select(Event).where(Event.run_id == run_id, Event.event_type.in_(["step.retry_scheduled", "role.reassigned"]))))


@pytest.mark.parametrize("damaged", [None, [], {"status": "authorized", "lease_expires_at": "invalid"},
                                      {"status": "authorized", "lease_expires_at": None},
                                      {"kind": "vision", "status": "claimed"},
                                      {"kind": "vision", "status": "running"},
                                      {"kind": "vision", "status": "unrecognized"}])
def test_corrupt_visual_capacity_before_text_authorization_never_sends(factory, damaged):
    run_id = seed(factory)
    seed_image(factory, synthetic_image_configuration())
    with factory.begin() as session:
        advance_run(session, lock_work_run(session, run_id))
        step_id = session.scalar(select(Step.id).where(Step.run_id == run_id, Step.stage == "intake"))
        session.get(DesignOperation, "image_operation_one").payload = deepcopy(damaged)
    first, fallback = SimulatedProvider("openai"), SimulatedProvider("anthropic")
    result = execute_step(step_id, session_factory=factory, adapter_registry={"openai": first, "anthropic": fallback})
    assert result["status"] in {"policy_blocked", "capacity_wait"} and first.calls == fallback.calls == 0
    with factory() as session:
        assert session.scalar(select(Attempt).where(Attempt.step_id == step_id)) is None
        assert session.get(Step, step_id).calls == 0
        assert session.get(DesignOperation, "image_operation_one").payload == damaged
        assert not [row for row in session.scalars(select(BudgetOperation).where(BudgetOperation.run_id == run_id)) if row.metadata_json.get("model_call") is True]


def test_statusless_legacy_review_supplement_is_not_an_inflight_image_call(factory, tmp_path):
    from dataclasses import asdict

    from round_robin.database.visual_models import DesignReview, DesignVersion
    from round_robin.orchestration.admission import (
        capacity_delay_seconds,
        provider_capacity_available,
    )
    from round_robin.visual.assets import validate_raster
    from round_robin.visual.service import saved_version_reviews

    from backend.tests.test_visual import png
    from backend.tests.test_visual_review_commit_mutation_regressions import (
        new_critique,
        new_receipt,
    )
    seed(factory)
    seed_image(factory, synthetic_image_configuration())
    raster = validate_raster(png())
    with factory.begin() as session:
        version = DesignVersion(id="version_old", session_id="design_image", version=1, canonical_hash=raster.canonical_hash,
                                original_hash=raster.original_hash, asset_path=str(tmp_path / "synthetic.png"),
                                width=raster.width, height=raster.height, source_kind="upload", provenance={})
        session.add(version)
        session.flush()
        receipt = new_receipt(version, "anthropic")
        critique = new_critique(version, receipt).model_dump(mode="json")
        session.add(DesignReview(id="review_old", session_id="design_image", version_id=version.id, provider="anthropic", receipt=asdict(receipt), critique=critique))
        session.add(DesignOperation(id="supplement_old", session_id="design_image", provider="anthropic",
                                    payload={"kind": "review_supplement", "version_id": version.id, "source_review_id": "review_old", "receipt": asdict(receipt), "critique": critique}))
    with factory() as session:
        reviews, supplements = saved_version_reviews(session, "version_old")
        assert len(reviews) == len(supplements) == 1
        assert capacity_delay_seconds(session) == 0
        assert provider_capacity_available(session, "openai")


@pytest.mark.parametrize("status", ["authorized", "unknown"])
def test_deleted_ambiguous_operation_cannot_be_treated_as_free_capacity(factory, status):
    from round_robin.database.visual_models import DesignSession
    from round_robin.orchestration.admission import (
        capacity_delay_seconds,
        provider_capacity_available,
    )
    seed(factory)
    seed_image(factory, synthetic_image_configuration())
    with factory.begin() as session:
        session.get(DesignSession, "design_image").status = "deleted"
        session.get(DesignOperation, "image_operation_one").payload = {"status": status}
    with factory() as session:
        assert capacity_delay_seconds(session) == float("inf")
        assert provider_capacity_available(session, "openai")  # One known-provider tombstone occupies one of two provider slots.


@pytest.mark.parametrize("status,ambiguous", [("queued", False), ("completed", False), ("authorized", True), ("unknown", True)])
def test_real_purge_tombstone_capacity_is_conservative(factory, tmp_path, status, ambiguous):
    from round_robin.database.models import utcnow
    from round_robin.database.visual_models import DesignSession
    from round_robin.orchestration.admission import (
        capacity_delay_seconds,
        provider_capacity_available,
    )
    from round_robin.visual.service import purge_designs
    seed(factory)
    seed_image(factory, synthetic_image_configuration())
    with factory.begin() as session:
        operation = session.get(DesignOperation, "image_operation_one")
        operation.payload = {**operation.payload, "status": status, "lease_expires_at": utcnow().isoformat()}
    with factory.begin() as session:
        purge_designs(session, "meeting_image", tmp_path)
    with factory() as session:
        assert session.get(DesignSession, "design_image").status == "deleted"
        assert "kind" not in session.get(DesignOperation, "image_operation_one").payload
        assert capacity_delay_seconds(session) == (float("inf") if ambiguous else 0)
        assert provider_capacity_available(session, "openai")  # Zero or one occupied slot remains under both thresholds.


@pytest.mark.parametrize("expired", [False, True])
def test_valid_visual_lease_keeps_original_capacity_behavior(factory, monkeypatch, expired):
    from datetime import timedelta

    from round_robin.database.models import utcnow
    from round_robin.orchestration import admission
    seed(factory)
    seed_image(factory, synthetic_image_configuration())
    now = utcnow()
    with factory.begin() as session:
        operation = session.get(DesignOperation, "image_operation_one")
        operation.payload = {**operation.payload, "status": "authorized",
                             "lease_expires_at": (now + timedelta(seconds=-1 if expired else 30)).isoformat()}
    monkeypatch.setattr(admission, "utcnow", lambda: now)
    with factory() as session:
        assert admission.capacity_delay_seconds(session) == (0 if expired else 30)
        # One valid active image still leaves the configured two-per-provider capacity.
        assert admission.provider_capacity_available(session, "openai")


@pytest.mark.parametrize("providers,target,expected", [
    (["openai"], "openai", True),
    (["anthropic"], "openai", True),
    (["openai", "openai"], "openai", False),
    (["anthropic", "google"], "openai", True),
    (["openai", "anthropic", "google"], "openai", False),
])
def test_real_purged_tombstones_count_global_and_provider_slots(factory, tmp_path, providers, target, expected):
    from round_robin.orchestration.admission import (
        capacity_delay_seconds,
        provider_capacity_available,
    )
    from round_robin.visual.service import purge_designs
    seed(factory)
    seed_image(factory, synthetic_image_configuration())
    with factory.begin() as session:
        operation = session.get(DesignOperation, "image_operation_one")
        operation.provider = providers[0]
        operation.payload = {**operation.payload, "status": "authorized"}
        for index, provider in enumerate(providers[1:], 1):
            session.add(DesignOperation(id=f"extra_visual_{index}", session_id="design_image", provider=provider,
                                        payload={"kind": "vision", "status": "unknown"}))
    with factory.begin() as session:
        purge_designs(session, "meeting_image", tmp_path)
    with factory() as session:
        assert capacity_delay_seconds(session) == float("inf")
        assert provider_capacity_available(session, target) is expected


@pytest.mark.parametrize("damage", ["invalid_provider", "mismatched_owner"])
def test_ambiguous_tombstone_with_invalid_provider_or_scope_fails_closed(factory, tmp_path, damage):
    from round_robin.database.visual_models import DesignSession
    from round_robin.orchestration.admission import (
        capacity_delay_seconds,
        provider_capacity_available,
    )
    from round_robin.orchestration.budget import BudgetDenied
    from round_robin.visual.service import purge_designs
    seed(factory)
    seed_image(factory, synthetic_image_configuration())
    with factory.begin() as session:
        operation = session.get(DesignOperation, "image_operation_one")
        operation.payload = {**operation.payload, "status": "authorized"}
        if damage == "invalid_provider": operation.provider = "not_a_provider"
    with factory.begin() as session:
        purge_designs(session, "meeting_image", tmp_path)
    if damage == "mismatched_owner":
        with factory.begin() as session:
            session.get(DesignSession, "design_image").owner_id = "owner_inconsistent"
    with factory() as session:
        assert capacity_delay_seconds(session) == float("inf")
        try:
            available = provider_capacity_available(session, "google")
        except BudgetDenied:
            available = False
        assert not available


@pytest.mark.parametrize("damage", ["invalid_provider", "mismatched_owner"])
def test_active_authorized_visual_with_invalid_provider_or_scope_fails_closed(factory, damage):
    from datetime import timedelta

    from round_robin.database.models import utcnow
    from round_robin.database.visual_models import DesignSession
    from round_robin.orchestration.admission import (
        capacity_delay_seconds,
        provider_capacity_available,
    )
    from round_robin.orchestration.budget import BudgetDenied
    seed(factory)
    seed_image(factory, synthetic_image_configuration())
    with factory.begin() as session:
        operation = session.get(DesignOperation, "image_operation_one")
        operation.payload = {**operation.payload, "status": "authorized", "lease_expires_at": (utcnow() + timedelta(seconds=30)).isoformat()}
        if damage == "invalid_provider": operation.provider = "not_a_provider"
        if damage == "mismatched_owner": session.get(DesignSession, "design_image").owner_id = "owner_inconsistent"
    with factory() as session:
        assert capacity_delay_seconds(session) == float("inf")
        try:
            available = provider_capacity_available(session, "google")
        except BudgetDenied:
            available = False
        assert not available
